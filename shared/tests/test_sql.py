from types import SimpleNamespace

from comments_core._sql import execute_statement


def _statement_response(
    *,
    state: str = "SUCCEEDED",
    statement_id: str = "stmt-1",
    columns: list[str] | None = None,
    data_array=None,
    next_chunk_index=None,
):
    cols = [SimpleNamespace(name=c) for c in (columns or [])]
    manifest = SimpleNamespace(schema=SimpleNamespace(columns=cols)) if cols else None
    result = SimpleNamespace(
        data_array=data_array,
        next_chunk_index=next_chunk_index,
    )
    return SimpleNamespace(
        status=SimpleNamespace(state=state, error=None),
        statement_id=statement_id,
        manifest=manifest,
        result=result,
    )


def _result_chunk(*, data_array=None, next_chunk_index=None):
    return SimpleNamespace(data_array=data_array, next_chunk_index=next_chunk_index)


def test_execute_statement_collects_paginated_chunks(mocker):
    first = _statement_response(
        columns=["table_schema", "table_name"],
        data_array=[["sales", "orders"]],
        next_chunk_index=1,
    )
    second = _result_chunk(data_array=[["sales", "customers"]], next_chunk_index=None)

    client = mocker.Mock()
    client.statement_execution.execute_statement.return_value = first
    client.statement_execution.get_statement_result_chunk_n.return_value = second

    rows = execute_statement(client, "wh-1", "SELECT 1")
    assert rows == [
        {"table_schema": "sales", "table_name": "orders"},
        {"table_schema": "sales", "table_name": "customers"},
    ]
    client.statement_execution.get_statement_result_chunk_n.assert_called_once_with(
        "stmt-1", 1
    )


def test_execute_statement_returns_empty_for_ddl(mocker):
    ddl_resp = _statement_response(columns=[])
    client = mocker.Mock()
    client.statement_execution.execute_statement.return_value = ddl_resp
    rows = execute_statement(client, "wh-1", "ALTER TABLE foo")
    assert rows == []
