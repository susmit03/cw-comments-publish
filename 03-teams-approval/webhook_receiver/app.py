"""FastAPI receiver for Teams approval links."""

from __future__ import annotations

import html
import json
import os
import sys
from typing import Any

import requests
from databricks.sdk import WorkspaceClient
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse

from comments_core import CatalogConfig, compute_changes, execute, extract_comments, generate_sql

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from teams_cards import verify_action_token  # noqa: E402


app = FastAPI(title="Comment Promotion Teams Receiver")


def post_status(text: str, color: str = "0076D7") -> None:
    webhook_url = os.environ.get("TEAMS_STATUS_WEBHOOK_URL") or os.environ.get(
        "TEAMS_WEBHOOK_URL"
    )
    if not webhook_url:
        return
    payload = {
        "@type": "MessageCard",
        "@context": "https://schema.org/extensions",
        "summary": "Comment promotion result",
        "themeColor": color,
        "title": "Comment promotion result",
        "text": text,
    }
    try:
        requests.post(
            webhook_url,
            data=json.dumps(payload),
            headers={"Content-Type": "application/json"},
            timeout=10,
        ).raise_for_status()
    except Exception as exc:  # pragma: no cover
        print(f"Teams status post failed: {exc}")


def run_promotion(source_env: str, target_env: str, reviewer: str) -> dict[str, Any]:
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
    statements = generate_sql(changes, target_catalog)
    results = execute(client, cfg.warehouse_id, statements)
    ok = sum(1 for r in results if r.status == "OK")
    failed = [r for r in results if r.status == "ERROR"]
    return {
        "target_catalog": target_catalog,
        "ok": ok,
        "failed_count": len(failed),
        "failed_sql": [r.sql for r in failed],
        "reviewer": reviewer,
    }


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/teams/decision", response_class=HTMLResponse)
def teams_decision(
    action: str = Query(..., pattern="^(approve|reject)$"),
    token: str = Query(...),
    reviewer: str = Query("teams-user"),
) -> HTMLResponse:
    secret = os.environ.get("APPROVAL_SIGNING_SECRET", "")
    if not secret:
        raise HTTPException(status_code=500, detail="APPROVAL_SIGNING_SECRET is not set")
    try:
        payload = verify_action_token(token, secret)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc))

    source_env = payload["source_env"]
    target_env = payload["target_env"]
    if action == "reject":
        msg = (
            f"Promotion {source_env.upper()} -> {target_env.upper()} was rejected by {reviewer}."
        )
        post_status(msg, color="D13438")
        return HTMLResponse(
            f"<h2>Rejected</h2><p>{html.escape(msg)}</p><p>You can close this tab.</p>"
        )

    try:
        result = run_promotion(source_env=source_env, target_env=target_env, reviewer=reviewer)
    except Exception as exc:
        text = (
            f"Promotion {source_env.upper()} -> {target_env.upper()} failed: {exc}"
        )
        post_status(text, color="D13438")
        return HTMLResponse(
            f"<h2>Failed</h2><p>{html.escape(text)}</p><p>You can close this tab.</p>",
            status_code=500,
        )

    text = (
        f"Promotion {source_env.upper()} -> {target_env.upper()} applied: "
        f"{result['ok']} OK, {result['failed_count']} failed. Reviewer: {reviewer}"
    )
    post_status(text, color="107C10" if result["failed_count"] == 0 else "FF8C00")
    return HTMLResponse(
        f"<h2>Approved</h2><p>{html.escape(text)}</p><p>You can close this tab.</p>"
    )

