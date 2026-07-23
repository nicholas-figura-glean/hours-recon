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
        if path == "/api/remediation/cases":
            query = parse_qs(urlparse(self.path).query)
            allowed = {key: values[0] for key, values in query.items() if key in {"status", "route", "priority", "account_id"} and values}
            try:
                self._json(200, {"cases": self.service.list_remediation_cases(allowed)})
            except QueueError as exc:
                self._json(503, {"error": str(exc)})
            return
        if path.startswith("/api/remediation/cases/"):
            fingerprint = unquote(path.rsplit("/", 1)[-1])
            if not re.fullmatch(r"hrc1_[a-f0-9]{64}", fingerprint):
                self._json(400, {"error": "Invalid remediation case ID."})
                return
            try:
                case = self.service.get_remediation_case(fingerprint)
                self._json(200, {"case": case}) if case else self._json(404, {"error": "Remediation case not found."})
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
            self._json(403, {"error": "Cross-origin refresh requests are not allowed."})
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
        match = re.fullmatch(r"/api/remediation/gaps/(hrg1_[a-f0-9]{64})/actions", path)
        if match:
            if not secrets.compare_digest(self.headers.get("X-Hours-Recon-Action-Token", ""), self.service.action_token):
                self._json(403, {"error": "Invalid remediation action token."})
                return
            try:
                body = self._read_json_body()
                action = str(body.pop("action", ""))
                expected_version = int(body.pop("expected_version"))
                gap = self.service.remediation_action(
                    match.group(1), action=action, expected_version=expected_version, payload=body,
                )
                self._json(200, {"gap": gap})
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
