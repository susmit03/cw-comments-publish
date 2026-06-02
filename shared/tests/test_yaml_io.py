from pathlib import Path

from comments_core.models import CommentSet, TableComments, TableRef
from comments_core.yaml_io import read_comment_set, write_comment_set


def _make_set() -> CommentSet:
    cs = CommentSet(catalog="dev_main")
    cs.tables[TableRef("sales", "orders")] = TableComments(
        table_comment="Customer orders",
        column_comments={"id": "PK", "amount": "USD"},
    )
    cs.tables[TableRef("sales", "customers")] = TableComments(
        table_comment=None,
        column_comments={"id": "PK"},
    )
    return cs


def test_write_creates_one_file_per_table(tmp_path: Path):
    written = write_comment_set(_make_set(), tmp_path / "comments")
    relative = sorted(str(p.relative_to(tmp_path / "comments")) for p in written)
    assert relative == ["sales/customers.yaml", "sales/orders.yaml"]


def test_round_trip_preserves_comments(tmp_path: Path):
    original = _make_set()
    write_comment_set(original, tmp_path / "comments")
    loaded = read_comment_set(tmp_path / "comments", catalog="dev_main")
    assert loaded.catalog == "dev_main"
    assert set(loaded.tables.keys()) == set(original.tables.keys())
    for ref, comments in original.tables.items():
        assert loaded.tables[ref].table_comment == comments.table_comment
        assert loaded.tables[ref].column_comments == comments.column_comments


def test_read_returns_empty_when_root_missing(tmp_path: Path):
    out = read_comment_set(tmp_path / "does-not-exist", catalog="x")
    assert out.catalog == "x"
    assert out.tables == {}


def test_yaml_files_are_human_readable(tmp_path: Path):
    write_comment_set(_make_set(), tmp_path / "comments")
    text = (tmp_path / "comments" / "sales" / "orders.yaml").read_text()
    assert "schema: sales" in text
    assert "table: orders" in text
    assert "table_comment: Customer orders" in text
    assert "id: PK" in text
