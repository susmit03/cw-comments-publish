"""Compute the minimal set of changes needed in `target` to match `source`."""

from __future__ import annotations

from comments_core.models import CommentChange, CommentSet


def _normalize(value: str | None) -> str:
    """Treat None and '' the same so we don't emit no-op statements."""
    return (value or "").strip()


def compute_changes(source: CommentSet, target: CommentSet) -> list[CommentChange]:
    """Yield one CommentChange per (table or column) where comments differ.

    Tables that exist in `source` but not in `target` are skipped — we only
    promote comments onto pre-existing tables. Tables that exist in `target`
    but not in `source` are also skipped (the source of truth is `source`,
    but we don't blow away target-only tables).
    """
    changes: list[CommentChange] = []

    for ref, src in source.tables.items():
        if ref not in target.tables:
            continue
        tgt = target.tables[ref]

        if _normalize(src.table_comment) != _normalize(tgt.table_comment):
            changes.append(
                CommentChange(
                    table=ref,
                    kind="table",
                    column=None,
                    old_value=tgt.table_comment,
                    new_value=src.table_comment,
                )
            )

        all_columns = set(src.column_comments) | set(tgt.column_comments)
        for col in sorted(all_columns):
            src_comment = src.column_comments.get(col)
            tgt_comment = tgt.column_comments.get(col)
            if col not in src.column_comments:
                continue
            if col not in tgt.column_comments:
                continue
            if _normalize(src_comment) == _normalize(tgt_comment):
                continue
            changes.append(
                CommentChange(
                    table=ref,
                    kind="column",
                    column=col,
                    old_value=tgt_comment,
                    new_value=src_comment,
                )
            )

    return changes
