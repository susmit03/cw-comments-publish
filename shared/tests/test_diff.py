from comments_core.diff import compute_changes
from comments_core.models import CommentSet, TableComments, TableRef


def _set(catalog: str, rows: dict[tuple[str, str], dict]) -> CommentSet:
    cs = CommentSet(catalog=catalog)
    for (schema, table), spec in rows.items():
        cs.tables[TableRef(schema, table)] = TableComments(
            table_comment=spec.get("table"),
            column_comments=dict(spec.get("columns") or {}),
        )
    return cs


def test_no_changes_when_identical():
    src = _set("dev", {("sales", "orders"): {"table": "Customer orders", "columns": {"id": "PK"}}})
    tgt = _set("uat", {("sales", "orders"): {"table": "Customer orders", "columns": {"id": "PK"}}})
    assert compute_changes(src, tgt) == []


def test_detects_table_comment_edit():
    src = _set("dev", {("sales", "orders"): {"table": "New description", "columns": {"id": "PK"}}})
    tgt = _set("uat", {("sales", "orders"): {"table": "Old description", "columns": {"id": "PK"}}})
    changes = compute_changes(src, tgt)
    assert len(changes) == 1
    assert changes[0].kind == "table"
    assert changes[0].column is None
    assert changes[0].old_value == "Old description"
    assert changes[0].new_value == "New description"
    assert changes[0].change_type == "edit"


def test_detects_table_comment_add():
    src = _set("dev", {("sales", "orders"): {"table": "Brand new", "columns": {}}})
    tgt = _set("uat", {("sales", "orders"): {"table": None, "columns": {}}})
    changes = compute_changes(src, tgt)
    assert len(changes) == 1
    assert changes[0].change_type == "add"


def test_detects_column_comment_edit():
    src = _set("dev", {("sales", "orders"): {"columns": {"id": "PK monotonic"}}})
    tgt = _set("uat", {("sales", "orders"): {"columns": {"id": "PK"}}})
    changes = compute_changes(src, tgt)
    assert len(changes) == 1
    assert changes[0].kind == "column"
    assert changes[0].column == "id"
    assert changes[0].new_value == "PK monotonic"


def test_skips_columns_not_in_target():
    src = _set("dev", {("sales", "orders"): {"columns": {"id": "PK", "extra": "new col"}}})
    tgt = _set("uat", {("sales", "orders"): {"columns": {"id": "PK"}}})
    assert compute_changes(src, tgt) == []


def test_skips_tables_not_in_target():
    src = _set("dev", {("sales", "orders"): {"table": "x", "columns": {}}})
    tgt = _set("uat", {})
    assert compute_changes(src, tgt) == []


def test_treats_none_and_empty_as_equivalent():
    src = _set("dev", {("sales", "orders"): {"table": "", "columns": {"id": None}}})
    tgt = _set("uat", {("sales", "orders"): {"table": None, "columns": {"id": ""}}})
    assert compute_changes(src, tgt) == []


def test_handles_multiple_tables_and_columns():
    src = _set(
        "dev",
        {
            ("sales", "orders"): {"table": "Orders", "columns": {"id": "PK", "amount": "USD"}},
            ("sales", "customers"): {"table": "Customers", "columns": {"id": "PK"}},
        },
    )
    tgt = _set(
        "uat",
        {
            ("sales", "orders"): {"table": "Old orders", "columns": {"id": "PK", "amount": None}},
            ("sales", "customers"): {"table": "Customers", "columns": {"id": "PK"}},
        },
    )
    changes = compute_changes(src, tgt)
    assert len(changes) == 2
    kinds = {(c.kind, c.column) for c in changes}
    assert ("table", None) in kinds
    assert ("column", "amount") in kinds
