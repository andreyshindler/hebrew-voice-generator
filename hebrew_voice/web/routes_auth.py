"""Signup, login, logout, and the session bootstrap endpoint."""

from __future__ import annotations

import hmac
import logging

from fastapi import APIRouter, Depends, Request, Response
from starlette.concurrency import run_in_threadpool

from .. import repo, security
from ..config import Settings
from ..errors import (
    AccountLocked,
    Conflict,
    Forbidden,
    NotFound,
    RateLimited,
    Unauthorized,
)
from ..models import User
from ..voices import catalog
from .deps import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    client_ip,
    current_auth,
    get_settings,
    require_csrf,
    require_user,
)
from .schemas import LoginRequest, SignupRequest

log = logging.getLogger("hebrew_voice.auth")

router = APIRouter(prefix="/api/auth", tags=["auth"])


def set_session_cookies(
    response: Response, settings: Settings, *, token: str, csrf_token: str
) -> None:
    """Attach the session and CSRF cookies to a response.

    The session cookie is HttpOnly; the CSRF cookie deliberately is not, since
    the frontend has to read it to echo the token back in a header.
    """
    max_age = settings.session_ttl_days * 86400
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=max_age,
        httponly=True,
        secure=settings.secure_cookies,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        CSRF_COOKIE,
        csrf_token,
        max_age=max_age,
        httponly=False,
        secure=settings.secure_cookies,
        samesite="lax",
        path="/",
    )


def clear_session_cookies(response: Response, settings: Settings) -> None:
    for name in (SESSION_COOKIE, CSRF_COOKIE):
        response.delete_cookie(name, path="/", secure=settings.secure_cookies, samesite="lax")


async def _start_session(
    request: Request, response: Response, settings: Settings, user: User
) -> str:
    token = security.new_session_token()
    csrf_token = security.new_csrf_token()
    await run_in_threadpool(
        repo.create_session,
        settings.db_path,
        token_hash=security.hash_session_token(token),
        user_id=user.id,
        csrf_token=csrf_token,
        ttl_seconds=settings.session_ttl_days * 86400,
        user_agent=(request.headers.get("user-agent") or "")[:300],
        ip=client_ip(request),
    )
    set_session_cookies(response, settings, token=token, csrf_token=csrf_token)
    return csrf_token


@router.post("/signup", status_code=201)
async def signup(
    payload: SignupRequest,
    request: Request,
    response: Response,
    settings: Settings = Depends(get_settings),
):
    """Create an account. Requires a valid invite code from the configuration."""
    if not settings.signup_enabled:
        # 404 rather than 403: a disabled route shouldn't confirm it exists.
        raise NotFound("Signup is closed", code="signup_disabled")

    runner = request.app.state.runner
    allowed, retry_after = runner.signup_limiter.check(f"ip:{client_ip(request)}")
    if not allowed:
        raise RateLimited(
            "Too many signup attempts",
            headers={"Retry-After": str(max(1, int(retry_after)))},
        )

    supplied = payload.invite_code.strip()
    valid = any(
        hmac.compare_digest(supplied, code) for code in settings.invite_codes
    )
    if not valid:
        raise Forbidden("Invalid invite code", code="invalid_invite")

    password_hash = await _hash(request, payload.password, settings)
    try:
        user = await run_in_threadpool(
            repo.create_user,
            settings.db_path,
            email=payload.email,
            password_hash=password_hash,
            invite_code=supplied,
        )
    except repo.EmailTaken as exc:
        raise Conflict("That email is already registered", code="email_taken") from exc

    csrf_token = await _start_session(request, response, settings, user)
    log.info("new account %s (admin=%s)", user.email, user.is_admin)
    return {"user": user.public(), "csrf_token": csrf_token}


@router.post("/login")
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    settings: Settings = Depends(get_settings),
):
    """Exchange credentials for a session cookie."""
    runner = request.app.state.runner
    ip = client_ip(request)
    for key in (f"ip:{ip}", f"email:{payload.email.strip().lower()}"):
        allowed, retry_after = runner.login_limiter.check(key)
        if not allowed:
            raise RateLimited(
                "Too many attempts, wait a moment",
                headers={"Retry-After": str(max(1, int(retry_after)))},
            )

    user = await run_in_threadpool(repo.get_user_by_email, settings.db_path, payload.email)
    if user is None:
        # Burn the same work as a real check so timing doesn't leak whether
        # the account exists.
        await _dummy_hash(request, settings)
        raise Unauthorized("Incorrect email or password", code="invalid_credentials")

    import time as _time

    if user.locked_until > _time.time():
        raise AccountLocked(
            "Account temporarily locked after too many failed attempts",
            detail={"locked_until": user.locked_until},
        )
    if not user.is_active:
        raise Forbidden("This account is disabled", code="account_disabled")

    ok = await _verify(request, payload.password, user.password_hash, settings)
    if not ok:
        locked_until = await run_in_threadpool(
            repo.note_failed_login,
            settings.db_path,
            user.id,
            max_failures=settings.max_failed_logins,
            lockout_seconds=settings.lockout_minutes * 60,
        )
        if locked_until:
            raise AccountLocked(
                "Account temporarily locked after too many failed attempts",
                detail={"locked_until": locked_until},
            )
        raise Unauthorized("Incorrect email or password", code="invalid_credentials")

    await run_in_threadpool(repo.clear_failed_logins, settings.db_path, user.id)

    if security.needs_rehash(
        user.password_hash, n=settings.scrypt_n, r=settings.scrypt_r, p=settings.scrypt_p
    ):
        upgraded = await _hash(request, payload.password, settings)
        await run_in_threadpool(repo.set_password, settings.db_path, user.id, upgraded)

    csrf_token = await _start_session(request, response, settings, user)
    return {"user": user.public(), "csrf_token": csrf_token}


@router.post("/logout", status_code=204, dependencies=[Depends(require_csrf)])
async def logout(
    request: Request, response: Response, settings: Settings = Depends(get_settings)
):
    """Revoke the current session server-side and clear the cookies."""
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        await run_in_threadpool(
            repo.delete_session, settings.db_path, security.hash_session_token(token)
        )
    clear_session_cookies(response, settings)
    return Response(status_code=204, headers=dict(response.headers))


@router.get("/me")
async def me(request: Request, user: User = Depends(require_user)):
    """Everything the frontend needs to render a logged-in page."""
    settings: Settings = request.app.state.settings
    runner = request.app.state.runner
    auth = await current_auth(request)
    quota = await runner.quota_state(user)
    return {
        "user": user.public(),
        "csrf_token": auth[0].csrf_token if auth else "",
        "limits": {
            "max_chars": settings.max_chars,
            **quota.public(),
        },
        "voices": catalog(),
    }


# -- password hashing, kept off the event loop and bounded -------------------


async def _hash(request: Request, password: str, settings: Settings) -> str:
    runner = request.app.state.runner
    async with runner.hash_sem:
        return await run_in_threadpool(
            security.hash_password,
            password,
            n=settings.scrypt_n,
            r=settings.scrypt_r,
            p=settings.scrypt_p,
        )


async def _verify(request: Request, password: str, encoded: str, settings: Settings) -> bool:
    runner = request.app.state.runner
    async with runner.hash_sem:
        return await run_in_threadpool(security.verify_password, password, encoded)


async def _dummy_hash(request: Request, settings: Settings) -> None:
    runner = request.app.state.runner
    async with runner.hash_sem:
        await run_in_threadpool(
            security.dummy_verify,
            n=settings.scrypt_n,
            r=settings.scrypt_r,
            p=settings.scrypt_p,
        )
