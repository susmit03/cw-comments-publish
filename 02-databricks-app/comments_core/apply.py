"""Generate and execute SQL to apply CommentChange records onto a target catalog."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from databricks.sdk import WorkspaceClient

from comments_core._sql import execute_statement
from comments_core.models import CommentChange


def _escape_string(value: str | None) -> str:
    """Return a SQL string literal or the keyword NULL."""
    if value is None or value == "":
        return "NULL"
    # Spark SQL string literals require single-quote escaping. Backslashes are
    # preserved as-is so path-like comments are not mutated during promotion.
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


def generate_sql(changes: list[CommentChange], target_catalog: str) -> list[str]:
    """Render each CommentChange as a single DDL statement.

    Table-level: COMMENT ON TABLE `cat`.`schema`.`table` IS '...'
    Column-level: ALTER TABLE `cat`.`schema`.`table` ALTER COLUMN `col` COMMENT '...'
    """
    statements: list[str] = []
    for change in changes:
        fqn = change.table.fqn(target_catalog)
        literal = _escape_string(change.new_value)
        if change.kind == "table":
            statements.append(f"COMMENT ON TABLE {fqn} IS {literal}")
        elif change.kind == "column":
            statements.append(
                f"ALTER TABLE {fqn} ALTER COLUMN `{change.column}` COMMENT {literal}"
            )
        else:
            raise ValueError(f"Unknown change kind: {change.kind!r}")
    return statements


@dataclass
class ApplyResult:
    sql: str
    status: str
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out = {"sql": self.sql, "status": self.status}
        if self.error:
            out["error"] = self.error
        return out


def execute(
    client: WorkspaceClient,
    warehouse_id: str,
    statements: list[str],
    dry_run: bool = False,
    stop_on_error: bool = False,
) -> list[ApplyResult]:
    """Run each statement against `warehouse_id`. Continue past errors by default."""
    results: list[ApplyResult] = []
    for stmt in statements:
        if dry_run:
            results.append(ApplyResult(sql=stmt, status="DRY_RUN"))
            continue
        try:
            execute_statement(client, warehouse_id, stmt)
            results.append(ApplyResult(sql=stmt, status="OK"))
        except Exception as exc:
            results.append(ApplyResult(sql=stmt, status="ERROR", error=str(exc)))
            if stop_on_error:
                break
    return results
