"""Typed error taxonomy.

Every error carries a machine-readable ``code`` so the agent loop can decide whether
to retry, repair, or surface the blocker to the user.
"""

from __future__ import annotations


class TsqlError(Exception):
    """Base class for all harness errors."""

    code = "error"

    def __init__(self, message: str, code: str | None = None, details: dict | None = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.details = details or {}

    def to_dict(self) -> dict:
        return {"error": self.message, "code": self.code, "details": self.details}


class ConfigError(TsqlError):
    code = "config_error"


class ProviderError(TsqlError):
    """Raised for transport/auth/rate-limit failures against a model provider."""

    code = "provider_error"

    def __init__(self, message: str, code: str | None = None, details: dict | None = None,
                 retryable: bool = False, status_code: int | None = None):
        super().__init__(message, code, details)
        self.retryable = retryable
        self.status_code = status_code


class ToolError(TsqlError):
    """Raised inside a tool handler. Converted to ``{"error": ...}`` JSON, never to a crash."""

    code = "tool_error"


class GuardError(TsqlError):
    """The AST guard refused a query. Always fail-closed."""

    code = "sql_blocked"


class ApprovalRequired(TsqlError):
    """A gated action needs a human decision."""

    code = "approval_required"


class BudgetExhausted(TsqlError):
    """The iteration budget ran out."""

    code = "budget_exhausted"


class ContextOverflow(TsqlError):
    """The provider rejected the request because the context window overflowed."""

    code = "context_overflow"


class SessionError(TsqlError):
    code = "session_error"


class DatabaseError(TsqlError):
    code = "database_error"


class SpecError(TsqlError):
    """A chart spec or artifact spec failed validation."""

    code = "spec_error"
