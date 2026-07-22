"""Small bounded JSON HTTP client built on urllib."""

from __future__ import annotations

import json
from typing import Any, Dict, Mapping, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

MAX_RESPONSE_BYTES = 10 * 1024 * 1024


class ApiError(RuntimeError):
    pass


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def _origin(url: str) -> Tuple[str, str, int]:
    parsed = urlsplit(url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return parsed.scheme.lower(), (parsed.hostname or "").lower(), port


def _safe_target(url: str, allowed_origin: Optional[str]) -> str:
    parsed = urlsplit(url)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise ApiError("Authenticated API requests require an absolute HTTPS URL.")
    if allowed_origin and _origin(url) != _origin(allowed_origin):
        raise ApiError("Refused an authenticated request to a different API origin.")
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def request_json(
    method: str,
    url: str,
    *,
    headers: Optional[Mapping[str, str]] = None,
    params: Optional[Mapping[str, Any]] = None,
    form: Optional[Mapping[str, Any]] = None,
    body: Optional[Mapping[str, Any]] = None,
    timeout: int = 60,
    allowed_origin: Optional[str] = None,
) -> Dict[str, Any]:
    if params:
        query = urlencode({k: v for k, v in params.items() if v is not None})
        url += ("&" if "?" in url else "?") + query
    safe_target = _safe_target(url, allowed_origin)
    payload = None
    request_headers = {"Accept": "application/json", **dict(headers or {})}
    if form is not None:
        payload = urlencode(form).encode("utf-8")
        request_headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        payload = json.dumps(body).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = Request(url, data=payload, headers=request_headers, method=method.upper())
    opener = build_opener(_NoRedirects())
    try:
        with opener.open(request, timeout=timeout) as response:
            raw_bytes = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw_bytes) > MAX_RESPONSE_BYTES:
                raise ApiError(f"{method.upper()} {safe_target} exceeded the response size limit.")
            raw = raw_bytes.decode("utf-8")
            return json.loads(raw) if raw else {}
    except HTTPError as exc:
        detail = exc.read(1000).decode("utf-8", errors="replace")
        raise ApiError(f"{method.upper()} {safe_target} failed ({exc.code}): {detail}") from exc
    except URLError as exc:
        raise ApiError(f"{method.upper()} {safe_target} failed: {exc.reason}") from exc
