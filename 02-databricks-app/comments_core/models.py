"""Dataclasses used across extract, diff, and apply."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class TableRef:
    """A schema-qualified reference that is catalog-agnostic.

    The same `TableRef` identifies the dev, uat, and prod copies of a table.
    """

    schema: str
    table: str

    def fqn(self, catalog: str) -> str:
        return f"`{catalog}`.`{self.schema}`.`{self.table}`"

    def relative_path(self) -> str:
        return f"{self.schema}/{self.table}"


@dataclass
class TableComments:
    """All comments attached to one table."""

    table_comment: Optional[str] = None
    column_comments: dict[str, Optional[str]] = field(default_factory=dict)


@dataclass
class CommentSet:
    """All comments extracted from one catalog."""

    catalog: str
    tables: dict[TableRef, TableComments] = field(default_factory=dict)


@dataclass
class CommentChange:
    """A single delta describing one comment that needs to change in the target.

    `kind` is `"table"` for table-level comments and `"column"` for column-level.
    """

    table: TableRef
    kind: str
    column: Optional[str]
    old_value: Optional[str]
    new_value: Optional[str]

    def __post_init__(self) -> None:
        if self.kind not in ("table", "column"):
            raise ValueError(f"kind must be 'table' or 'column', got {self.kind!r}")
        if self.kind == "column" and not self.column:
            raise ValueError("column must be set when kind == 'column'")
        if self.kind == "table" and self.column is not None:
            raise ValueError("column must be None when kind == 'table'")

    @property
    def change_type(self) -> str:
        """One of 'add', 'edit', 'delete' — useful for UI rendering."""
        empty_old = not (self.old_value or "").strip()
        empty_new = not (self.new_value or "").strip()
        if empty_old and not empty_new:
            return "add"
        if not empty_old and empty_new:
            return "delete"
        return "edit"

    def describe(self) -> str:
        target = f"{self.table.relative_path()}"
        if self.kind == "column":
            target = f"{target}.{self.column}"
        return f"[{self.change_type:6s}] {target}"


@dataclass
class PromotionRequest:
    """A bundle of changes proposed for promotion to a specific target catalog."""

    target_catalog: str
    changes: list[CommentChange] = field(default_factory=list)
    requested_by: Optional[str] = None
    note: Optional[str] = None

    def summary(self) -> dict[str, int]:
        out = {"add": 0, "edit": 0, "delete": 0, "total": 0}
        for c in self.changes:
            out[c.change_type] += 1
            out["total"] += 1
        return out
