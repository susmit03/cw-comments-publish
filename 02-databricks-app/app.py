"""Comment Promotion — Databricks App (Streamlit).

Two-party workflow:
  1. A reviewer picks a table + source/target schemas and sees the comment diff.
  2. The reviewer submits a promotion request — it is stored as PENDING and the
     target table's Unity Catalog owner is resolved as the required approver.
  3. The owner logs into the UI, reviews the pending request they own, and
     approves (applies the change + writes audit) or rejects it.
  4. An audit log records every applied promotion.

The caller's identity comes from the `X-Forwarded-Email` header that the
Databricks Apps proxy adds. SQL runs under the App's service principal token,
which the SDK picks up automatically from env vars when running inside the
Apps runtime.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import uuid
from typing import Optional

import pandas as pd
import streamlit as st
from databricks.sdk import WorkspaceClient
from databricks.sdk.core import Config
from databricks.sdk.credentials_provider import CredentialsStrategy

from comments_core import (
    CatalogConfig,
    compute_changes,
    execute,
    extract_comments,
    generate_sql,
)
from comments_core._sql import execute_statement


st.set_page_config(
    page_title="Comment Promotion",
    page_icon=":memo:",
    layout="wide",
)


# ----------------------------------------------------------------------------
# Boot-time singletons
# ----------------------------------------------------------------------------


def _get_user_token() -> Optional[str]:
    """The logged-in user's OAuth token, forwarded by the Apps proxy."""
    try:
        return st.context.headers.get("x-forwarded-access-token")
    except Exception:
        return None


class _UserOAuthStrategy(CredentialsStrategy):
    """Authenticate as the signed-in user using their forwarded OAuth token.

    Using an explicit strategy (rather than passing `token=`) means the SDK
    does NOT fall back to the app service principal's OAuth env vars
    (DATABRICKS_CLIENT_ID/SECRET), so there's no "more than one authorization
    method configured" conflict — and the identity is genuine user OAuth, not
    a PAT or the service principal.
    """

    def __init__(self, token: str) -> None:
        self._token = token

    def auth_type(self) -> str:
        return "oauth-user"

    def __call__(self, cfg: Config):
        token = self._token
        return lambda: {"Authorization": f"Bearer {token}"}


def get_client() -> WorkspaceClient:
    """WorkspaceClient that acts as the logged-in user (on-behalf-of).

    Databricks Apps forwards the user's OAuth access token in the
    `x-forwarded-access-token` header. We authenticate with it so every query,
    DDL, and audit write runs under the *user's* identity and permissions —
    never the app service principal. Falls back to ambient auth (CLI profile
    or SP) for local development where the header is absent.
    """
    token = _get_user_token()
    if token:
        host = os.environ.get("DATABRICKS_HOST") or Config().host
        return WorkspaceClient(
            host=host, credentials_strategy=_UserOAuthStrategy(token)
        )
    return WorkspaceClient()


@st.cache_resource
def get_config() -> CatalogConfig:
    return CatalogConfig.from_env()


def get_audit_table() -> str:
    return os.environ.get("AUDIT_TABLE", "")


def get_reviewer_email() -> str:
    """Pull the calling user's email from the Apps proxy header."""
    try:
        headers = st.context.headers
    except Exception:
        headers = {}
    return (
        headers.get("X-Forwarded-Email")
        or headers.get("X-Forwarded-User")
        or os.environ.get("DATABRICKS_APP_INVOKER_EMAIL")
        or "anonymous@local"
    )


# ----------------------------------------------------------------------------
# Audit table
# ----------------------------------------------------------------------------


_AUDIT_DDL = """
CREATE TABLE IF NOT EXISTS {audit_table} (
  promoted_at      TIMESTAMP,
  promoted_by      STRING,
  source_catalog   STRING,
  target_catalog   STRING,
  kind             STRING,
  schema_name      STRING,
  table_name       STRING,
  column_name      STRING,
  old_value        STRING,
  new_value        STRING,
  sql              STRING,
  status           STRING,
  error            STRING
) USING DELTA
"""


def ensure_audit_table() -> Optional[str]:
    audit_table = get_audit_table()
    if not audit_table:
        return "AUDIT_TABLE env var is not set; audit logging disabled."
    try:
        execute_statement(
            get_client(),
            get_config().warehouse_id,
            _AUDIT_DDL.format(audit_table=audit_table),
        )
        return None
    except Exception as exc:
        return f"Could not create audit table {audit_table}: {exc}"


def _sql_literal(value: Optional[str]) -> str:
    if value is None:
        return "NULL"
    return "'" + value.replace("'", "''") + "'"


def write_audit_rows(
    rows: list[dict],
) -> Optional[str]:
    """Append one row per change to the audit table."""
    audit_table = get_audit_table()
    if not audit_table or not rows:
        return None

    value_tuples = []
    for r in rows:
        value_tuples.append(
            "("
            + ", ".join(
                [
                    f"TIMESTAMP '{r['promoted_at']}'",
                    _sql_literal(r["promoted_by"]),
                    _sql_literal(r["source_catalog"]),
                    _sql_literal(r["target_catalog"]),
                    _sql_literal(r["kind"]),
                    _sql_literal(r["schema_name"]),
                    _sql_literal(r["table_name"]),
                    _sql_literal(r["column_name"]),
                    _sql_literal(r["old_value"]),
                    _sql_literal(r["new_value"]),
                    _sql_literal(r["sql"]),
                    _sql_literal(r["status"]),
                    _sql_literal(r["error"]),
                ]
            )
            + ")"
        )

    insert_sql = (
        f"INSERT INTO {audit_table} (promoted_at, promoted_by, source_catalog, "
        "target_catalog, kind, schema_name, table_name, column_name, "
        "old_value, new_value, sql, status, error) VALUES "
        + ",\n".join(value_tuples)
    )

    try:
        execute_statement(get_client(), get_config().warehouse_id, insert_sql)
        return None
    except Exception as exc:
        return f"Audit write failed: {exc}"


def read_audit_rows(limit: int = 100) -> pd.DataFrame:
    audit_table = get_audit_table()
    if not audit_table:
        return pd.DataFrame()
    sql = (
        f"SELECT promoted_at, promoted_by, source_catalog, target_catalog, "
        f"kind, schema_name, table_name, column_name, status "
        f"FROM {audit_table} ORDER BY promoted_at DESC LIMIT {int(limit)}"
    )
    try:
        rows = execute_statement(get_client(), get_config().warehouse_id, sql)
        return pd.DataFrame(rows)
    except Exception as exc:
        st.warning(f"Could not read audit table: {exc}")
        return pd.DataFrame()


# ----------------------------------------------------------------------------
# Requests table (owner-approval workflow)
# ----------------------------------------------------------------------------

STATUS_PENDING = "PENDING"
STATUS_APPLIED = "APPLIED"
STATUS_REJECTED = "REJECTED"
STATUS_FAILED = "FAILED"


def get_requests_table() -> str:
    """Three-part name for the pending-requests table.

    Defaults to a sibling of the audit table (`<catalog>.<schema>.comment_promotion_requests`)
    unless `REQUESTS_TABLE` is set explicitly.
    """
    explicit = os.environ.get("REQUESTS_TABLE", "")
    if explicit:
        return explicit
    audit = get_audit_table()
    parts = audit.split(".")
    if len(parts) == 3:
        return f"{parts[0]}.{parts[1]}.comment_promotion_requests"
    return ""


_REQUESTS_DDL = """
CREATE TABLE IF NOT EXISTS {requests_table} (
  request_id       STRING,
  requested_at     TIMESTAMP,
  requested_by     STRING,
  source_env       STRING,
  target_env       STRING,
  source_catalog   STRING,
  target_catalog   STRING,
  source_schema    STRING,
  target_schema    STRING,
  table_name       STRING,
  table_owner      STRING,
  changes_json     STRING,
  status           STRING,
  decided_by       STRING,
  decided_at       TIMESTAMP,
  note             STRING
) USING DELTA
"""


def ensure_requests_table() -> Optional[str]:
    requests_table = get_requests_table()
    if not requests_table:
        return (
            "Could not derive a requests table name; set REQUESTS_TABLE or a "
            "three-part AUDIT_TABLE. Approval workflow disabled."
        )
    try:
        execute_statement(
            get_client(),
            get_config().warehouse_id,
            _REQUESTS_DDL.format(requests_table=requests_table),
        )
        return None
    except Exception as exc:
        return f"Could not create requests table {requests_table}: {exc}"


def get_table_owner(catalog: str, schema: str, table: str) -> tuple[Optional[str], Optional[str]]:
    """Return (owner, error) for a Unity Catalog table.

    Resolved via `DESCRIBE TABLE EXTENDED` so it only needs the `sql` scope
    (same as every other query the app runs on behalf of the user).
    """
    full_name = f"`{catalog}`.`{schema}`.`{table}`"
    try:
        rows = execute_statement(
            get_client(),
            get_config().warehouse_id,
            f"DESCRIBE TABLE EXTENDED {full_name}",
        )
        for row in rows:
            if str(row.get("col_name", "")).strip().lower() == "owner":
                owner = str(row.get("data_type", "")).strip()
                if owner:
                    return owner, None
        return None, f"No owner recorded for {catalog}.{schema}.{table}."
    except Exception as exc:
        return None, f"Could not resolve owner for {catalog}.{schema}.{table}: {exc}"


def submit_request(
    source_env: str,
    target_env: str,
    source_schema: str,
    target_schema: str,
    table_name: str,
    changes: list[dict],
    requested_by: str,
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Insert a PENDING request. Returns (request_id, owner, error)."""
    requests_table = get_requests_table()
    if not requests_table:
        return None, None, "Requests table is not configured."

    cfg = get_config()
    target_catalog = cfg.catalog_for(target_env)
    source_catalog = cfg.catalog_for(source_env)

    owner, owner_err = get_table_owner(target_catalog, target_schema, table_name)
    if owner_err:
        return None, None, owner_err

    request_id = str(uuid.uuid4())
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    changes_json = json.dumps(changes)

    insert_sql = (
        f"INSERT INTO {requests_table} (request_id, requested_at, requested_by, "
        "source_env, target_env, source_catalog, target_catalog, source_schema, "
        "target_schema, table_name, table_owner, changes_json, status, decided_by, "
        "decided_at, note) VALUES ("
        + ", ".join(
            [
                _sql_literal(request_id),
                f"TIMESTAMP '{now}'",
                _sql_literal(requested_by),
                _sql_literal(source_env),
                _sql_literal(target_env),
                _sql_literal(source_catalog),
                _sql_literal(target_catalog),
                _sql_literal(source_schema),
                _sql_literal(target_schema),
                _sql_literal(table_name),
                _sql_literal(owner),
                _sql_literal(changes_json),
                _sql_literal(STATUS_PENDING),
                "NULL",
                "NULL",
                "NULL",
            ]
        )
        + ")"
    )
    try:
        execute_statement(get_client(), cfg.warehouse_id, insert_sql)
        return request_id, owner, None
    except Exception as exc:
        return None, owner, f"Could not save request: {exc}"


def read_requests(where_sql: str = "", limit: int = 200) -> pd.DataFrame:
    requests_table = get_requests_table()
    if not requests_table:
        return pd.DataFrame()
    clause = f"WHERE {where_sql} " if where_sql else ""
    sql = (
        f"SELECT request_id, requested_at, requested_by, source_env, target_env, "
        f"source_catalog, target_catalog, source_schema, target_schema, table_name, "
        f"table_owner, changes_json, status, decided_by, decided_at, note "
        f"FROM {requests_table} {clause}ORDER BY requested_at DESC LIMIT {int(limit)}"
    )
    try:
        rows = execute_statement(get_client(), get_config().warehouse_id, sql)
        return pd.DataFrame(rows)
    except Exception as exc:
        st.warning(f"Could not read requests table: {exc}")
        return pd.DataFrame()


def has_open_request(target_env: str, target_schema: str, table_name: str) -> Optional[dict]:
    """Return the latest PENDING request row for this target table, or None."""
    where = (
        f"status = '{STATUS_PENDING}' AND target_env = '{target_env}' "
        f"AND target_schema = '{target_schema.replace(chr(39), chr(39) * 2)}' "
        f"AND table_name = '{table_name.replace(chr(39), chr(39) * 2)}'"
    )
    df = read_requests(where_sql=where, limit=1)
    if df.empty:
        return None
    return df.iloc[0].to_dict()


def _apply_changes(
    changes: list[dict],
    source_catalog: str,
    target_catalog: str,
    applied_by: str,
) -> tuple[int, list, list]:
    """Apply change dicts to the target catalog and write audit rows.

    Returns (ok_count, results, statements).
    """
    from comments_core.models import CommentChange, TableRef

    client = get_client()
    cfg = get_config()
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    change_objs = [
        CommentChange(
            table=TableRef(schema=row["schema"], table=row["table"]),
            kind=row["kind"],
            column=row["column"],
            old_value=row["old_value"],
            new_value=row["new_value"],
        )
        for row in changes
    ]
    statements = generate_sql(change_objs, target_catalog)
    results = execute(client, cfg.warehouse_id, statements)

    audit_rows = []
    for change, stmt, result in zip(change_objs, statements, results):
        audit_rows.append(
            {
                "promoted_at": now,
                "promoted_by": applied_by,
                "source_catalog": source_catalog,
                "target_catalog": target_catalog,
                "kind": change.kind,
                "schema_name": change.table.schema,
                "table_name": change.table.table,
                "column_name": change.column,
                "old_value": change.old_value,
                "new_value": change.new_value,
                "sql": stmt,
                "status": result.status,
                "error": result.error,
            }
        )
    audit_err = write_audit_rows(audit_rows)
    if audit_err:
        st.warning(audit_err)

    ok = sum(1 for r in results if r.status == "OK")
    return ok, results, statements


def _update_request_status(
    request_id: str, status: str, decided_by: str, note: str = ""
) -> Optional[str]:
    requests_table = get_requests_table()
    if not requests_table:
        return "Requests table is not configured."
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    sql = (
        f"UPDATE {requests_table} SET status = {_sql_literal(status)}, "
        f"decided_by = {_sql_literal(decided_by)}, "
        f"decided_at = TIMESTAMP '{now}', note = {_sql_literal(note)} "
        f"WHERE request_id = {_sql_literal(request_id)}"
    )
    try:
        execute_statement(get_client(), get_config().warehouse_id, sql)
        return None
    except Exception as exc:
        return f"Could not update request: {exc}"


def decide_request(request: dict, approve: bool, decider: str) -> None:
    """Owner-only: approve (apply + audit) or reject a pending request."""
    request_id = request["request_id"]

    if not approve:
        err = _update_request_status(request_id, STATUS_REJECTED, decider, "Rejected by owner.")
        if err:
            st.error(err)
        else:
            st.success(f"Request {request_id[:8]} rejected.")
        return

    try:
        changes = json.loads(request.get("changes_json") or "[]")
    except (ValueError, TypeError):
        changes = []
    if not changes:
        _update_request_status(request_id, STATUS_APPLIED, decider, "No changes to apply.")
        st.info("No changes to apply; marked as applied.")
        return

    target_catalog = request["target_catalog"]
    source_catalog = request["source_catalog"]
    with st.spinner(f"Applying {len(changes)} change(s) to {target_catalog} ..."):
        ok, results, _ = _apply_changes(changes, source_catalog, target_catalog, decider)

    failed = [r for r in results if r.status == "ERROR"]
    if failed:
        note = f"{len(failed)} statement(s) failed."
        _update_request_status(request_id, STATUS_FAILED, decider, note)
        st.error(note)
        for r in failed:
            st.code(r.sql, language="sql")
            st.text(r.error)
    else:
        _update_request_status(
            request_id, STATUS_APPLIED, decider, f"Applied {ok} statement(s)."
        )
        st.success(f"Approved and applied {ok}/{len(results)} change(s) to {target_catalog}.")


# ----------------------------------------------------------------------------
# Demo data seeding
# ----------------------------------------------------------------------------

DEMO_TABLE = "demo_orders"

_DEMO_COLUMN_TYPES = [
    ("order_id", "BIGINT"),
    ("customer_id", "BIGINT"),
    ("amount", "DECIMAL(10,2)"),
    ("status", "STRING"),
]

# Identical table in all three schemas, with intentionally *slightly different*
# comments so DEV->UAT and UAT->PROD both show a meaningful diff to demo.
_DEMO_SPEC = {
    "dev": {
        "table": "Customer orders — one row per order (enriched)",
        "columns": {
            "order_id": "Primary key — globally unique order identifier",
            "customer_id": "Foreign key to the customer dimension",
            "amount": "Order total in USD, tax inclusive",
            "status": "Order lifecycle status (NEW, PAID, SHIPPED)",
        },
    },
    "uat": {
        "table": "Customer orders table",
        "columns": {
            "order_id": "Primary key for orders",
            "customer_id": "Foreign key to the customer dimension",
            "amount": "Order total in USD",
            "status": "Order lifecycle status (NEW, PAID, SHIPPED)",
        },
    },
    "prod": {
        "table": "Orders",
        "columns": {
            "order_id": "Order identifier",
            "customer_id": "Customer reference",
            "amount": "Order amount",
            "status": "Status",
        },
    },
}


def seed_demo_data(
    schemas: dict[str, str], owner: Optional[str] = None
) -> tuple[int, list[str]]:
    """Create/reset the demo table in each env's schema. Returns (ok, errors).

    If `owner` is provided, ownership of each demo table is transferred to that
    principal so the owner-approval workflow has a human approver (otherwise the
    tables would be owned by the app's service principal).
    """
    client = get_client()
    cfg = get_config()
    ok = 0
    errors: list[str] = []
    for env in ("dev", "uat", "prod"):
        schema = schemas.get(env, "")
        if not schema:
            errors.append(f"{env.upper()} schema is empty; skipped.")
            continue
        catalog = cfg.catalog_for(env)
        spec = _DEMO_SPEC[env]
        col_defs = ",\n".join(
            f"  `{name}` {typ} COMMENT {_sql_literal(spec['columns'][name])}"
            for name, typ in _DEMO_COLUMN_TYPES
        )
        statements = [
            f"CREATE SCHEMA IF NOT EXISTS `{catalog}`.`{schema}`",
            (
                f"CREATE OR REPLACE TABLE `{catalog}`.`{schema}`.`{DEMO_TABLE}` (\n"
                f"{col_defs}\n) USING DELTA COMMENT {_sql_literal(spec['table'])}"
            ),
        ]
        if owner and "@" in owner:
            statements.append(
                f"ALTER TABLE `{catalog}`.`{schema}`.`{DEMO_TABLE}` OWNER TO `{owner}`"
            )
        for stmt in statements:
            try:
                execute_statement(client, cfg.warehouse_id, stmt)
                ok += 1
            except Exception as exc:
                errors.append(f"{catalog}.{schema}: {exc}")
    return ok, errors


# ----------------------------------------------------------------------------
# Diff loading (cached for 60 seconds)
# ----------------------------------------------------------------------------


@st.cache_data(ttl=60, show_spinner=False)
def load_diff(
    source_env: str,
    target_env: str,
    source_schema: str,
    target_schema: str,
    table_name: str,
) -> dict:
    """Diff comments for a single table across two envs.

    The source and target schemas may differ (the same logical table can live
    under different schemas in DEV/UAT/PROD). Returns a JSON-serializable dict
    so Streamlit can cache it.
    """
    from comments_core.models import CommentSet, TableRef

    cfg = get_config()
    client = get_client()
    source_catalog = cfg.catalog_for(source_env)
    target_catalog = cfg.catalog_for(target_env)

    source = extract_comments(
        client,
        cfg.warehouse_id,
        source_catalog,
        schema_filter=[source_schema],
        table_filter=[table_name],
    )
    target = extract_comments(
        client,
        cfg.warehouse_id,
        target_catalog,
        schema_filter=[target_schema],
        table_filter=[table_name],
    )

    src_ref = TableRef(schema=source_schema, table=table_name)
    tgt_ref = TableRef(schema=target_schema, table=table_name)
    source_found = src_ref in source.tables
    target_found = tgt_ref in target.tables

    changes: list = []
    if source_found and target_found:
        # Remap both tables onto the target ref so compute_changes can pair
        # them even when the source and target schemas differ. Emitted changes
        # carry the target schema so the generated DDL hits the right table.
        src_cs = CommentSet(catalog=source_catalog)
        src_cs.tables[tgt_ref] = source.tables[src_ref]
        tgt_cs = CommentSet(catalog=target_catalog)
        tgt_cs.tables[tgt_ref] = target.tables[tgt_ref]
        changes = compute_changes(src_cs, tgt_cs)

    return {
        "source_found": source_found,
        "target_found": target_found,
        "changes": [
            {
                "schema": c.table.schema,
                "table": c.table.table,
                "kind": c.kind,
                "column": c.column,
                "old_value": c.old_value,
                "new_value": c.new_value,
                "change_type": c.change_type,
            }
            for c in changes
        ],
    }


@st.cache_data(ttl=60, show_spinner=False)
def scan_schema(
    dev_catalog: str,
    dev_schema: str,
    uat_catalog: str,
    uat_schema: str,
    prod_catalog: str,
    prod_schema: str,
) -> list[dict]:
    """Scan every table in the DEV schema and report comment drift vs UAT/PROD.

    DEV is treated as the source of truth. For each DEV table we report whether
    it exists in UAT/PROD and how many comment changes would be needed to bring
    each target into line with DEV.
    """
    from comments_core.models import CommentSet, TableRef

    client = get_client()
    cfg = get_config()

    dev = extract_comments(client, cfg.warehouse_id, dev_catalog, schema_filter=[dev_schema])
    uat = extract_comments(client, cfg.warehouse_id, uat_catalog, schema_filter=[uat_schema])
    prod = extract_comments(client, cfg.warehouse_id, prod_catalog, schema_filter=[prod_schema])

    def by_table(cs) -> dict:
        return {ref.table: comments for ref, comments in cs.tables.items()}

    dev_t = by_table(dev)
    uat_t = by_table(uat)
    prod_t = by_table(prod)

    def diff_count(src_comments, tgt_map: dict, tgt_schema: str, table: str):
        if table not in tgt_map:
            return None
        ref = TableRef(schema=tgt_schema, table=table)
        a = CommentSet(catalog="src")
        a.tables[ref] = src_comments
        b = CommentSet(catalog="tgt")
        b.tables[ref] = tgt_map[table]
        return len(compute_changes(a, b))

    rows: list[dict] = []
    for table in sorted(dev_t):
        uat_diffs = diff_count(dev_t[table], uat_t, uat_schema, table)
        prod_diffs = diff_count(dev_t[table], prod_t, prod_schema, table)
        in_uat = table in uat_t
        in_prod = table in prod_t
        drift = bool((uat_diffs or 0) or (prod_diffs or 0)) or not in_uat or not in_prod
        rows.append(
            {
                "table": table,
                "in_uat": in_uat,
                "uat_diffs": uat_diffs if uat_diffs is not None else None,
                "in_prod": in_prod,
                "prod_diffs": prod_diffs if prod_diffs is not None else None,
                "status": "DRIFT" if drift else "in sync",
            }
        )
    return rows


# ----------------------------------------------------------------------------
# UI
# ----------------------------------------------------------------------------


def _render_changes_table(
    changes: list[dict], source_env: str, target_env: str
) -> None:
    df = pd.DataFrame(changes)
    display = df[["kind", "column", "change_type", "old_value", "new_value"]].rename(
        columns={
            "kind": "Kind",
            "column": "Column",
            "change_type": "Change",
            "old_value": f"Current ({target_env})",
            "new_value": f"Desired ({source_env})",
        }
    )
    st.dataframe(display, hide_index=True, use_container_width=True)


def render_diff_section(
    source_env: str,
    target_env: str,
    table_name: str,
    source_schema: str,
    target_schema: str,
) -> None:
    cfg = get_config()
    target_catalog = cfg.catalog_for(target_env)
    source_catalog = cfg.catalog_for(source_env)

    st.markdown(
        f"### {source_env.upper()} `{source_catalog}.{source_schema or '?'}.{table_name or '?'}` "
        f"→ {target_env.upper()} `{target_catalog}.{target_schema or '?'}.{table_name or '?'}`"
    )

    if not table_name or not source_schema or not target_schema:
        st.info(
            "Enter a table name and the source/target schemas in the sidebar "
            "to load a diff."
        )
        return

    try:
        result = load_diff(
            source_env, target_env, source_schema, target_schema, table_name
        )
    except Exception as exc:
        st.error(f"Could not load diff: {exc}")
        return

    if not result["source_found"]:
        st.warning(
            f"Table `{source_catalog}.{source_schema}.{table_name}` not found in "
            f"{source_env.upper()}."
        )
        return
    if not result["target_found"]:
        st.warning(
            f"Table `{target_catalog}.{target_schema}.{table_name}` not found in "
            f"{target_env.upper()}. It must exist before promoting."
        )
        return

    changes = result["changes"]
    counts = {"add": 0, "edit": 0, "delete": 0}
    for c in changes:
        counts[c["change_type"]] = counts.get(c["change_type"], 0) + 1

    cols = st.columns(4)
    cols[0].metric("Total changes", len(changes))
    cols[1].metric("Adds", counts["add"])
    cols[2].metric("Edits", counts["edit"])
    cols[3].metric("Deletes", counts["delete"])

    if not changes:
        st.success(f"`{target_catalog}.{target_schema}.{table_name}` is up to date.")
        return

    _render_changes_table(changes, source_env, target_env)

    # Resolve and surface the required approver (the target table's owner).
    owner, owner_err = get_table_owner(target_catalog, target_schema, table_name)
    if owner_err:
        st.error(owner_err)
        return
    st.info(
        f"Approval required from the table owner: **{owner}**. "
        "Submitting creates a pending request they must approve in the "
        "**Approvals** tab before anything is applied."
    )

    existing = has_open_request(target_env, target_schema, table_name)
    if existing:
        st.warning(
            f"A pending request already exists for this table "
            f"(id `{existing['request_id'][:8]}`, submitted by "
            f"{existing['requested_by']}). Approve or reject it before "
            "submitting a new one."
        )
        return

    confirm_phrase = f"SUBMIT {target_env.upper()}"
    typed = st.text_input(
        f"Type **{confirm_phrase}** to enable the Submit button",
        key=f"confirm-{target_env}",
        placeholder=confirm_phrase,
    )
    submit_disabled = typed.strip() != confirm_phrase

    if st.button(
        f"Submit for owner approval → {target_catalog}.{target_schema}.{table_name}",
        type="primary",
        disabled=submit_disabled,
        key=f"submit-{target_env}",
    ):
        request_id, resolved_owner, err = submit_request(
            source_env,
            target_env,
            source_schema,
            target_schema,
            table_name,
            changes,
            get_reviewer_email(),
        )
        if err:
            st.error(err)
        else:
            st.success(
                f"Request `{request_id[:8]}` submitted. Awaiting approval from "
                f"**{resolved_owner}**."
            )
            load_diff.clear()


def render_approvals() -> None:
    st.markdown("### Pending approvals you own")
    me = get_reviewer_email()
    st.caption(f"Showing requests where the table owner is **{me}**.")

    me_escaped = me.replace("'", "''")
    where = f"status = '{STATUS_PENDING}' AND lower(table_owner) = lower('{me_escaped}')"
    df = read_requests(where_sql=where, limit=200)

    if df.empty:
        st.info("No pending requests await your approval.")
        return

    for _, row in df.iterrows():
        req = row.to_dict()
        rid = req["request_id"]
        with st.container(border=True):
            st.markdown(
                f"**`{req['target_catalog']}.{req['target_schema']}.{req['table_name']}`** "
                f"— {req['source_env'].upper()} → {req['target_env'].upper()}"
            )
            st.caption(
                f"Request `{rid[:8]}` · requested by {req['requested_by']} · "
                f"{req['requested_at']}"
            )
            try:
                changes = json.loads(req.get("changes_json") or "[]")
            except (ValueError, TypeError):
                changes = []
            if changes:
                _render_changes_table(changes, req["source_env"], req["target_env"])
            else:
                st.write("No changes recorded in this request.")

            c1, c2, _ = st.columns([1, 1, 4])
            if c1.button("Approve & apply", type="primary", key=f"approve-{rid}"):
                decide_request(req, approve=True, decider=me)
                st.rerun()
            if c2.button("Reject", key=f"reject-{rid}"):
                decide_request(req, approve=False, decider=me)
                st.rerun()


def render_my_requests() -> None:
    st.markdown("### My requests")
    me = get_reviewer_email()
    me_escaped = me.replace("'", "''")
    where = f"lower(requested_by) = lower('{me_escaped}')"
    df = read_requests(where_sql=where, limit=200)
    if df.empty:
        st.info("You have not submitted any requests yet.")
        return
    view = df[
        [
            "request_id",
            "requested_at",
            "target_env",
            "target_schema",
            "table_name",
            "table_owner",
            "status",
            "decided_by",
            "decided_at",
        ]
    ].copy()
    view["request_id"] = view["request_id"].str.slice(0, 8)
    st.dataframe(view, hide_index=True, use_container_width=True)


def render_history() -> None:
    st.markdown("### Recent applied promotions")
    df = read_audit_rows(limit=200)
    if df.empty:
        st.info("No promotions recorded yet.")
        return
    st.dataframe(df, hide_index=True, use_container_width=True)


def render_scan() -> None:
    st.markdown("### Drift scan")
    st.caption(
        "Scan every table in a DEV catalog/schema and see which tables have "
        "comment drift versus their UAT and PROD counterparts. DEV is the "
        "source of truth."
    )

    cfg = get_config()
    c1, c2, c3 = st.columns(3)
    with c1:
        st.markdown("**DEV**")
        dev_catalog = st.text_input("DEV catalog", value=cfg.dev_catalog, key="scan_dev_cat")
        dev_schema = st.text_input(
            "DEV schema", value=st.session_state.get("dev_schema", ""), key="scan_dev_schema"
        )
    with c2:
        st.markdown("**UAT**")
        uat_catalog = st.text_input("UAT catalog", value=cfg.uat_catalog, key="scan_uat_cat")
        uat_schema = st.text_input(
            "UAT schema", value=st.session_state.get("uat_schema", ""), key="scan_uat_schema"
        )
    with c3:
        st.markdown("**PROD**")
        prod_catalog = st.text_input("PROD catalog", value=cfg.prod_catalog, key="scan_prod_cat")
        prod_schema = st.text_input(
            "PROD schema", value=st.session_state.get("prod_schema", ""), key="scan_prod_schema"
        )

    if not (dev_catalog and dev_schema and uat_catalog and uat_schema and prod_catalog and prod_schema):
        st.info("Fill in all three catalog/schema pairs to scan.")
        return

    run = st.button("Scan for drift", type="primary")
    if st.button("Refresh scan"):
        scan_schema.clear()
        st.rerun()

    if not run:
        return

    try:
        with st.spinner("Scanning DEV tables ..."):
            rows = scan_schema(
                dev_catalog.strip(),
                dev_schema.strip(),
                uat_catalog.strip(),
                uat_schema.strip(),
                prod_catalog.strip(),
                prod_schema.strip(),
            )
    except Exception as exc:
        st.error(f"Scan failed: {exc}")
        return

    if not rows:
        st.warning(f"No tables found in {dev_catalog}.{dev_schema}.")
        return

    drifted = [r for r in rows if r["status"] == "DRIFT"]
    cols = st.columns(3)
    cols[0].metric("Tables scanned", len(rows))
    cols[1].metric("With drift", len(drifted))
    cols[2].metric("In sync", len(rows) - len(drifted))

    df = pd.DataFrame(rows).rename(
        columns={
            "table": "Table",
            "in_uat": "In UAT",
            "uat_diffs": "UAT diffs",
            "in_prod": "In PROD",
            "prod_diffs": "PROD diffs",
            "status": "Status",
        }
    )
    st.dataframe(df, hide_index=True, use_container_width=True)


def main() -> None:
    st.title("Comment promotion")
    st.caption(
        "Promote Unity Catalog table + column comments from DEV through UAT to PROD."
    )

    audit_warn = ensure_audit_table()
    if audit_warn:
        st.warning(audit_warn)

    requests_warn = ensure_requests_table()
    if requests_warn:
        st.warning(requests_warn)

    cfg = get_config()
    fallback_schema = cfg.allowed_schemas[0] if cfg.allowed_schemas else ""
    default_dev = os.environ.get("DEV_SCHEMA", "") or fallback_schema
    default_uat = os.environ.get("UAT_SCHEMA", "") or fallback_schema
    default_prod = os.environ.get("PROD_SCHEMA", "") or fallback_schema
    default_table = os.environ.get("DEFAULT_TABLE", "")
    with st.sidebar:
        st.subheader("Reviewer")
        st.markdown(f"**{get_reviewer_email()}**")
        if _get_user_token():
            st.caption("SQL runs as: **you** (on-behalf-of-user)")
        else:
            st.caption("SQL runs as: app service principal (fallback)")
        st.divider()
        st.subheader("Table selection")
        table_name = st.text_input(
            "Table name", value=default_table, key="table_name", placeholder="orders"
        ).strip()
        dev_schema = st.text_input(
            "DEV schema", value=default_dev, key="dev_schema", placeholder="sales"
        ).strip()
        uat_schema = st.text_input(
            "UAT schema", value=default_uat, key="uat_schema", placeholder="sales"
        ).strip()
        prod_schema = st.text_input(
            "PROD schema", value=default_prod, key="prod_schema", placeholder="sales"
        ).strip()
        st.divider()
        st.subheader("Catalogs")
        st.code(
            f"DEV  = {cfg.dev_catalog}\nUAT  = {cfg.uat_catalog}\nPROD = {cfg.prod_catalog}\n"
            f"Warehouse = {cfg.warehouse_id}",
            language="text",
        )
        st.divider()
        with st.expander("Demo data"):
            st.caption(
                "Create/reset an identical `demo_orders` table in all three "
                "schemas with slightly different comments."
            )
            if st.button("Seed demo table", use_container_width=True):
                ok, errs = seed_demo_data(
                    {"dev": dev_schema, "uat": uat_schema, "prod": prod_schema},
                    owner=get_reviewer_email(),
                )
                if errs:
                    st.error("Some statements failed:")
                    for e in errs:
                        st.text(e)
                else:
                    st.success(
                        f"Seeded `{DEMO_TABLE}` in {dev_schema}, {uat_schema}, "
                        f"{prod_schema}."
                    )
                load_diff.clear()
                scan_schema.clear()
        st.divider()
        if st.button("Refresh diff", use_container_width=True):
            load_diff.clear()
            st.rerun()

    schemas = {"dev": dev_schema, "uat": uat_schema, "prod": prod_schema}

    tab_scan, tab_uat, tab_prod, tab_approvals, tab_mine, tab_history = st.tabs(
        [
            "Scan",
            "Request: DEV → UAT",
            "Request: UAT → PROD",
            "Approvals",
            "My requests",
            "History",
        ]
    )

    with tab_scan:
        render_scan()

    with tab_uat:
        render_diff_section(
            source_env="dev",
            target_env="uat",
            table_name=table_name,
            source_schema=schemas["dev"],
            target_schema=schemas["uat"],
        )

    with tab_prod:
        render_diff_section(
            source_env="uat",
            target_env="prod",
            table_name=table_name,
            source_schema=schemas["uat"],
            target_schema=schemas["prod"],
        )

    with tab_approvals:
        render_approvals()

    with tab_mine:
        render_my_requests()

    with tab_history:
        render_history()


if __name__ == "__main__":
    main()
