"""Comment Promotion — Databricks App (Streamlit).

Lets a reviewer:
  1. Pick a target environment (UAT or PROD).
  2. See the diff between the source catalog and that target.
  3. Approve & apply with one click.
  4. View an audit log of every prior promotion.

The reviewer's identity comes from the `X-Forwarded-Email` header that the
Databricks Apps proxy adds. SQL runs under the App's service principal token,
which the SDK picks up automatically from env vars when running inside the
Apps runtime.
"""

from __future__ import annotations

import datetime as dt
import os
from typing import Optional

import pandas as pd
import streamlit as st
from databricks.sdk import WorkspaceClient

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


@st.cache_resource
def get_client() -> WorkspaceClient:
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
# Diff loading (cached for 60 seconds)
# ----------------------------------------------------------------------------


@st.cache_data(ttl=60, show_spinner=False)
def load_diff(source_env: str, target_env: str) -> list[dict]:
    """Return a JSON-serializable list of changes so Streamlit can cache it."""
    cfg = get_config()
    client = get_client()
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
    return [
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
    ]


# ----------------------------------------------------------------------------
# Promotion handler
# ----------------------------------------------------------------------------


def promote(source_env: str, target_env: str) -> None:
    """Apply the current diff to the target catalog and write audit rows."""
    from comments_core.models import CommentChange, TableRef

    cfg = get_config()
    client = get_client()
    source_catalog = cfg.catalog_for(source_env)
    target_catalog = cfg.catalog_for(target_env)
    reviewer = get_reviewer_email()
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    payload = load_diff(source_env, target_env)
    if not payload:
        st.info("Nothing to promote.")
        return

    changes = [
        CommentChange(
            table=TableRef(schema=row["schema"], table=row["table"]),
            kind=row["kind"],
            column=row["column"],
            old_value=row["old_value"],
            new_value=row["new_value"],
        )
        for row in payload
    ]
    statements = generate_sql(changes, target_catalog)

    with st.spinner(f"Applying {len(statements)} statement(s) to {target_catalog} ..."):
        results = execute(client, cfg.warehouse_id, statements)

    audit_rows = []
    for change, stmt, result in zip(changes, statements, results):
        audit_rows.append(
            {
                "promoted_at": now,
                "promoted_by": reviewer,
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
    failed = [r for r in results if r.status == "ERROR"]
    if not failed:
        st.success(f"Applied {ok}/{len(results)} statements to {target_catalog}.")
    else:
        st.error(f"{len(failed)} statement(s) failed. See details below.")
        for r in failed:
            st.code(r.sql, language="sql")
            st.text(r.error)

    load_diff.clear()


# ----------------------------------------------------------------------------
# UI
# ----------------------------------------------------------------------------


def render_diff_section(source_env: str, target_env: str) -> None:
    cfg = get_config()
    target_catalog = cfg.catalog_for(target_env)
    source_catalog = cfg.catalog_for(source_env)

    st.markdown(
        f"### {source_env.upper()} ({source_catalog}) → {target_env.upper()} ({target_catalog})"
    )

    try:
        changes = load_diff(source_env, target_env)
    except Exception as exc:
        st.error(f"Could not load diff: {exc}")
        return

    counts = {"add": 0, "edit": 0, "delete": 0}
    for c in changes:
        counts[c["change_type"]] = counts.get(c["change_type"], 0) + 1

    cols = st.columns(4)
    cols[0].metric("Total changes", len(changes))
    cols[1].metric("Adds", counts["add"])
    cols[2].metric("Edits", counts["edit"])
    cols[3].metric("Deletes", counts["delete"])

    if not changes:
        st.success(f"{target_catalog} is up to date with {source_catalog}.")
        return

    df = pd.DataFrame(changes)
    grouped = df.groupby(["schema", "table"])

    for (schema, table), group in grouped:
        with st.expander(
            f"`{schema}.{table}` — {len(group)} change(s)", expanded=False
        ):
            display = group[["kind", "column", "change_type", "old_value", "new_value"]]
            display = display.rename(
                columns={
                    "kind": "Kind",
                    "column": "Column",
                    "change_type": "Change",
                    "old_value": f"Current ({target_env})",
                    "new_value": f"Desired ({source_env})",
                }
            )
            st.dataframe(display, hide_index=True, use_container_width=True)

    confirm_phrase = f"PROMOTE {target_env.upper()}"
    typed = st.text_input(
        f"Type **{confirm_phrase}** to enable the Promote button",
        key=f"confirm-{target_env}",
        placeholder=confirm_phrase,
    )
    promote_disabled = typed.strip() != confirm_phrase

    if st.button(
        f"Approve & promote to {target_catalog}",
        type="primary",
        disabled=promote_disabled,
        key=f"promote-{target_env}",
    ):
        promote(source_env, target_env)


def render_history() -> None:
    st.markdown("### Recent promotions")
    df = read_audit_rows(limit=200)
    if df.empty:
        st.info("No promotions recorded yet.")
        return
    st.dataframe(df, hide_index=True, use_container_width=True)


def main() -> None:
    st.title("Comment promotion")
    st.caption(
        "Promote Unity Catalog table + column comments from DEV through UAT to PROD."
    )

    audit_warn = ensure_audit_table()
    if audit_warn:
        st.warning(audit_warn)

    cfg = get_config()
    with st.sidebar:
        st.subheader("Reviewer")
        st.markdown(f"**{get_reviewer_email()}**")
        st.divider()
        st.subheader("Catalogs")
        st.code(
            f"DEV  = {cfg.dev_catalog}\nUAT  = {cfg.uat_catalog}\nPROD = {cfg.prod_catalog}\n"
            f"Warehouse = {cfg.warehouse_id}",
            language="text",
        )
        st.divider()
        if st.button("Refresh diff", use_container_width=True):
            load_diff.clear()
            st.rerun()

    tab_uat, tab_prod, tab_history = st.tabs(
        ["Promote to UAT", "Promote to PROD", "History"]
    )

    with tab_uat:
        render_diff_section(source_env="dev", target_env="uat")

    with tab_prod:
        render_diff_section(source_env="uat", target_env="prod")

    with tab_history:
        render_history()


if __name__ == "__main__":
    main()
