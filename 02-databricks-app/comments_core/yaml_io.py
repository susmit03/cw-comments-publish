"""Round-trip a CommentSet to/from a directory of YAML files.

Used by the Git PR POC: comments live in `comments/<schema>/<table>.yaml` so a
PR diff is a human-readable diff. Other POCs may use it for snapshots / audit.

File shape:

    schema: sales
    table: orders
    table_comment: "Customer orders, one row per order_id"
    columns:
      order_id: "Primary key, monotonically increasing"
      customer_id: "FK to dim_customer.id"
      total_amount: null
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

import yaml

from comments_core.models import CommentSet, TableComments, TableRef


def write_comment_set(comment_set: CommentSet, root: str | Path) -> list[Path]:
    """Write each TableRef as its own YAML under `root/<schema>/<table>.yaml`.

    Returns the list of files written. Does NOT clear `root` first; callers
    who want to drop deletions should clean the directory themselves.
    """
    root_path = Path(root)
    root_path.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for ref, comments in sorted(
        comment_set.tables.items(), key=lambda kv: (kv[0].schema, kv[0].table)
    ):
        schema_dir = root_path / ref.schema
        schema_dir.mkdir(parents=True, exist_ok=True)
        target = schema_dir / f"{ref.table}.yaml"
        payload = {
            "schema": ref.schema,
            "table": ref.table,
            "table_comment": comments.table_comment,
            "columns": dict(sorted(comments.column_comments.items())),
        }
        target.write_text(
            yaml.safe_dump(payload, sort_keys=False, allow_unicode=True, width=120)
        )
        written.append(target)
    return written


def read_comment_set(root: str | Path, catalog: str) -> CommentSet:
    """Inverse of write_comment_set."""
    root_path = Path(root)
    out = CommentSet(catalog=catalog)
    if not root_path.exists():
        return out
    for path in sorted(root_path.glob("*/*.yaml")):
        data = yaml.safe_load(path.read_text()) or {}
        schema = data.get("schema") or path.parent.name
        table = data.get("table") or path.stem
        ref = TableRef(schema=schema, table=table)
        out.tables[ref] = TableComments(
            table_comment=data.get("table_comment"),
            column_comments=dict(data.get("columns") or {}),
        )
    return out


def discover_yaml_files(root: str | Path) -> Iterable[Path]:
    return sorted(Path(root).glob("*/*.yaml"))
