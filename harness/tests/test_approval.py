"""Human approval for risky-but-read-only actions."""

from __future__ import annotations

from tsql.tools.approval import (
    APPROVED,
    DENIED,
    HIGH,
    LOW,
    MEDIUM,
    TIMED_OUT,
    ApprovalManager,
    ApprovalRequest,
    make_approval_manager,
)


def request(reason="expensive_query", **detail):
    return ApprovalRequest(tool="run_sql", reason=reason, detail=detail)


# ------------------------------------------------------------------- modes
def test_off_mode_approves_everything():
    manager = ApprovalManager(mode="off")
    assert manager.request(request(cost="expensive")) == APPROVED


def test_manual_mode_always_asks():
    asked = []

    def callback(req):
        asked.append(req)
        return "approve"

    manager = ApprovalManager(mode="manual", callback=callback)
    assert manager.request(request(cost="cheap")) == APPROVED
    assert len(asked) == 1


def test_smart_mode_skips_low_risk():
    asked = []

    manager = ApprovalManager(mode="smart", callback=lambda r: asked.append(r) or "approve")
    assert manager.request(request(cost="cheap")) == APPROVED
    assert not asked


def test_smart_mode_asks_for_expensive():
    asked = []

    manager = ApprovalManager(mode="smart", callback=lambda r: asked.append(r) or "approve")
    assert manager.request(request(cost="expensive")) == APPROVED
    assert len(asked) == 1


def test_no_callback_means_denied():
    manager = ApprovalManager(mode="manual")
    assert manager.request(request(cost="expensive")) == DENIED


# ---------------------------------------------------------------- decisions
def test_denial_is_honoured():
    manager = ApprovalManager(mode="manual", callback=lambda r: "deny")
    assert manager.request(request(cost="expensive")) == DENIED


def test_a_broken_callback_denies():
    def broken(req):
        raise RuntimeError("no ui")

    manager = ApprovalManager(mode="manual", callback=broken)
    assert manager.request(request(cost="expensive")) == DENIED


def test_a_none_callback_result_denies():
    manager = ApprovalManager(mode="manual", callback=lambda r: None)
    assert manager.request(request(cost="expensive")) == DENIED


def test_always_allowlist_covers_the_rest_of_the_session():
    manager = ApprovalManager(mode="manual", callback=lambda r: "always")
    manager.request(request(cost="expensive"))
    # The second one does not reach the callback.
    asked = []
    manager.callback = lambda r: asked.append(r) or "deny"
    assert manager.request(request(cost="expensive")) == APPROVED
    assert not asked


def test_an_explicit_allowlist_is_honoured():
    manager = ApprovalManager(mode="manual", allowlist=["expensive_query"],
                              callback=lambda r: "deny")
    assert manager.request(request(cost="expensive")) == APPROVED


def test_session_allowlist_can_be_cleared():
    manager = ApprovalManager(mode="manual", callback=lambda r: "always")
    manager.request(request(cost="expensive"))
    manager.clear_session_allowlist()
    manager.callback = lambda r: "deny"
    assert manager.request(request(cost="expensive")) == DENIED


# ------------------------------------------------------------------- risk
def test_expensive_queries_are_high_risk():
    manager = ApprovalManager()
    assert manager.assess(request(cost="expensive")) == HIGH


def test_pii_exposure_is_high_risk():
    manager = ApprovalManager()
    assert manager.assess(request(reason="expose_pii", exposes_pii=True)) == HIGH


def test_a_very_large_scan_is_high_risk():
    manager = ApprovalManager()
    assert manager.assess(request(estimated_rows=50_000_000)) == HIGH


def test_a_moderate_query_is_medium_risk():
    manager = ApprovalManager()
    assert manager.assess(request(cost="moderate")) == MEDIUM


def test_a_cheap_query_is_low_risk():
    manager = ApprovalManager()
    assert manager.assess(request(cost="cheap")) == LOW


def test_a_custom_risk_assessor_overrides_the_heuristics():
    manager = ApprovalManager(risk_assessor=lambda r: LOW)
    assert manager.assess(request(cost="expensive")) == LOW


def test_a_broken_risk_assessor_falls_back_to_the_heuristics():
    def broken(req):
        raise RuntimeError("nope")

    manager = ApprovalManager(risk_assessor=broken)
    assert manager.assess(request(cost="expensive")) == HIGH


# ----------------------------------------------------------------- factory
def test_make_approval_manager_reads_the_policy():
    class FakePolicy:
        approval_mode = "manual"
        approval_timeout_seconds = 42

    manager = make_approval_manager(FakePolicy())
    assert manager.mode == "manual"
    assert manager.timeout == 42
