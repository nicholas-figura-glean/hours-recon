"""Small Slack Web API client for direct remediation handoffs."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Optional

from .http_client import ApiError, request_json

DEFAULT_SLACK_API_BASE_URL = "https://slack.com/api"
_SLACK_ID = re.compile(r"^[A-Z][A-Z0-9]{2,}$")


class SlackError(RuntimeError):
    """Base error for a Slack API or delivery failure."""


class SlackRecipientError(SlackError):
    """Raised when a recipient query is missing, ambiguous, or not found."""


class SlackApiError(SlackError):
    """Raised when Slack rejects an authenticated API request."""


def _text(value: Any, maximum: int = 200) -> str:
    return re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or "")).strip()[:maximum]


def _user_label(user: Mapping[str, Any]) -> str:
    profile = user.get("profile") if isinstance(user.get("profile"), Mapping) else {}
    return _text(profile.get("display_name") or user.get("name") or profile.get("real_name") or user.get("real_name") or user.get("id"))


def _user_search_values(user: Mapping[str, Any]) -> List[str]:
    profile = user.get("profile") if isinstance(user.get("profile"), Mapping) else {}
    return [
        _text(user.get("name")).casefold(),
        _text(user.get("real_name")).casefold(),
        _text(profile.get("display_name")).casefold(),
        _text(profile.get("real_name")).casefold(),
        _text(profile.get("email")).casefold(),
    ]


class SlackClient:
    """Resolve Slack recipients and send messages using a bot token."""

    def __init__(self, token: str, *, api_base_url: str = DEFAULT_SLACK_API_BASE_URL) -> None:
        self.token = str(token or "").strip()
        self.api_base_url = str(api_base_url or DEFAULT_SLACK_API_BASE_URL).rstrip("/")
        if not self.token:
            raise ValueError("A Slack bot token is required.")
        if self.api_base_url != DEFAULT_SLACK_API_BASE_URL:
            raise ValueError("Slack API requests are restricted to https://slack.com/api.")

    def _call(
        self,
        method: str,
        *,
        http_method: str = "POST",
        body: Optional[Mapping[str, Any]] = None,
        params: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        try:
            result = request_json(
                http_method,
                f"{self.api_base_url}/{method}",
                headers={"Authorization": f"Bearer {self.token}"},
                body=body,
                params=params,
                timeout=20,
                allowed_origin=self.api_base_url,
            )
        except ApiError as exc:
            raise SlackApiError("Slack could not confirm the request. Check Slack before retrying.") from exc
        if result.get("ok") is not True:
            code = _text(result.get("error") or "unknown_error", 100)
            if code in {"users_not_found", "user_not_found", "channel_not_found"}:
                raise SlackRecipientError("Slack could not find that recipient.")
            if code in {"missing_scope", "not_authed", "invalid_auth", "token_revoked", "account_inactive"}:
                raise SlackApiError(f"Slack authentication is not ready ({code}). Check the Hours Recon Slack app configuration.")
            raise SlackApiError(f"Slack rejected the request ({code}).")
        return result

    def _paged(self, method: str, collection: str, *, params: Optional[Mapping[str, Any]] = None) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        cursor = ""
        for _ in range(20):
            query = {**dict(params or {}), "limit": 200}
            if cursor:
                query["cursor"] = cursor
            result = self._call(method, http_method="GET", params=query)
            rows.extend(dict(item) for item in result.get(collection, []) if isinstance(item, Mapping))
            metadata = result.get("response_metadata") if isinstance(result.get("response_metadata"), Mapping) else {}
            cursor = _text(metadata.get("next_cursor"), 500)
            if not cursor:
                return rows
        raise SlackApiError(f"Slack {collection} pagination exceeded the safety limit.")

    @staticmethod
    def _recipient(kind: str, recipient_id: str, label: str) -> Dict[str, str]:
        return {"kind": kind, "id": recipient_id, "label": label}

    def _user_recipient(self, user: Mapping[str, Any]) -> Dict[str, str]:
        user_id = _text(user.get("id"), 50)
        if not _SLACK_ID.fullmatch(user_id):
            raise SlackRecipientError("Slack returned an invalid user ID.")
        handle = _text(user.get("name"), 120)
        label = f"@{handle}" if handle else _user_label(user)
        return self._recipient("user", user_id, label or user_id)

    def resolve_recipient(self, query: str) -> Dict[str, str]:
        raw = _text(query)
        if not raw:
            raise SlackRecipientError("Enter a Slack user, email, @handle, channel, or Slack ID.")
        upper = raw.upper()
        if _SLACK_ID.fullmatch(upper):
            if upper.startswith(("U", "W")):
                return self._recipient("user", upper, upper)
            if upper.startswith(("C", "G", "D")):
                return self._recipient("channel", upper, upper)

        if raw.startswith("#"):
            target = raw[1:].strip().casefold()
            channels = self._paged(
                "conversations.list",
                "channels",
                params={"types": "public_channel,private_channel", "exclude_archived": "true"},
            )
            matches = [channel for channel in channels if _text(channel.get("name")).casefold() == target]
            if len(matches) == 1:
                channel_id = _text(matches[0].get("id"), 50)
                return self._recipient("channel", channel_id, f"#{_text(matches[0].get('name'))}")
            if len(matches) > 1:
                raise SlackRecipientError(f"More than one Slack channel matched {raw}; use its channel ID.")
            raise SlackRecipientError(f"Slack could not find {raw}, or the Hours Recon app cannot access it.")

        if "@" in raw and not raw.startswith("@"):
            try:
                result = self._call("users.lookupByEmail", http_method="GET", params={"email": raw})
            except SlackRecipientError:
                raise SlackRecipientError(f"Slack could not find a user with email {raw}.") from None
            user = result.get("user")
            if isinstance(user, Mapping):
                return self._user_recipient(user)

        target = raw.removeprefix("@").casefold()
        if not target:
            raise SlackRecipientError("Enter a complete Slack @handle or user name.")
        users = [
            user for user in self._paged("users.list", "members")
            if not user.get("deleted") and not user.get("is_bot") and str(user.get("id")) != "USLACKBOT"
        ]
        exact = [user for user in users if target in _user_search_values(user)]
        if len(exact) == 1:
            return self._user_recipient(exact[0])
        if len(exact) > 1:
            labels = ", ".join(_user_label(user) for user in exact[:5])
            raise SlackRecipientError(f"That Slack recipient is ambiguous: {labels}. Use an @handle or email.")
        suggestions = [user for user in users if any(target in value for value in _user_search_values(user) if value)]
        if suggestions:
            labels = ", ".join(_user_label(user) for user in suggestions[:5])
            raise SlackRecipientError(f"No exact Slack recipient matched {raw}. Possible matches: {labels}. Use an @handle or email.")
        raise SlackRecipientError(f"Slack could not find a recipient matching {raw}.")

    def send_message(self, recipient: Mapping[str, str], message: str, *, client_msg_id: str) -> Dict[str, str]:
        recipient_id = _text(recipient.get("id"), 50)
        kind = _text(recipient.get("kind"), 20)
        if not _SLACK_ID.fullmatch(recipient_id) or kind not in {"user", "channel"}:
            raise SlackRecipientError("The resolved Slack destination is invalid.")
        channel_id = recipient_id
        if kind == "user":
            opened = self._call("conversations.open", body={"users": recipient_id, "return_im": True})
            channel = opened.get("channel") if isinstance(opened.get("channel"), Mapping) else {}
            channel_id = _text(channel.get("id"), 50)
            if not _SLACK_ID.fullmatch(channel_id):
                raise SlackApiError("Slack did not return a valid direct-message channel.")
        posted = self._call(
            "chat.postMessage",
            body={
                "channel": channel_id,
                "text": message,
                "mrkdwn": True,
                "unfurl_links": False,
                "unfurl_media": False,
                "client_msg_id": client_msg_id,
            },
        )
        delivered_channel = _text(posted.get("channel") or channel_id, 50)
        timestamp = _text(posted.get("ts"), 50)
        if not _SLACK_ID.fullmatch(delivered_channel) or not re.fullmatch(r"\d+\.\d+", timestamp):
            raise SlackApiError("Slack accepted the request but did not return delivery evidence.")
        permalink_result = self._call(
            "chat.getPermalink",
            http_method="GET",
            params={"channel": delivered_channel, "message_ts": timestamp},
        )
        permalink = str(permalink_result.get("permalink") or "")
        if not permalink.startswith("https://") or ".slack.com/" not in permalink:
            raise SlackApiError("Slack sent the message but did not return a safe permalink. Check Slack before retrying.")
        return {
            "recipient_id": recipient_id,
            "recipient_label": _text(recipient.get("label") or recipient_id),
            "channel_id": delivered_channel,
            "message_ts": timestamp,
            "permalink": permalink,
            "client_msg_id": client_msg_id,
        }
