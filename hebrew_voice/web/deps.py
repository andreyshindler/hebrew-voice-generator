"""Shared request dependencies: settings, the current user, and CSRF."""

from __future__ import annotations

from typing import Optional, Tuple

from fastapi import Depends, Request
from starlette.concurrency import run_in_threadpool

from .. import repo, security
from ..config import Settings
from ..errors import Forbidden, Unauthorized
from ..models import Session, User

__all__ = [
    "SESSION_COOKIE",
    "CSRF_COOKIE",
    "get_settings",
    "client_ip",
    "current_auth",
    "current_user",
    "require_user",
    "require_csrf",
    "require_admin",
]

SESSION_COOKIE = "hv_session"
CSRF_COOKIE = "hv_csrf"

#: Sliding session renewal: don't write to the database on every request.
_TOUCH_AFTER_SECONDS = 3600


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def client_ip(request: Request) -> str:
    """The caller's address, honouring ``X-Forwarded-For`` behind nginx."""
    settings: Settings = request.app.state.settings
    if settings.trust_proxy:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def current_auth(request: Request) -> Optional[Tuple[Session, User]]:
    """Resolve the session cookie, or ``None`` when there isn't a live one."""
    cached = getattr(request.state, "auth", "unset")
    if cached != "unset":
        return cached

    token = request.cookies.get(SESSION_COOKIE)
    auth = None
    if token:
        settings: Settings = request.app.state.settings
        token_hash = security.hash_session_token(token)
        auth = await run_in_threadpool(repo.get_session_with_user, settings.db_path, token_hash)
        if auth is not None:
            session, _user = auth
            import time as _time

            if _time.time() - session.last_seen_at > _TOUCH_AFTER_SECONDS:
                await run_in_threadpool(
                    repo.touch_session,
                    settings.db_path,
                    token_hash,
                    ttl_seconds=settings.session_ttl_days * 86400,
                )
    request.state.auth = auth
    return auth


async def current_user(request: Request) -> Optional[User]:
    auth = await current_auth(request)
    return auth[1] if auth else None


async def require_user(request: Request) -> User:
    """Every protected router depends on this, so auth is opt-out."""
    user = await current_user(request)
    if user is None:
        raise Unauthorized("Sign in to continue")
    return user


async def require_admin(user: User = Depends(require_user)) -> User:
    if not user.is_admin:
        raise Forbidden("Admins only", code="admin_required")
    return user


async def require_csrf(request: Request) -> None:
    """Double-submit check on every state-changing authenticated request."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    auth = await current_auth(request)
    if auth is None:
        raise Unauthorized("Sign in to continue")
    session, _user = auth
    supplied = request.headers.get("x-csrf-token", "")
    if not supplied or not security.constant_time_equals(supplied, session.csrf_token):
        raise Forbidden("CSRF token missing or invalid", code="csrf_failed")
