# comments-core

Shared library used by all four POCs in this repo. Provides four primitives:

- `extract.extract_comments(...)` — read table + column comments from a Unity Catalog catalog via `information_schema`
- `diff.compute_changes(source, target)` — produce a list of `CommentChange` records describing what needs to change in `target` to match `source`
- `apply.generate_sql(changes, target_catalog)` — render those changes as `COMMENT ON TABLE` / `ALTER TABLE ... ALTER COLUMN` statements
- `apply.execute(client, warehouse_id, statements)` — run the statements against a SQL warehouse

The library has zero workflow opinions. Approval lives in the four sibling folders.

## Install (editable)

```bash
cd shared
pip install -e ".[dev]"
```

## Run tests

```bash
cd shared
pytest
```

## Authentication

All public functions take a `databricks.sdk.WorkspaceClient`. Auth is whatever
the SDK supports — env vars (`DATABRICKS_HOST`, `DATABRICKS_TOKEN`), a profile
in `~/.databrickscfg`, or unified auth from inside a Databricks runtime. The
shared library never reads credentials directly.
