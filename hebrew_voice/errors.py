"""Application errors that map onto HTTP responses.

Every error carries a stable machine-readable ``code``. The frontend maps the
code to a Hebrew message, so the server never has to carry translations.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

__all__ = [
    "AppError",
    "BadRequest",
    "Unauthorized",
    "Forbidden",
    "NotFound",
    "Conflict",
    "PayloadTooLarge",
    "UnprocessableEntity",
    "AccountLocked",
    "RateLimited",
    "QuotaExceeded",
    "ServerBusy",
    "UpstreamError",
    "MailFailed",
    "SynthesisTimeout",
]


class AppError(Exception):
    """Base class for everything the API turns into a JSON error envelope."""

    status = 500
    code = "internal_error"

    def __init__(
        self,
        message: str = "",
        *,
        code: Optional[str] = None,
        detail: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> None:
        super().__init__(message or self.__class__.__name__)
        self.message = message or self.__class__.__name__
        if code:
            self.code = code
        self.detail = detail or {}
        self.headers = headers or {}

    def payload(self) -> Dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message, "detail": self.detail}}


class BadRequest(AppError):
    status = 400
    code = "bad_request"


class Unauthorized(AppError):
    status = 401
    code = "auth_required"


class Forbidden(AppError):
    status = 403
    code = "forbidden"


class NotFound(AppError):
    status = 404
    code = "not_found"


class Conflict(AppError):
    status = 409
    code = "conflict"


class PayloadTooLarge(AppError):
    status = 413
    code = "text_too_long"


class UnprocessableEntity(AppError):
    status = 422
    code = "unprocessable"


class AccountLocked(AppError):
    status = 423
    code = "account_locked"


class RateLimited(AppError):
    status = 429
    code = "rate_limited"


class QuotaExceeded(AppError):
    status = 429
    code = "quota_exceeded"


class ServerBusy(AppError):
    status = 503
    code = "server_busy"


class UpstreamError(AppError):
    status = 502
    code = "tts_upstream_failed"


class MailFailed(AppError):
    status = 502
    code = "email_send_failed"


class SynthesisTimeout(AppError):
    status = 504
    code = "synthesis_timeout"
