"""Teams message helpers and signed approval token utilities."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64url_decode(raw: str) -> bytes:
    pad = "=" * (-len(raw) % 4)
    return base64.urlsafe_b64decode(raw + pad)


def create_action_token(payload: dict[str, Any], secret: str, ttl_seconds: int = 3600) -> str:
    body = dict(payload)
    body["exp"] = int(time.time()) + int(ttl_seconds)
    body_json = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    body_part = _b64url_encode(body_json)
    sig = hmac.new(secret.encode(), body_part.encode(), hashlib.sha256).hexdigest()
    return f"{body_part}.{sig}"


def verify_action_token(token: str, secret: str) -> dict[str, Any]:
    try:
        body_part, sig = token.split(".", 1)
    except ValueError as exc:
        raise ValueError("Invalid token shape") from exc
    expected = hmac.new(secret.encode(), body_part.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        raise ValueError("Invalid token signature")
    body = json.loads(_b64url_decode(body_part).decode())
    exp = int(body.get("exp", 0))
    if exp < int(time.time()):
        raise ValueError("Token expired")
    return body


def _preview_lines(changes: list[dict[str, Any]], limit: int = 12) -> str:
    lines: list[str] = []
    for c in changes[:limit]:
        loc = f"{c['schema']}.{c['table']}"
        if c.get("kind") == "column":
            loc = f"{loc}.{c['column']}"
        lines.append(f"- [{c['change_type']}] {loc}")
    if len(changes) > limit:
        lines.append(f"- ... and {len(changes) - limit} more")
    return "\n".join(lines)


def build_promotion_card(
    source_env: str,
    target_env: str,
    source_catalog: str,
    target_catalog: str,
    changes: list[dict[str, Any]],
    approve_url: str,
    reject_url: str,
) -> dict[str, Any]:
    counts = {"add": 0, "edit": 0, "delete": 0}
    for c in changes:
        counts[c["change_type"]] = counts.get(c["change_type"], 0) + 1
    text = (
        f"**Comment promotion ready: {source_env.upper()} -> {target_env.upper()}**\n\n"
        f"{len(changes)} change(s): {counts['add']} add / {counts['edit']} edit / {counts['delete']} delete\n\n"
        f"`{source_catalog}` -> `{target_catalog}`\n\n"
        f"{_preview_lines(changes)}"
    )
    return {
        "@type": "MessageCard",
        "@context": "https://schema.org/extensions",
        "summary": f"Comment promotion {source_env}->{target_env}",
        "themeColor": "0076D7",
        "title": f"Comment Promotion: {source_env.upper()} -> {target_env.upper()}",
        "text": text,
        "potentialAction": [
            {
                "@type": "OpenUri",
                "name": f"Approve {target_env.upper()}",
                "targets": [{"os": "default", "uri": approve_url}],
            },
            {
                "@type": "OpenUri",
                "name": "Reject",
                "targets": [{"os": "default", "uri": reject_url}],
            },
        ],
    }

