#!/usr/bin/env python3
"""Operate the local Hours Recon source-action outbox through its loopback API."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

DEFAULT_BASE_URL = "http://127.0.0.1:8765"


def _base_url() -> str:
    value = os.getenv("HOURS_RECON_URL", DEFAULT_BASE_URL).rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme != "http" or (parsed.hostname or "").lower() not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("HOURS_RECON_URL must be a loopback HTTP URL.")
    return value


def _request(path: str, *, token: Optional[str] = None, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    base = _base_url()
    headers = {"Accept": "application/json", "Origin": base}
    data = None
    method = "GET"
    if token:
        headers["X-Hours-Recon-Action-Token"] = token
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
        method = "POST"
    request = Request(base + path, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=10) as response:
            raw = response.read(2_000_000)
            return json.loads(raw.decode("utf-8")) if raw else {}
    except HTTPError as exc:
        raw = exc.read(10_000).decode("utf-8", errors="replace")
        try:
            detail = json.loads(raw).get("error") or raw
        except json.JSONDecodeError:
            detail = raw
        raise RuntimeError(f"Hours Recon API returned {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Hours Recon is unavailable at {base}: {exc.reason}") from exc


def _token() -> str:
    data = _request("/api/data")
    token = str((data.get("remediation_queue") or {}).get("action_token") or "")
    if not token:
        raise RuntimeError("Hours Recon did not return a remediation action token.")
    return token


def main() -> int:
    parser = argparse.ArgumentParser(description="Read and update the Hours Recon source-action outbox.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="List source-action outbox items.")
    list_parser.add_argument("--status", default="pending", choices=["pending", "executing", "needs_review", "completed", "cancelled"])

    claim_parser = subparsers.add_parser("claim", help="Claim an action immediately before the confirmed MCP write.")
    claim_parser.add_argument("outbox_id")
    claim_parser.add_argument("--version", required=True, type=int)

    completed_parser = subparsers.add_parser("completed", help="Record a verified post-write read and source links.")
    completed_parser.add_argument("outbox_id")
    completed_parser.add_argument("--version", required=True, type=int)
    completed_parser.add_argument("--source-link", required=True, action="append")
    completed_parser.add_argument("--result", required=True)

    uncertain_parser = subparsers.add_parser("uncertain", help="Stop retries when a source write result is uncertain.")
    uncertain_parser.add_argument("outbox_id")
    uncertain_parser.add_argument("--version", required=True, type=int)
    uncertain_parser.add_argument("--error", required=True)

    retry_parser = subparsers.add_parser("retry", help="Retry only after a fresh read proves the values were not applied.")
    retry_parser.add_argument("outbox_id")
    retry_parser.add_argument("--version", required=True, type=int)
    retry_parser.add_argument("--confirmed-not-applied", required=True, action="store_true")

    args = parser.parse_args()
    token = _token()
    if args.command == "list":
        result = _request("/api/remediation/source/outbox?" + urlencode({"status": args.status}), token=token)
    elif args.command == "claim":
        result = _request(
            f"/api/remediation/source/outbox/{args.outbox_id}/claim", token=token,
            body={"expected_version": args.version},
        )
    elif args.command == "completed":
        result = _request(
            f"/api/remediation/source/outbox/{args.outbox_id}/completed", token=token,
            body={"expected_version": args.version, "source_links": args.source_link, "result_summary": args.result},
        )
    elif args.command == "uncertain":
        result = _request(
            f"/api/remediation/source/outbox/{args.outbox_id}/uncertain", token=token,
            body={"expected_version": args.version, "error": args.error},
        )
    else:
        result = _request(
            f"/api/remediation/source/outbox/{args.outbox_id}/retry", token=token,
            body={"expected_version": args.version, "confirmed_not_applied": args.confirmed_not_applied},
        )
    print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
