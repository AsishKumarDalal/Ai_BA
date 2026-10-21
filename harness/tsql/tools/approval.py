"""Human approval for risky-but-read-only actions.

The AST guard hard-denies anything that is not a read query — no approval
mode, no override, no "yolo" equivalent can bring a `DROP TABLE` back. That
is the *floor*.

This module is the layer above it: read-only actions that are nonetheless
risky or expensive. Those get a human decision:

| Action | Default |
|---|---|
| A query the cost estimator flags as expensive | ask |
| A query that would expose masked PII | ask |
| A query estimated to scan a very large number of rows | ask |

Approval modes:

* ``smart``  — ask only when the risk assessment says it matters.
* ``manual`` — always ask.
* ``off``    — never ask (trusted environments only).

An unanswered prompt is a **denial**. The failure mode is closed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

#: Approval outcomes.
APPROVED = "approved"
DENIED = "denied"
TIMED_OUT = "timed_out"
ALLOWED_ALWAYS = "allowed_always"

#: Risk bands.
LOW = "low"
MEDIUM = "medium"
HIGH = "high"


@dataclass
class ApprovalRequest:
    """A request for a human decision."""

    tool: str
    reason: str
    detail: dict = field(default_factory=dict)
    risk: str = MEDIUM
    action: str = "once"          # once | session | always | deny

    def to_dict(self) -> dict:
        return {
            "tool": self.tool,
            "reason": self.reason,
            "detail": self.detail,
            "risk": self.risk,
            "action": self.action,
        }


#: A callback that shows the request to a human and returns their decision.
ApprovalCallback = Callable[[ApprovalRequest], str]


class ApprovalManager:
    """Decides whether a risky action needs a human, and asks when it does."""

    def __init__(
        self,
        mode: str = "smart",
        timeout: int = 300,
        callback: ApprovalCallback | None = None,
        allowlist: list[str] | None = None,
        risk_assessor: Callable[[ApprovalRequest], str] | None = None,
    ):
        self.mode = (mode or "smart").lower()
        self.timeout = timeout
        self.callback = callback
        self.allowlist = list(allowlist or [])
        self._risk_assessor = risk_assessor
        #: Patterns approved for the rest of the session.
        self._session_allowlist: set[str] = set()

    # ------------------------------------------------------------------ risk
    def assess(self, request: ApprovalRequest) -> str:
        """Rate a request. Deterministic heuristics, optionally model-assisted."""
        if self._risk_assessor is not None:
            try:
                rating = self._risk_assessor(request)
                if rating in (LOW, MEDIUM, HIGH):
                    return rating
            except Exception:  # noqa: BLE001 - fall back to the heuristics
                pass
        return self._heuristic_risk(request)

    @staticmethod
    def _heuristic_risk(request: ApprovalRequest) -> str:
        detail = request.detail or {}
        if detail.get("cost") == "expensive":
            return HIGH
        if detail.get("exposes_pii"):
            return HIGH
        if detail.get("estimated_rows", 0) >= 10_000_000:
            return HIGH
        if detail.get("estimated_rows", 0) >= 1_000_000:
            return MEDIUM
        if detail.get("cost") == "moderate":
            return MEDIUM
        return LOW

    # --------------------------------------------------------------- decisions
    def request(self, request: ApprovalRequest) -> str:
        """Ask for approval if needed. Returns the decision."""
        if self.mode == "off":
            return APPROVED
        if self._is_allowlisted(request):
            return APPROVED
        if self.mode == "smart" and self.assess(request) == LOW:
            return APPROVED
        if self.callback is None:
            # No human channel. Fail closed.
            return DENIED
        return self._ask(request)

    def _is_allowlisted(self, request: ApprovalRequest) -> bool:
        key = self._pattern_key(request)
        return key in self._session_allowlist or key in self.allowlist

    @staticmethod
    def _pattern_key(request: ApprovalRequest) -> str:
        """A stable key for the *kind* of action, so one approval covers the rest."""
        detail = request.detail or {}
        if detail.get("cost") == "expensive":
            return "expensive_query"
        if detail.get("exposes_pii"):
            return "expose_pii"
        return request.reason

    def _ask(self, request: ApprovalRequest) -> str:
        started = time.monotonic()
        try:
            decision = self.callback(request)
        except Exception:  # noqa: BLE001 - a broken callback is a denial
            return DENIED
        if decision is None:
            return DENIED

        decision = str(decision).lower().strip()
        if decision in ("approve", "approved", "yes", "y", "ok", "go", "allow"):
            return APPROVED
        if decision in ("always", "allow_always", "session", "allow_session"):
            self._session_allowlist.add(self._pattern_key(request))
            return ALLOWED_ALWAYS
        if decision in ("timeout", "expired"):
            return TIMED_OUT
        return DENIED

    # ---------------------------------------------------------------- helpers
    def allow_for_session(self, pattern: str) -> None:
        self._session_allowlist.add(pattern)

    def clear_session_allowlist(self) -> None:
        self._session_allowlist.clear()

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "timeout": self.timeout,
            "allowlist": sorted(self.allowlist),
            "session_allowlist": sorted(self._session_allowlist),
        }


def make_approval_manager(
    policy: Any,
    callback: ApprovalCallback | None = None,
    allowlist: list[str] | None = None,
) -> ApprovalManager:
    """Build an approval manager from a policy."""
    return ApprovalManager(
        mode=getattr(policy, "approval_mode", "smart"),
        timeout=getattr(policy, "approval_timeout_seconds", 300),
        callback=callback,
        allowlist=allowlist,
    )
