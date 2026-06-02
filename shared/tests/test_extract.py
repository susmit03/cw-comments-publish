from comments_core.extract import extract_comments
from comments_core.models import TableRef


def test_extract_combines_tables_and_columns(mocker):
    table_rows = [
        {"table_schema": "sales", "table_name": "orders", "comment": "Customer orders"},
        {"table_schema": "sales", "table_name": "customers", "comment": None},
        {
            "table_schema": "information_schema",
            "table_name": "tables",
            "comment": "system",
        },
    ]
    column_rows = [
        {
            "table_schema": "sales",
            "table_name": "orders",
            "column_name": "id",
            "comment": "PK",
        },
        {
            "table_schema": "sales",
            "table_name": "orders",
            "column_name": "amount",
            "comment": None,
        },
        {
            "table_schema": "sales",
            "table_name": "customers",
            "column_name": "id",
            "comment": "PK",
        },
    ]
    call_count = {"n": 0}

    def fake_exec(client, warehouse, sql, **kwargs):
        call_count["n"] += 1
        return table_rows if call_count["n"] == 1 else column_rows

    mocker.patch("comments_core.extract.execute_statement", side_effect=fake_exec)

    out = extract_comments(mocker.Mock(), "wh-1", "dev_main")
    assert out.catalog == "dev_main"
    assert TableRef("sales", "orders") in out.tables
    assert TableRef("sales", "customers") in out.tables
    assert TableRef("information_schema", "tables") not in out.tables
    orders = out.tables[TableRef("sales", "orders")]
    assert orders.table_comment == "Customer orders"
    assert orders.column_comments == {"id": "PK", "amount": None}


def test_extract_applies_schema_filter(mocker):
    table_rows = [
        {"table_schema": "sales", "table_name": "orders", "comment": None},
        {"table_schema": "marketing", "table_name": "campaigns", "comment": None},
    ]
    column_rows = []

    mocker.patch(
        "comments_core.extract.execute_statement",
        side_effect=[table_rows, column_rows],
    )

    out = extract_comments(
        mocker.Mock(),
        "wh-1",
        "dev_main",
        schema_filter=["sales"],
    )
    assert TableRef("sales", "orders") in out.tables
    assert TableRef("marketing", "campaigns") not in out.tables
