"""HTML pages: the app shell, login, and signup."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import __version__
from ..config import Settings
from ..errors import NotFound
from ..voices import catalog
from .deps import current_auth, get_settings

router = APIRouter(tags=["pages"])


def _templates(request: Request):
    return request.app.state.templates


@router.get("/", response_class=HTMLResponse)
async def index(request: Request, settings: Settings = Depends(get_settings)):
    """The generator itself. Anonymous visitors go to the login page."""
    auth = await current_auth(request)
    if auth is None:
        return RedirectResponse(settings.url("/login"), status_code=303)
    session, user = auth
    quota = await request.app.state.runner.quota_state(user)
    bootstrap = {
        "user": user.public(),
        "csrf_token": session.csrf_token,
        "voices": catalog(),
        "limits": {"max_chars": settings.max_chars, **quota.public()},
        "version": __version__,
    }
    return _templates(request).TemplateResponse(
        request,
        "app.html",
        {
            "bootstrap": json.dumps(bootstrap, ensure_ascii=False),
            "user": user,
            "version": __version__,
        },
    )


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, settings: Settings = Depends(get_settings)):
    if await current_auth(request) is not None:
        return RedirectResponse(settings.url("/"), status_code=303)
    return _templates(request).TemplateResponse(
        request,
        "login.html",
        {"signup_enabled": settings.signup_enabled, "version": __version__},
    )


@router.get("/signup", response_class=HTMLResponse)
async def signup_page(request: Request, settings: Settings = Depends(get_settings)):
    if not settings.signup_enabled:
        raise NotFound("Signup is closed", code="signup_disabled")
    if await current_auth(request) is not None:
        return RedirectResponse(settings.url("/"), status_code=303)
    return _templates(request).TemplateResponse(
        request, "signup.html", {"version": __version__}
    )


@router.get("/healthz")
async def healthz():
    """Liveness probe - no auth, no database, no upstream call."""
    return {"status": "ok", "version": __version__}
