"""Seed schemas/tables/comments into pre-existing cwc_* catalogs.

Catalog creation is intentionally skipped: on metastores with Default Storage,
catalogs must be created via the UI (or with an explicit MANAGED LOCATION).
This runner executes every statement in seed_catalogs.sql EXCEPT the
``CREATE CATALOG`` statements, so it can populate catalogs you created by hand.

Env vars required:
  DATABRICKS_HOST, DATABRICKS_TOKEN, DATABRICKS_WAREHOUSE_ID
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))

from databricks.sdk import WorkspaceClient  # noqa: E402

from comments_core._sql import execute_statement  # noqa: E402


def _split_sql(sql_text: str) -> list[str]:
    # Split on semicolons that are NOT inside single-quoted string literals.
    parts: list[str] = []
    buf: list[str] = []
    in_str = False
    i = 0
    n = len(sql_text)
    while i < n:
        ch = sql_text[i]
        if ch == "'":
            # Handle escaped '' inside a string literal.
            if in_str and i + 1 < n and sql_text[i + 1] == "'":
                buf.append("''")
                i += 2
                continue
            in_str = not in_str
            buf.append(ch)
        elif ch == ";" and not in_str:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    if buf:
        parts.append("".join(buf))
    return parts


def _statements(sql_text: str) -> list[str]:
    # Strip line comments first, then split quote-aware, drop CREATE CATALOG.
    no_comments = "\n".join(
        line for line in sql_text.splitlines() if not line.strip().startswith("--")
    )
    out: list[str] = []
    for raw in _split_sql(no_comments):
        stmt = raw.strip()
        if not stmt:
            continue
        if re.match(r"(?i)^create\s+catalog", stmt):
            continue
        out.append(stmt)
    return out


def main() -> int:
    host = os.environ["DATABRICKS_HOST"]
    token = os.environ["DATABRICKS_TOKEN"]
    warehouse = os.environ["DATABRICKS_WAREHOUSE_ID"]

    sql_path = Path(__file__).with_name("seed_catalogs.sql")
    statements = _statements(sql_path.read_text())

    client = WorkspaceClient(host=host, token=token)
    print(f"Executing {len(statements)} statement(s) (CREATE CATALOG skipped)...")
    for i, stmt in enumerate(statements, 1):
        label = " ".join(stmt.split())[:80]
        execute_statement(client, warehouse, stmt)
        print(f"  [{i}/{len(statements)}] OK: {label}")
    print("Seed complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
