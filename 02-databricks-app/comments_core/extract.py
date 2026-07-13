"""Read table + column comments out of a catalog's information_schema."""

from __future__ import annotations

from typing import Iterable, Optional

from databricks.sdk import WorkspaceClient

from comments_core._sql import execute_statement
from comments_core.models import CommentSet, TableComments, TableRef


_SYSTEM_SCHEMAS = {"information_schema"}


def extract_comments(
    client: WorkspaceClient,
    warehouse_id: str,
    catalog: str,
    schema_filter: Optional[Iterable[str]] = None,
    table_filter: Optional[Iterable[str]] = None,
) -> CommentSet:
    """Return all table + column comments in `catalog`.

    Args:
        client: Authenticated WorkspaceClient
        warehouse_id: Serverless or pro SQL warehouse to run queries against
        catalog: Catalog name (e.g. "dev_main")
        schema_filter: If provided, only these schemas are extracted
        table_filter: If provided, only these table names are extracted
    """
    schema_set = set(schema_filter) if schema_filter else None
    table_set = set(table_filter) if table_filter else None

    tables_sql = (
        f"SELECT table_schema, table_name, comment "
        f"FROM `{catalog}`.information_schema.tables "
        f"WHERE table_type IN ('MANAGED', 'EXTERNAL', 'VIEW')"
    )
    columns_sql = (
        f"SELECT table_schema, table_name, column_name, comment "
        f"FROM `{catalog}`.information_schema.columns "
        f"ORDER BY table_schema, table_name, ordinal_position"
    )

    table_rows = execute_statement(client, warehouse_id, tables_sql)
    column_rows = execute_statement(client, warehouse_id, columns_sql)

    out = CommentSet(catalog=catalog)
    for row in table_rows:
        schema = row["table_schema"]
        table = row["table_name"]
        if schema in _SYSTEM_SCHEMAS:
            continue
        if schema_set is not None and schema not in schema_set:
            continue
        if table_set is not None and table not in table_set:
            continue
        ref = TableRef(schema=schema, table=table)
        out.tables[ref] = TableComments(table_comment=row.get("comment"))

    for row in column_rows:
        schema = row["table_schema"]
        table = row["table_name"]
        if schema in _SYSTEM_SCHEMAS:
            continue
        ref = TableRef(schema=schema, table=table)
        if ref not in out.tables:
            continue
        out.tables[ref].column_comments[row["column_name"]] = row.get("comment")

    return out
