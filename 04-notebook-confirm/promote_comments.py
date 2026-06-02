# Databricks notebook source
# MAGIC %md
# MAGIC # Comment Promotion — Notebook (Approach D)
# MAGIC
# MAGIC Promote table + column comments between two Unity Catalog catalogs.
# MAGIC The minimal POC: set widgets, run, review the diff, set `confirm=I_APPROVE`, run again.
# MAGIC
# MAGIC **Widgets**
# MAGIC - `source_catalog` — catalog with the source-of-truth comments (e.g. `cwc_dev`)
# MAGIC - `target_catalog` — catalog to update (e.g. `cwc_uat` or `cwc_prod`)
# MAGIC - `schema_filter` — optional comma-separated list (blank = all schemas)
# MAGIC - `table_filter` — optional comma-separated list (blank = all tables)
# MAGIC - `confirm` — `DRY_RUN` (preview only) or `I_APPROVE` (actually run)

# COMMAND ----------

# MAGIC %pip install -q "databricks-sdk>=0.30.0" pyyaml

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

# MAGIC %md
# MAGIC ### Make the shared library importable
# MAGIC The repo lives at `/Workspace/Repos/<you>/cw-comments-publish/`. We add
# MAGIC the `shared/` directory to `sys.path` so we can `import comments_core`.

# COMMAND ----------

import os
import sys

DEFAULT_REPO_ROOT = "/Workspace/Repos"
shared_candidate = None
for root in [DEFAULT_REPO_ROOT, "/Workspace"]:
    for entry in os.listdir(root) if os.path.isdir(root) else []:
        candidate = os.path.join(root, entry, "cw-comments-publish", "shared")
        if os.path.isdir(candidate):
            shared_candidate = candidate
            break
    if shared_candidate:
        break

if not shared_candidate:
    shared_candidate = "../shared"

if shared_candidate not in sys.path:
    sys.path.insert(0, shared_candidate)

print(f"Using shared library at: {shared_candidate}")

# COMMAND ----------

# MAGIC %md
# MAGIC ### Widgets

# COMMAND ----------

dbutils.widgets.text("source_catalog", "cwc_dev", "Source catalog")
dbutils.widgets.text("target_catalog", "cwc_uat", "Target catalog")
dbutils.widgets.text("schema_filter", "", "Schema filter (comma-separated)")
dbutils.widgets.text("table_filter", "", "Table filter (comma-separated)")
dbutils.widgets.dropdown(
    "confirm",
    "DRY_RUN",
    ["DRY_RUN", "I_APPROVE"],
    "Confirm (must be I_APPROVE to actually run)",
)
dbutils.widgets.text("warehouse_id", "", "SQL warehouse ID")

source_catalog = dbutils.widgets.get("source_catalog").strip()
target_catalog = dbutils.widgets.get("target_catalog").strip()
schema_filter_raw = dbutils.widgets.get("schema_filter").strip()
table_filter_raw = dbutils.widgets.get("table_filter").strip()
confirm = dbutils.widgets.get("confirm").strip().upper()
warehouse_id = dbutils.widgets.get("warehouse_id").strip() or os.environ.get(
    "DATABRICKS_WAREHOUSE_ID", ""
)

if not source_catalog or not target_catalog:
    raise ValueError("source_catalog and target_catalog are required")
if source_catalog == target_catalog:
    raise ValueError("source_catalog and target_catalog must differ")
if not warehouse_id:
    raise ValueError(
        "warehouse_id widget or DATABRICKS_WAREHOUSE_ID env var must be set"
    )

schema_filter = [s.strip() for s in schema_filter_raw.split(",") if s.strip()] or None
table_filter = [t.strip() for t in table_filter_raw.split(",") if t.strip()] or None

print(f"Source : {source_catalog}")
print(f"Target : {target_catalog}")
print(f"Schemas: {schema_filter or 'all'}")
print(f"Tables : {table_filter or 'all'}")
print(f"Confirm: {confirm}")

# COMMAND ----------

# MAGIC %md
# MAGIC ### Extract + diff

# COMMAND ----------

from databricks.sdk import WorkspaceClient

from comments_core import (
    compute_changes,
    execute,
    extract_comments,
    generate_sql,
)

client = WorkspaceClient()

source_set = extract_comments(
    client,
    warehouse_id,
    source_catalog,
    schema_filter=schema_filter,
    table_filter=table_filter,
)
target_set = extract_comments(
    client,
    warehouse_id,
    target_catalog,
    schema_filter=schema_filter,
    table_filter=table_filter,
)
changes = compute_changes(source_set, target_set)
print(f"{len(changes)} change(s) detected.")

# COMMAND ----------

# MAGIC %md
# MAGIC ### Render the diff

# COMMAND ----------

if not changes:
    print("Target is already in sync with source. Nothing to do.")
else:
    import pandas as pd

    rows = [
        {
            "schema": c.table.schema,
            "table": c.table.table,
            "kind": c.kind,
            "column": c.column,
            "change_type": c.change_type,
            "current_value": c.old_value,
            "desired_value": c.new_value,
        }
        for c in changes
    ]
    display(pd.DataFrame(rows))  # noqa: F821 - Databricks-provided

# COMMAND ----------

# MAGIC %md
# MAGIC ### Render the SQL that would run

# COMMAND ----------

statements = generate_sql(changes, target_catalog)
for stmt in statements:
    print(stmt)

# COMMAND ----------

# MAGIC %md
# MAGIC ### Apply
# MAGIC If `confirm` is `I_APPROVE`, run the statements. Otherwise this cell
# MAGIC is a no-op so you can review the SQL above before committing.

# COMMAND ----------

if confirm != "I_APPROVE":
    print("Dry run — set the `confirm` widget to I_APPROVE and re-run to apply.")
elif not statements:
    print("Nothing to apply.")
else:
    results = execute(client, warehouse_id, statements)
    ok = sum(1 for r in results if r.status == "OK")
    failed = [r for r in results if r.status == "ERROR"]
    print(f"{ok}/{len(results)} statement(s) applied OK.")
    for r in failed:
        print(f"  FAIL: {r.sql}")
        print(f"        {r.error}")
    if failed:
        raise RuntimeError(f"{len(failed)} statement(s) failed.")

# COMMAND ----------

# MAGIC %md
# MAGIC ### Done
# MAGIC Re-run this notebook against the next environment (e.g. switch
# MAGIC `target_catalog` from `cwc_uat` to `cwc_prod`) to continue the
# MAGIC promotion chain.
