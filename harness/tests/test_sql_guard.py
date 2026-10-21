"""The AST guard — the safety core.

The red-team cases here are the contract: none of them may ever reach a
database. The accuracy cases matter just as much: ordinary analyst SQL must
not be blocked, because a guard that cries wolf gets turned off.
"""

from __future__ import annotations

import pytest

from tsql.safety.policy import Policy
from tsql.safety.sql_guard import SqlGuard, validate_sql


@pytest.fixture
def policy() -> Policy:
    return Policy(
        dialect="sqlite",
        allowed_tables=["customers", "orders", "products", "order_items", "v_revenue_by_region"],
        max_rows=1000,
        expensive_row_threshold=1_000_000,
    )


@pytest.fixture
def guard(policy: Policy) -> SqlGuard:
    return SqlGuard(policy)


# --------------------------------------------------------------- happy path
def test_simple_select_passes(guard):
    result = guard.validate("SELECT id, name FROM customers")
    assert result.ok
    assert result.tables == ["customers"]
    assert result.errors == []


def test_join_passes(guard):
    result = guard.validate(
        "SELECT c.region, SUM(o.amount) AS revenue "
        "FROM orders o JOIN customers c ON c.id = o.customer_id "
        "GROUP BY c.region"
    )
    assert result.ok
    assert set(result.tables) == {"orders", "customers"}


def test_aggregate_passes(guard):
    result = guard.validate("SELECT COUNT(*) AS n FROM orders")
    assert result.ok, result.errors


def test_cte_is_not_mistaken_for_a_table(guard):
    result = guard.validate(
        "WITH totals AS (SELECT customer_id, SUM(amount) AS t FROM orders GROUP BY customer_id) "
        "SELECT customer_id, t FROM totals"
    )
    assert result.ok, result.errors
    assert result.tables == ["orders"]


def test_subquery_passes(guard):
    result = guard.validate(
        "SELECT id FROM customers WHERE id IN (SELECT customer_id FROM orders WHERE amount > 100)"
    )
    assert result.ok, result.errors


def test_union_passes(guard):
    result = guard.validate(
        "SELECT id FROM orders UNION ALL SELECT id FROM order_items"
    )
    assert result.ok, result.errors


def test_view_passes(guard):
    result = guard.validate("SELECT region, revenue FROM v_revenue_by_region")
    assert result.ok, result.errors


def test_limit_is_preserved(guard):
    result = guard.validate("SELECT id FROM orders LIMIT 10")
    assert result.ok
    assert result.limit_applied == 10
    assert "LIMIT 10" in result.sql


def test_missing_limit_is_injected(guard):
    result = guard.validate("SELECT id FROM orders")
    assert result.ok
    assert result.limit_applied == 1000
    assert "LIMIT" in result.sql
    assert any("LIMIT" in w for w in result.warnings)


def test_oversized_limit_is_capped(guard):
    result = guard.validate("SELECT id FROM orders LIMIT 999999")
    assert result.ok
    assert result.limit_applied == 1000
    assert any("reduced" in w for w in result.warnings)


def test_explicit_columns_are_collected(guard):
    result = guard.validate("SELECT name, region FROM customers")
    assert "name" in result.columns
    assert "region" in result.columns


# ----------------------------------------------------------------- red team
def test_drop_table_is_blocked(guard):
    result = guard.validate("DROP TABLE orders")
    assert not result.ok
    assert result.denied_by in ("not_a_query", "write_operation")


def test_delete_is_blocked(guard):
    result = guard.validate("DELETE FROM orders")
    assert not result.ok


def test_delete_without_where_is_blocked(guard):
    result = guard.validate("DELETE FROM orders WHERE 1=1")
    assert not result.ok


def test_update_is_blocked(guard):
    result = guard.validate("UPDATE orders SET amount = 0")
    assert not result.ok


def test_insert_is_blocked(guard):
    result = guard.validate("INSERT INTO orders (id) VALUES (1)")
    assert not result.ok


def test_truncate_is_blocked(guard):
    result = guard.validate("TRUNCATE TABLE orders")
    assert not result.ok


def test_alter_is_blocked(guard):
    result = guard.validate("ALTER TABLE orders ADD COLUMN x INT")
    assert not result.ok


def test_create_is_blocked(guard):
    result = guard.validate("CREATE TABLE t (id INT)")
    assert not result.ok


def test_multi_statement_is_blocked(guard):
    result = guard.validate("SELECT id FROM orders; DROP TABLE orders")
    assert not result.ok
    assert result.denied_by == "multiple_statements"


def test_write_hidden_in_a_cte_is_blocked(guard):
    result = guard.validate(
        "WITH x AS (DELETE FROM orders RETURNING *) SELECT * FROM x"
    )
    assert not result.ok


def test_unknown_table_is_blocked(guard):
    result = guard.validate("SELECT * FROM salary_table")
    assert not result.ok
    assert result.denied_by == "table_not_allowed"
    assert "salary_table" in result.errors[0]


def test_error_message_names_the_allowed_tables(guard):
    result = guard.validate("SELECT 1 FROM nope")
    assert "customers" in result.errors[0]


def test_system_schema_is_blocked(guard):
    result = guard.validate("SELECT * FROM information_schema.tables")
    assert not result.ok


def test_pg_catalog_is_blocked(guard):
    result = guard.validate("SELECT usename FROM pg_catalog.pg_user")
    assert not result.ok


def test_select_star_is_blocked(guard):
    result = guard.validate("SELECT * FROM customers")
    assert not result.ok
    assert result.denied_by == "select_star"


def test_aliased_select_star_is_blocked(guard):
    result = guard.validate("SELECT c.* FROM customers c")
    assert not result.ok


def test_count_star_is_allowed(guard):
    result = guard.validate("SELECT COUNT(*) FROM customers")
    assert result.ok


def test_dangerous_function_is_blocked(guard):
    result = guard.validate("SELECT pg_read_file('/etc/passwd')")
    assert not result.ok
    assert result.denied_by == "dangerous_function"


def test_read_csv_table_function_is_blocked(guard):
    result = guard.validate("SELECT * FROM read_csv('/etc/passwd')")
    assert not result.ok


def test_dblink_is_blocked(guard):
    result = guard.validate("SELECT * FROM dblink('host=evil','SELECT 1') AS t(x int)")
    assert not result.ok


def test_load_extension_is_blocked(guard):
    result = guard.validate("SELECT load_extension('evil')")
    assert not result.ok


def test_known_dangerous_function_in_scalar_position_is_blocked(guard):
    # read_csv parses as a known function class, not an anonymous one.
    result = guard.validate("SELECT read_csv('/etc/passwd') AS contents")
    assert not result.ok
    assert result.denied_by == "dangerous_function"


def test_schema_qualified_dangerous_function_is_blocked(guard):
    result = guard.validate("SELECT pg_catalog.pg_read_file('/etc/passwd')")
    assert not result.ok


def test_function_names_are_matched_case_and_underscore_insensitively(guard):
    result = guard.validate("SELECT READ_CSV('/etc/passwd')")
    assert not result.ok


def test_ordinary_functions_are_not_blocked(guard):
    result = guard.validate(
        "SELECT COUNT(*) AS n, ROUND(AVG(amount), 2) AS avg, UPPER(region) AS r FROM orders"
    )
    assert result.ok, result.errors


def test_copy_to_program_is_blocked(guard):
    result = guard.validate("COPY orders TO PROGRAM 'sh -c evil'")
    assert not result.ok


def test_unparseable_sql_is_denied_not_allowed(guard):
    result = guard.validate("SELECT FROM WHERE ((")
    assert not result.ok
    assert result.denied_by == "parse_error"


def test_empty_sql_is_denied(guard):
    result = guard.validate("   ")
    assert not result.ok


def test_comment_only_sql_is_denied(guard):
    result = guard.validate("-- just a comment")
    assert not result.ok


# ----------------------------------------------------------------- obfuscation
def test_mixed_case_is_still_valid_sql(guard):
    result = guard.validate("SeLeCt Id FrOm orders")
    assert result.ok


def test_keyword_in_a_string_literal_is_data_not_code(guard):
    result = guard.validate("SELECT 'DROP TABLE orders' AS note FROM customers")
    assert result.ok
    assert result.tables == ["customers"]


def test_inline_comment_does_not_hide_a_write(guard):
    result = guard.validate("SELECT 1 /* harmless */ FROM orders")
    assert result.ok
    result = guard.validate("/*x*/ DROP TABLE orders")
    assert not result.ok


# ------------------------------------------------------------------- policy
def test_deny_list_blocks_even_an_allowed_table(policy):
    policy.denied_tables = ["orders"]
    guard = SqlGuard(policy)
    result = guard.validate("SELECT id FROM orders")
    assert not result.ok


def test_allowlist_none_permits_any_table():
    policy = Policy(dialect="sqlite", allowed_tables=None)
    guard = SqlGuard(policy)
    result = guard.validate("SELECT id FROM anything")
    assert result.ok


def test_select_star_can_be_allowed_by_policy():
    policy = Policy(dialect="sqlite", forbid_select_star=False)
    guard = SqlGuard(policy)
    result = guard.validate("SELECT * FROM orders")
    assert result.ok


def test_cost_is_estimated_from_row_estimates():
    policy = Policy(dialect="sqlite", allowed_tables=["big"])
    guard = SqlGuard(policy, row_estimates={"big": 5_000_000})
    result = guard.validate("SELECT id FROM big")
    assert result.cost == "expensive"
    assert result.estimated_rows == 5_000_000
    assert any("estimated" in w for w in result.warnings)


def test_expensive_queries_can_be_permitted():
    policy = Policy(dialect="sqlite", allowed_tables=["big"], allow_expensive_queries=True)
    guard = SqlGuard(policy, row_estimates={"big": 5_000_000})
    result = guard.validate("SELECT id FROM big")
    assert result.ok
    assert not any("estimated" in w for w in result.warnings)


def test_parameterized_limit_is_rejected(guard):
    result = guard.validate("SELECT id FROM orders LIMIT ?")
    assert not result.ok
    assert result.denied_by == "limit"


def test_dialect_is_honoured():
    policy = Policy(dialect="postgres", allowed_tables=["orders"])
    guard = SqlGuard(policy)
    result = guard.validate("SELECT id FROM orders")
    assert result.ok
    assert "LIMIT" in result.sql
