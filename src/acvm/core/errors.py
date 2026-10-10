"""Error taxonomy. ``retryable`` drives Temporal retry policy: non-retryable errors fail the
activity immediately instead of burning retries (and API budget) on a deterministic failure."""

from __future__ import annotations


class ACVMError(Exception):
    retryable: bool = False
    code: str = "acvm_error"

    def __init__(self, message: str, **context: object) -> None:
        super().__init__(message)
        self.context = context


class ConfigError(ACVMError):
    code = "config_error"


class ValidationFailed(ACVMError):
    code = "validation_failed"


class PolicyViolation(ACVMError):
    """A hard rule (§8.2 never-do list, gate bypass attempt, RBAC) was hit."""

    code = "policy_violation"


class BudgetExceeded(ACVMError):
    code = "budget_exceeded"


class QuotaExhausted(ACVMError):
    retryable = True
    code = "quota_exhausted"

    def __init__(self, message: str, retry_after_seconds: float, **context: object) -> None:
        super().__init__(message, **context)
        self.retry_after_seconds = retry_after_seconds


class UpstreamError(ACVMError):
    """External API failure. 5xx/timeouts are retryable; 4xx (except 429) are not."""

    code = "upstream_error"

    def __init__(self, message: str, *, status: int | None = None, retryable: bool = True,
                 **context: object) -> None:
        super().__init__(message, status=status, **context)
        self.status = status
        self.retryable = retryable


class CircuitOpen(ACVMError):
    retryable = True
    code = "circuit_open"

    def __init__(self, message: str, retry_after_seconds: float, **context: object) -> None:
        super().__init__(message, **context)
        self.retry_after_seconds = retry_after_seconds


class NotFound(ACVMError):
    code = "not_found"


class Conflict(ACVMError):
    code = "conflict"


class IntegrityViolation(ACVMError):
    """Link tampering / signature mismatch (§10). Never retried, always alerted."""

    code = "integrity_violation"
