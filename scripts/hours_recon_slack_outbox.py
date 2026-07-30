#!/usr/bin/env python3
"""Operate the local Hours Recon Slack MCP outbox through its loopback API."""

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
    parser = argparse.ArgumentParser(description="Read and update the Hours Recon Slack MCP outbox.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="List outbox items.")
    list_parser.add_argument("--status", default="pending", choices=["pending", "sending", "needs_review", "sent", "cancelled"])

    claim_parser = subparsers.add_parser("claim", help="Atomically claim a pending message immediately before Slack MCP send.")
    claim_parser.add_argument("outbox_id")
    claim_parser.add_argument("--version", required=True, type=int)

    sent_parser = subparsers.add_parser("sent", help="Record a Slack MCP permalink after confirmed delivery.")
    sent_parser.add_argument("outbox_id")
    sent_parser.add_argument("--version", required=True, type=int)
    sent_parser.add_argument("--recipient-id", required=True)
    sent_parser.add_argument("--permalink", required=True)

    uncertain_parser = subparsers.add_parser("uncertain", help="Stop retries when Slack MCP delivery is uncertain.")
    uncertain_parser.add_argument("outbox_id")
    uncertain_parser.add_argument("--version", required=True, type=int)
    uncertain_parser.add_argument("--error", required=True)

    retry_parser = subparsers.add_parser("retry", help="Retry only after the user confirms Slack has no delivered copy.")
    retry_parser.add_argument("outbox_id")
    retry_parser.add_argument("--version", required=True, type=int)
    retry_parser.add_argument("--confirmed-not-delivered", required=True, action="store_true")

    args = parser.parse_args()
    token = _token()
    if args.command == "list":
        result = _request("/api/remediation/slack/outbox?" + urlencode({"status": args.status}), token=token)
    elif args.command == "claim":
        result = _request(
            f"/api/remediation/slack/outbox/{args.outbox_id}/claim",
            token=token,
            body={"expected_version": args.version},
        )
    elif args.command == "sent":
        result = _request(
            f"/api/remediation/slack/outbox/{args.outbox_id}/sent",
            token=token,
            body={
                "expected_version": args.version,
                "recipient_id": args.recipient_id,
                "permalink": args.permalink,
            },
        )
    elif args.command == "uncertain":
        result = _request(
            f"/api/remediation/slack/outbox/{args.outbox_id}/uncertain",
            token=token,
            body={"expected_version": args.version, "error": args.error},
        )
    else:
        result = _request(
            f"/api/remediation/slack/outbox/{args.outbox_id}/retry",
            token=token,
            body={"expected_version": args.version, "confirmed_not_delivered": args.confirmed_not_delivered},
        )
    print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
