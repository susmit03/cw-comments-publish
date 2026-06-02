import pytest

from comments_core.apply import _escape_string, execute, generate_sql
from comments_core.models import CommentChange, TableRef


def test_escape_string_basic():
    assert _escape_string("hello") == "'hello'"


def test_escape_string_with_single_quote():
    assert _escape_string("it's") == "'it''s'"


def test_escape_string_with_backslash():
    assert _escape_string("a\\b") == "'a\\b'"


def test_escape_string_null():
    assert _escape_string(None) == "NULL"
    assert _escape_string("") == "NULL"


def test_generate_sql_table_comment():
    change = CommentChange(
        table=TableRef("sales", "orders"),
        kind="table",
        column=None,
        old_value=None,
        new_value="Customer orders",
    )
    sql = generate_sql([change], target_catalog="uat_main")
    assert sql == ["COMMENT ON TABLE `uat_main`.`sales`.`orders` IS 'Customer orders'"]


def test_generate_sql_column_comment():
    change = CommentChange(
        table=TableRef("sales", "orders"),
        kind="column",
        column="customer_id",
        old_value=None,
        new_value="FK to dim_customer.id",
    )
    sql = generate_sql([change], target_catalog="uat_main")
    assert sql == [
        "ALTER TABLE `uat_main`.`sales`.`orders` ALTER COLUMN `customer_id` "
        "COMMENT 'FK to dim_customer.id'"
    ]


def test_generate_sql_delete_comment_uses_null():
    change = CommentChange(
        table=TableRef("sales", "orders"),
        kind="table",
        column=None,
        old_value="old",
        new_value=None,
    )
    sql = generate_sql([change], target_catalog="prod_main")
    assert sql == ["COMMENT ON TABLE `prod_main`.`sales`.`orders` IS NULL"]


def test_execute_dry_run_does_not_call_client():
    sentinel_client = object()
    statements = [
        "COMMENT ON TABLE `uat`.`s`.`t` IS 'x'",
        "ALTER TABLE `uat`.`s`.`t` ALTER COLUMN `c` COMMENT 'y'",
    ]
    results = execute(sentinel_client, "wh-1", statements, dry_run=True)
    assert [r.status for r in results] == ["DRY_RUN", "DRY_RUN"]
    assert [r.sql for r in results] == statements


def test_execute_runs_and_collects_results(mocker):
    fake_client = mocker.Mock()
    mocker.patch("comments_core.apply.execute_statement", return_value=[])

    statements = ["COMMENT ON TABLE `uat`.`s`.`t` IS 'x'"]
    results = execute(fake_client, "wh-1", statements)

    assert len(results) == 1
    assert results[0].status == "OK"


def test_execute_collects_errors_and_continues(mocker):
    fake_client = mocker.Mock()
    call_count = {"n": 0}

    def fake_exec(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("boom")
        return []

    mocker.patch("comments_core.apply.execute_statement", side_effect=fake_exec)

    results = execute(
        fake_client,
        "wh-1",
        ["stmt-1", "stmt-2"],
        stop_on_error=False,
    )
    assert [r.status for r in results] == ["ERROR", "OK"]
    assert "boom" in (results[0].error or "")


def test_execute_stops_on_error_when_requested(mocker):
    fake_client = mocker.Mock()
    mocker.patch(
        "comments_core.apply.execute_statement", side_effect=RuntimeError("boom")
    )

    results = execute(
        fake_client,
        "wh-1",
        ["stmt-1", "stmt-2"],
        stop_on_error=True,
    )
    assert len(results) == 1
    assert results[0].status == "ERROR"


def test_unknown_kind_raises():
    with pytest.raises(ValueError):
        CommentChange(
            table=TableRef("s", "t"),
            kind="weird",
            column=None,
            old_value=None,
            new_value="x",
        )
