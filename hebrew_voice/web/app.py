"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from .. import cleanup, db, storage
from ..config import Settings, get_settings as load_settings
from ..errors import AppError, Unauthorized
from . import routes_auth, routes_history, routes_pages, routes_synth
from .runner import SynthRunner

log = logging.getLogger("hebrew_voice.web")

HERE = Path(__file__).parent

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "media-src 'self'; font-src 'self'; connect-src 'self'; form-action 'self'; "
    "frame-ancestors 'none'; base-uri 'none'"
)

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    """Build the application. Tests call this with their own settings."""
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db.migrate(settings.db_path)
        storage.ensure_data_dir(settings.data_dir)
        _limit_threadpool(settings.thread_pool_size)
        log.info(
            "ready: max_concurrent=%d per_user=%d daily_quota=%d max_chars=%d",
            settings.max_concurrent_synth,
            settings.max_concurrent_per_user,
            settings.daily_char_quota,
            settings.max_chars,
        )
        task = asyncio.create_task(_cleanup_loop(settings))
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    app = FastAPI(
        title="Hebrew Voice Generator",
        version=__import__("hebrew_voice").__version__,
        docs_url="/api/docs" if not settings.is_production else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if not settings.is_production else None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.runner = SynthRunner(settings)
    app.state.templates = Jinja2Templates(directory=str(HERE / "templates"))
    # Templates build every link as {{ base }}/...; empty at the root.
    app.state.templates.env.globals["base"] = settings.root_path

    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    app.include_router(routes_pages.router)
    app.include_router(routes_auth.router)
    app.include_router(routes_synth.router)
    app.include_router(routes_history.router)

    _install_middleware(app, settings)
    _install_error_handlers(app, settings)
    if settings.root_path:
        # Wraps everything above, so routing sees a root-relative path no
        # matter which proxy style is in front.
        return PrefixStripper(app, settings.root_path)  # type: ignore[return-value]
    return app


class PrefixStripper:
    """Serve an app mounted at ``prefix`` whether or not the proxy strips it.

    ``proxy_pass http://host:8080/;`` (trailing slash) strips the prefix, while
    ``proxy_pass http://host:8080;`` forwards it intact. Rather than depend on
    getting that one character right - or on Starlette's ``root_path``
    semantics, which have shifted between versions - this removes the prefix if
    it is present and leaves the request alone if it isn't.
    """

    def __init__(self, app, prefix: str) -> None:
        self.app = app
        self.prefix = prefix

    def __getattr__(self, name):
        # Keep the FastAPI instance usable for tests and tooling that reach for
        # .routes, .state, .router, and friends.
        return getattr(self.app, name)

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            path = scope.get("path", "")
            if path == self.prefix or path.startswith(self.prefix + "/"):
                stripped = path[len(self.prefix) :] or "/"
                scope = dict(scope)
                scope["path"] = stripped
                # Deliberately NOT setting scope["root_path"]: Starlette's
                # Mount matching assumes path still begins with root_path, and
                # setting both would break the /static mount. Nothing here uses
                # url_for, so the app has no other need for it.
                raw = scope.get("raw_path")
                if raw:
                    encoded = self.prefix.encode()
                    if raw.startswith(encoded):
                        scope["raw_path"] = raw[len(encoded) :] or b"/"
        await self.app(scope, receive, send)


def _limit_threadpool(size: int) -> None:
    """Cap anyio's worker threads.

    The default of 40 would let 40 concurrent scrypt hashes allocate over a
    gigabyte at once, which is a cheap way to knock over a small VPS.
    """
    try:
        import anyio.to_thread

        anyio.to_thread.current_default_thread_limiter().total_tokens = max(4, size)
    except Exception as exc:  # pragma: no cover - anyio internals moved
        log.warning("could not cap the thread pool: %r", exc)


async def _cleanup_loop(settings: Settings) -> None:
    """Run the retention sweep on a timer for the life of the process."""
    interval = max(1, settings.cleanup_interval_min) * 60
    while True:
        try:
            await asyncio.to_thread(cleanup.sweep, settings)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a sweep failure must not kill the app
            log.warning("retention sweep failed: %r", exc)
        await asyncio.sleep(interval)


def _install_middleware(app: FastAPI, settings: Settings) -> None:
    @app.middleware("http")
    async def guard_and_harden(request: Request, call_next):
        # Cross-origin state changes are refused before any handler runs.
        if request.method not in _SAFE_METHODS:
            rejection = _origin_violation(request, settings)
            if rejection is not None:
                return rejection

        response = await call_next(request)

        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        content_type = response.headers.get("content-type", "")
        if content_type.startswith("text/html"):
            response.headers.setdefault("Content-Security-Policy", CSP)
        return response


def _blocked() -> JSONResponse:
    return JSONResponse(
        status_code=403,
        content={
            "error": {
                "code": "cross_origin_blocked",
                "message": "Cross-origin requests are not allowed",
                "detail": {},
            }
        },
    )


def _origin_violation(request: Request, settings: Settings):
    """Reject a cross-site unsafe request, if the browser tells us it is one."""
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site and fetch_site not in ("same-origin", "same-site", "none"):
        return _blocked()

    origin = request.headers.get("origin")
    if origin:
        # Compare origins only. A browser's Origin header is scheme://host and
        # never carries a path, so comparing it against a base URL that has one
        # (https://host/voice-gen) would reject every write.
        expected = settings.origin or f"{request.url.scheme}://{request.url.netloc}"
        if origin.rstrip("/") != expected.rstrip("/"):
            return _blocked()
    return None


def _wants_html(request: Request, settings: Settings) -> bool:
    """True for a browser navigation, false for an XHR that wants JSON.

    The prefix is stripped first, so this stays correct whether or not the
    proxy strips it - otherwise an expired session on /voice-gen/api/... would
    answer an XHR with an HTML redirect instead of a 401.
    """
    path = request.url.path
    if settings.root_path and path.startswith(settings.root_path):
        path = path[len(settings.root_path) :] or "/"
    return "text/html" in request.headers.get("accept", "") and not path.startswith("/api/")


def _install_error_handlers(app: FastAPI, settings: Settings) -> None:
    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError):
        # An unauthenticated page view should land on the login form, not on
        # a JSON body the browser will render as text.
        if isinstance(exc, Unauthorized) and _wants_html(request, settings):
            target = settings.url("/login")
            return RedirectResponse(
                f"{target}?next={quote(request.url.path, safe='/')}", status_code=303
            )
        return JSONResponse(status_code=exc.status, content=exc.payload(), headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def handle_validation(request: Request, exc: RequestValidationError):
        first = exc.errors()[0] if exc.errors() else {}
        field = ".".join(str(part) for part in first.get("loc", [])[1:]) or "request"
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_failed",
                    "message": f"Invalid value for {field}",
                    "detail": {"field": field, "reason": first.get("msg", "")},
                }
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http(request: Request, exc: StarletteHTTPException):
        if exc.status_code == 404 and _wants_html(request, settings):
            return JSONResponse(
                status_code=404,
                content={"error": {"code": "not_found", "message": "Page not found", "detail": {}}},
            )
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": f"http_{exc.status_code}",
                    "message": str(exc.detail),
                    "detail": {},
                }
            },
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception):
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "internal_error",
                    "message": "Something went wrong",
                    "detail": {},
                }
            },
        )


#: Module-level app for ``uvicorn hebrew_voice.web.app:app``.
def _default_app() -> FastAPI:
    from ..config import load_dotenv

    load_dotenv()
    return create_app(Settings.from_env())


def __getattr__(name: str):
    """Build the default app only when something actually asks for it.

    Keeps ``import hebrew_voice.web.app`` cheap (and side-effect free) for the
    tests, while ``uvicorn hebrew_voice.web.app:app`` still works.
    """
    if name == "app":
        instance = _default_app()
        globals()["app"] = instance
        return instance
    raise AttributeError(name)
