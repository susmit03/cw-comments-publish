"""Thin wrapper around the SDK's Statement Execution API.

Centralized so extract and apply use the same retry/wait semantics.
"""

from __future__ import annotations

import time
from typing import Any, Optional

from databricks.sdk import WorkspaceClient


_TERMINAL_STATES = {"SUCCEEDED", "FAILED", "CANCELED", "CLOSED"}


def execute_statement(
    client: WorkspaceClient,
    warehouse_id: str,
    statement: str,
    catalog: Optional[str] = None,
    schema: Optional[str] = None,
    wait_timeout_seconds: int = 120,
) -> list[dict[str, Any]]:
    """Run a single statement and return rows as list of dicts.

    Raises RuntimeError on FAILED/CANCELED/CLOSED. Returns [] for statements
    that produce no result set (e.g. ALTER TABLE).
    """
    resp = client.statement_execution.execute_statement(
        statement=statement,
        warehouse_id=warehouse_id,
        catalog=catalog,
        schema=schema,
        wait_timeout="30s",
    )
    deadline = time.monotonic() + wait_timeout_seconds
    while _state_name(resp) not in _TERMINAL_STATES:
        if time.monotonic() > deadline:
            raise RuntimeError(
                f"Statement timed out after {wait_timeout_seconds}s: {statement[:120]}"
            )
        time.sleep(0.5)
        resp = client.statement_execution.get_statement(resp.statement_id)

    state = _state_name(resp)
    if state != "SUCCEEDED":
        err = getattr(resp.status, "error", None) if getattr(resp, "status", None) else None
        message = getattr(err, "message", None) if err else None
        raise RuntimeError(
            f"Statement {state}: {message or 'no error message'} | sql={statement[:200]}"
        )

    if not resp.manifest or not resp.manifest.schema or not resp.manifest.schema.columns:
        return []
    columns = [c.name for c in resp.manifest.schema.columns]
    rows = _collect_all_rows(client, resp)
    return [dict(zip(columns, row)) for row in rows]


def _collect_all_rows(client: WorkspaceClient, statement_response: Any) -> list[list[Any]]:
    """Collect all chunked rows for a successful statement response.

    The Statement API can paginate results across chunks. The first chunk is
    nested under `statement_response.result`; subsequent chunks are fetched by
    index via `get_statement_result_chunk_n`.
    """
    rows: list[list[Any]] = []
    result = getattr(statement_response, "result", None)
    if result and getattr(result, "data_array", None):
        rows.extend(result.data_array)
    next_chunk_index = getattr(result, "next_chunk_index", None) if result else None
    while next_chunk_index is not None:
        chunk = client.statement_execution.get_statement_result_chunk_n(
            statement_response.statement_id, next_chunk_index
        )
        if chunk and getattr(chunk, "data_array", None):
            rows.extend(chunk.data_array)
        next_chunk_index = getattr(chunk, "next_chunk_index", None)
    return rows


def _state_name(resp: Any) -> str:
    """Normalize a statement state to a plain string (handles enum or str)."""
    status = getattr(resp, "status", None)
    state = getattr(status, "state", None)
    if state is None:
        return ""
    if hasattr(state, "value"):
        return str(state.value)
    return str(state)
