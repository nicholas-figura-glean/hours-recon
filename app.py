#!/usr/bin/env python3
"""Hours Recon local web application."""

from __future__ import annotations

import argparse
import json
import mimetypes
import re
import secrets
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict
from urllib.parse import parse_qs, unquote, urlparse

from hours_recon.config import ROOT, settings
from hours_recon.remediation_store import QueueConflict, QueueError, QueueValidationError
from hours_recon.service import ReconciliationService

STATIC_ROOT = ROOT / "static"


class HoursReconHandler(BaseHTTPRequestHandler):
    server_version = "HoursRecon/0.1"

    @property
    def service(self) -> ReconciliationService:
        return self.server.service  # type: ignore[attr-defined]

    def _request_host_allowed(self) -> bool:
        host = self.headers.get("Host", "").lower()
        port = self.server.server_port
        return host in {"localhost", f"localhost:{port}", "127.0.0.1", f"127.0.0.1:{port}", "[::1]", f"[::1]:{port}"}

    def _origin_allowed(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        parsed = urlparse(origin)
        port = parsed.port or 80
        return parsed.scheme == "http" and (parsed.hostname or "").lower() in {"localhost", "127.0.0.1", "::1"} and port == self.server.server_port

    def _reject_invalid_host(self) -> bool:
        if self._request_host_allowed():
            return False
        self._json(421, {"error": "Invalid local Host header."})
        return True

    def do_GET(self) -> None:
        if self._reject_invalid_host():
            return
        path = urlparse(self.path).path
        if path == "/api/data":
            self._json(200, self.service.data)
            return
        if path == "/api/status":
            self._json(200, self.service.status())
            return
        if path == "/api/remediation/source/outbox":
            if not secrets.compare_digest(self.headers.get("X-Hours-Recon-Action-Token", ""), self.service.action_token):
                self._json(403, {"error": "Invalid remediation action token."})
                return
            query = parse_qs(urlparse(self.path).query)
            status = str((query.get("status") or ["pending"])[0])
            try:
                self._json(200, {"outbox": self.service.list_source_actions(status)})
            except QueueValidationError as exc:
                self._json(400, {"error": str(exc)})
            except QueueError as exc:
                self._json(503, {"error": str(exc)})
            return
        if path == "/api/remediation/slack/outbox":
            if not secrets.compare_digest(self.headers.get("X-Hours-Recon-Action-Token", ""), self.service.action_token):
                self._json(403, {"error": "Invalid remediation action token."})
                return
            query = parse_qs(urlparse(self.path).query)
            status = str((query.get("status") or ["pending"])[0])
            try:
                self._json(200, {"outbox": self.service.list_slack_outbox(status)})
            except QueueValidationError as exc:
                self._json(400, {"error": str(exc)})
            except QueueError as exc:
                self._json(503, {"error": str(exc)})
            return
        if path == "/api/remediation/workstreams":
            query = parse_qs(urlparse(self.path).query)
            allowed = {key: values[0] for key, values in query.items() if key in {"status", "route", "priority", "account_id"} and values}
            try:
                self._json(200, {"workstreams": self.service.list_remediation_workstreams(allowed)})
            except QueueError as exc:
                self._json(503, {"error": str(exc)})
            return
        if path.startswith("/api/remediation/workstreams/"):
            fingerprint = unquote(path.rsplit("/", 1)[-1])
            if not re.fullmatch(r"hrw2_[a-f0-9]{64}", fingerprint):
                self._json(400, {"error": "Invalid remediation workstream ID."})
                return
            try:
                workstream = self.service.get_remediation_workstream(fingerprint)
                self._json(200, {"workstream": workstream}) if workstream else self._json(404, {"error": "Remediation workstream not found."})
            except QueueError as exc:
                self._json(503, {"error": str(exc)})
            return
        self._static(path)

    def do_HEAD(self) -> None:
        if self._reject_invalid_host():
            return
        path = urlparse(self.path).path
        if path in {"/api/data", "/api/status"}:
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self._security_headers()
            self.end_headers()
            return
        relative = "index.html" if path in {"", "/"} else unquote(path.lstrip("/"))
        candidate = (STATIC_ROOT / relative).resolve()
        if candidate.is_file() and (STATIC_ROOT.resolve() in candidate.parents or candidate == STATIC_ROOT.resolve()):
            content_type = mimetypes.guess_type(str(candidate))[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", content_type + ("; charset=utf-8" if content_type.startswith("text/") else ""))
            self.send_header("Content-Length", str(candidate.stat().st_size))
            self.send_header("Cache-Control", "no-store")
            self._security_headers()
            self.end_headers()
        else:
            self.send_response(404)
            self._security_headers()
            self.end_headers()

    def do_POST(self) -> None:
        if self._reject_invalid_host():
            return
        if not self._origin_allowed():
            self._json(403, {"error": "Cross-origin mutation requests are not allowed."})
            return
        path = urlparse(self.path).path
        if path == "/api/refresh":
            try:
                result = self.service.refresh()
                self._json(200, result)
            except Exception as exc:  # Keep last successful cache visible to the UI.
                error_id = secrets.token_hex(4)
                print(f"Refresh error [{error_id}] {type(exc).__name__}: {exc}")
                self._json(500, {
                    "error": f"Refresh failed (reference {error_id}). Check the server log for details.",
                    "preserved_last_success": True,
                })
            return
        source_queue_match = re.fullmatch(r"/api/remediation/workstreams/(hrw2_[a-f0-9]{64})/source/queue", path)
        source_outbox_match = re.fullmatch(r"/api/remediation/source/outbox/(hsa1_[a-f0-9]{64})/(claim|completed|uncertain|retry)", path)
        slack_queue_match = re.fullmatch(r"/api/remediation/workstreams/(hrw2_[a-f0-9]{64})/slack/queue", path)
        outbox_match = re.fullmatch(r"/api/remediation/slack/outbox/(hro1_[a-f0-9]{64})/(claim|sent|uncertain|retry)", path)
        if source_queue_match or source_outbox_match or slack_queue_match or outbox_match:
            if not secrets.compare_digest(self.headers.get("X-Hours-Recon-Action-Token", ""), self.service.action_token):
                self._json(403, {"error": "Invalid remediation action token."})
                return
            try:
                body = self._read_json_body(65536 if source_queue_match else 16384)
                if source_queue_match:
                    proposed_fields = body.get("proposed_fields")
                    if not isinstance(proposed_fields, dict):
                        raise QueueValidationError("Reviewed proposed_fields must be a JSON object.")
                    result = self.service.queue_remediation_source_action(
                        source_queue_match.group(1), expected_version=int(body.get("expected_version")),
                        operation_index=int(body.get("operation_index")), proposed_fields=proposed_fields,
                        confirmed=body.get("confirmed") is True,
                    )
                elif source_outbox_match:
                    outbox_id, operation = source_outbox_match.groups()
                    expected_version = int(body.get("expected_version"))
                    if operation == "claim":
                        result = self.service.claim_source_action(outbox_id, expected_version=expected_version)
                    elif operation == "completed":
                        links = body.get("source_links")
                        if not isinstance(links, list):
                            raise QueueValidationError("source_links must be a JSON array.")
                        result = self.service.complete_source_action(
                            outbox_id, expected_version=expected_version,
                            source_links=[str(value) for value in links],
                            result_summary=str(body.get("result_summary") or ""),
                        )
                    elif operation == "uncertain":
                        result = self.service.mark_source_action_uncertain(
                            outbox_id, expected_version=expected_version, error=str(body.get("error") or ""),
                        )
                    else:
                        result = self.service.retry_source_action(
                            outbox_id, expected_version=expected_version,
                            confirmed_not_applied=body.get("confirmed_not_applied") is True,
                        )
                elif slack_queue_match:
                    result = self.service.queue_remediation_slack(
                        slack_queue_match.group(1),
                        expected_version=int(body.get("expected_version")),
                        recipient_query=str(body.get("recipient") or ""),
                        reviewed_message=str(body.get("message") or ""),
                        confirmed=body.get("confirmed") is True,
                    )
                else:
                    outbox_id, operation = outbox_match.groups()
                    expected_version = int(body.get("expected_version"))
                    if operation == "claim":
                        result = self.service.claim_slack_outbox(outbox_id, expected_version=expected_version)
                    elif operation == "sent":
                        result = self.service.complete_slack_outbox(
                            outbox_id, expected_version=expected_version,
                            recipient_id=str(body.get("recipient_id") or ""),
                            permalink=str(body.get("permalink") or ""),
                        )
                    elif operation == "uncertain":
                        result = self.service.mark_slack_outbox_uncertain(
                            outbox_id, expected_version=expected_version,
                            error=str(body.get("error") or ""),
                        )
                    else:
                        result = self.service.retry_slack_outbox(
                            outbox_id, expected_version=expected_version,
                            confirmed_not_delivered=body.get("confirmed_not_delivered") is True,
                        )
                self._json(200, result)
            except QueueConflict as exc:
                self._json(409, {"error": str(exc)})
            except (QueueValidationError, ValueError, TypeError, KeyError) as exc:
                self._json(400, {"error": str(exc) or "Invalid remediation outbox request."})
            except QueueError as exc:
                self._json(503, {"error": str(exc)})
            return
        match = re.fullmatch(r"/api/remediation/workstreams/(hrw2_[a-f0-9]{64})/actions", path)
        if match:
            if not secrets.compare_digest(self.headers.get("X-Hours-Recon-Action-Token", ""), self.service.action_token):
                self._json(403, {"error": "Invalid remediation action token."})
                return
            try:
                body = self._read_json_body()
                action = str(body.pop("action", ""))
                expected_version = int(body.pop("expected_version"))
                result = self.service.remediation_action(
                    match.group(1), action=action, expected_version=expected_version, payload=body,
                )
                self._json(200, result)
            except QueueConflict as exc:
                self._json(409, {"error": str(exc)})
            except (QueueValidationError, ValueError, TypeError, KeyError) as exc:
                self._json(400, {"error": str(exc) or "Invalid remediation action."})
            except QueueError as exc:
                self._json(503, {"error": str(exc)})
            return
        self._json(404, {"error": "Not found"})

    def _read_json_body(self, maximum_bytes: int = 16384) -> Dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise QueueValidationError("Invalid Content-Length header.") from exc
        if length <= 0 or length > maximum_bytes:
            raise QueueValidationError("JSON request body is missing or too large.")
        previous_timeout = self.connection.gettimeout()
        try:
            self.connection.settimeout(5)
            raw = self.rfile.read(length)
        except socket.timeout as exc:
            raise QueueValidationError("Timed out reading JSON request body.") from exc
        finally:
            self.connection.settimeout(previous_timeout)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise QueueValidationError("Malformed JSON request body.") from exc
        if not isinstance(body, dict):
            raise QueueValidationError("JSON request body must be an object.")
        return body

    def _static(self, request_path: str) -> None:
        relative = "index.html" if request_path in {"", "/"} else unquote(request_path.lstrip("/"))
        candidate = (STATIC_ROOT / relative).resolve()
        if STATIC_ROOT.resolve() not in candidate.parents and candidate != STATIC_ROOT.resolve():
            self._json(403, {"error": "Forbidden"})
            return
        if not candidate.is_file():
            self._json(404, {"error": "Not found"})
            return
        payload = candidate.read_bytes()
        content_type = mimetypes.guess_type(str(candidate))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type + ("; charset=utf-8" if content_type.startswith("text/") else ""))
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self._security_headers()
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, status: int, data: Dict[str, Any]) -> None:
        payload = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self._security_headers()
        self.end_headers()
        self.wfile.write(payload)

    def _security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'; base-uri 'self'; object-src 'none'")
        self.send_header("Referrer-Policy", "no-referrer")

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the AIOM Hours Recon dashboard.")
    parser.add_argument("--host", help="Bind host (default from HOURS_RECON_HOST)")
    parser.add_argument("--port", type=int, help="Bind port (default from HOURS_RECON_PORT)")
    parser.add_argument("--demo", action="store_true", help="Force demo data; no credentials required")
    args = parser.parse_args()

    app_settings = settings()
    if args.demo:
        app_settings["mode"] = "demo"
    host = args.host or app_settings["host"]
    if host.lower() not in {"127.0.0.1", "localhost"}:
        parser.error("Hours Recon only binds to loopback (127.0.0.1 or localhost) because the local API has no remote authentication.")
    port = args.port or app_settings["port"]
    service = ReconciliationService(app_settings)
    server = ThreadingHTTPServer((host, port), HoursReconHandler)
    server.service = service  # type: ignore[attr-defined]
    print(f"Hours Recon is running at http://{host}:{port}")
    print(f"Configured mode: {app_settings['mode']} · displayed mode: {service.data['meta']['mode']}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Hours Recon.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
