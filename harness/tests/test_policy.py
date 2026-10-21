"""Safety policy."""

from __future__ import annotations

from tsql.config import Config
from tsql.safety.policy import DANGEROUS_FUNCTIONS, Policy


def test_policy_from_config_carries_db_settings():
    config = Config()
    config.db.dialect = "postgres"
    config.db.allowed_tables = ["orders"]
    config.policy.max_rows = 500
    policy = Policy.from_config(config)
    assert policy.dialect == "postgres"
    assert policy.allowed_tables == ["orders"]
    assert policy.max_rows == 500


def test_allowed_table_check_is_case_insensitive():
    policy = Policy(allowed_tables=["Orders"])
    assert policy.is_table_allowed("orders")
    assert policy.is_table_allowed("ORDERS")
    assert not policy.is_table_allowed("customers")


def test_allowed_table_check_accepts_qualified_names():
    policy = Policy(allowed_tables=["public.orders"])
    assert policy.is_table_allowed("public.orders")
    assert policy.is_table_allowed("orders")


def test_denied_table_wins_over_allowlist():
    policy = Policy(allowed_tables=["orders"], denied_tables=["orders"])
    assert not policy.is_table_allowed("orders")


def test_no_allowlist_permits_anything():
    policy = Policy(allowed_tables=None)
    assert policy.is_table_allowed("whatever")


def test_pii_entitlement_requires_matching_role():
    policy = Policy(pii_entitled_roles=["privacy_officer"])
    assert not policy.role_can_read_pii()
    policy.active_role = "privacy_officer"
    assert policy.role_can_read_pii()


def test_pii_entitlement_is_case_insensitive():
    policy = Policy(pii_entitled_roles=["Privacy_Officer"], active_role="privacy_officer")
    assert policy.role_can_read_pii()


def test_no_active_role_means_no_pii_access():
    policy = Policy(pii_entitled_roles=["privacy_officer"], active_role=None)
    assert not policy.role_can_read_pii()


def test_denylist_covers_the_obvious_escapes():
    for name in ("pg_read_file", "dblink", "load_extension", "read_csv",
                 "xp_cmdshell", "copy", "attach", "system"):
        assert name in DANGEROUS_FUNCTIONS
