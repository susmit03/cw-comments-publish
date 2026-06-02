"""Databricks Job entrypoint: detect comment diffs and post approvals to Teams."""

from __future__ import annotations

import json
import os
import sys
from typing import Any
from urllib.parse import urlencode

import requests
from databricks.sdk import WorkspaceClient

from comments_core import CatalogConfig, compute_changes, extract_comments

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from teams_cards import build_promotion_card, create_action_token  # noqa: E402


def post_to_teams(card: dict[str, Any]) -> None:
    webhook_url = os.environ["TEAMS_WEBHOOK_URL"]
    resp = requests.post(
        webhook_url,
        data=json.dumps(card),
        headers={"Content-Type": "application/json"},
        timeout=15,
    )
    resp.raise_for_status()


def _build_action_url(base_url: str, action: str, token: str) -> str:
    query = urlencode({"action": action, "token": token})
    return f"{base_url.rstrip('/')}/teams/decision?{query}"


def detect_and_notify(source_env: str, target_env: str) -> int:
    cfg = CatalogConfig.from_env()
    client = WorkspaceClient()
    source_catalog = cfg.catalog_for(source_env)
    target_catalog = cfg.catalog_for(target_env)
    source = extract_comments(
        client,
        cfg.warehouse_id,
        source_catalog,
        schema_filter=cfg.allowed_schemas or None,
    )
    target = extract_comments(
        client,
        cfg.warehouse_id,
        target_catalog,
        schema_filter=cfg.allowed_schemas or None,
    )
    changes = compute_changes(source, target)
    if not changes:
        print(f"[{source_env}->{target_env}] no changes; skipping Teams post.")
        return 0

    payload_changes = [
        {
            "schema": c.table.schema,
            "table": c.table.table,
            "kind": c.kind,
            "column": c.column,
            "change_type": c.change_type,
            "old_value": c.old_value,
            "new_value": c.new_value,
        }
        for c in changes
    ]
    signing_secret = os.environ["APPROVAL_SIGNING_SECRET"]
    base_url = os.environ["APPROVAL_BASE_URL"]
    token = create_action_token(
        {
            "source_env": source_env,
            "target_env": target_env,
            "source_catalog": source_catalog,
            "target_catalog": target_catalog,
        },
        signing_secret,
    )
    approve_url = _build_action_url(base_url, "approve", token)
    reject_url = _build_action_url(base_url, "reject", token)

    card = build_promotion_card(
        source_env=source_env,
        target_env=target_env,
        source_catalog=source_catalog,
        target_catalog=target_catalog,
        changes=payload_changes,
        approve_url=approve_url,
        reject_url=reject_url,
    )
    post_to_teams(card)
    print(f"[{source_env}->{target_env}] posted {len(changes)} change(s) to Teams.")
    return len(changes)


def main() -> None:
    total = 0
    total += detect_and_notify("dev", "uat")
    total += detect_and_notify("uat", "prod")
    print(f"Done. Posted {total} total pending change(s).")


if __name__ == "__main__":
    main()

