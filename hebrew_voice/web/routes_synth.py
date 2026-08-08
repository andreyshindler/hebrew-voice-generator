"""Voice catalog, text preview, and the synthesis endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ..config import Settings
from ..errors import PayloadTooLarge, RateLimited
from ..models import User
from ..text import estimate_seconds, has_hebrew, prepare
from ..voices import catalog
from .deps import get_settings, require_csrf, require_user, require_verified
from .schemas import PreviewRequest, SynthesizeRequest

# Auth is a router-level dependency, so a new endpoint here is protected by
# default rather than by remembering to add it.
router = APIRouter(
    prefix="/api",
    tags=["synthesis"],
    dependencies=[Depends(require_user), Depends(require_verified)],
)


@router.get("/voices")
async def voices():
    """The Hebrew voice catalog. Static, so it works with the network down."""
    return {"voices": catalog()}


@router.post("/preview")
async def preview(
    payload: PreviewRequest,
    request: Request,
    settings: Settings = Depends(get_settings),
    user: User = Depends(require_user),
):
    """Show exactly what will be spoken, without contacting the TTS service.

    Pure CPU work, so it costs no quota and skips the synthesis gate - but it
    keeps its own cheap rate limit.
    """
    runner = request.app.state.runner
    allowed, retry_after = runner.preview_limiter.check(f"user:{user.id}")
    if not allowed:
        raise RateLimited(
            "Slow down a little",
            headers={"Retry-After": str(max(1, int(retry_after)))},
        )

    if len(payload.text) > settings.max_chars:
        raise PayloadTooLarge(
            "Text is longer than the limit",
            detail={"max_chars": settings.max_chars, "chars": len(payload.text)},
        )

    prepared = prepare(
        payload.text,
        keep_niqqud=payload.keep_niqqud,
        symbols=payload.expand_symbols,
        abbreviations=payload.expand_abbreviations,
        acronyms=payload.expand_acronyms,
    )
    return {
        "prepared": prepared,
        "char_count": len(payload.text.strip()),
        "prepared_char_count": len(prepared),
        "has_hebrew": has_hebrew(prepared),
        "estimated_seconds": estimate_seconds(prepared, payload.rate),
    }


@router.post("/synthesize", dependencies=[Depends(require_csrf)])
async def synthesize_endpoint(
    payload: SynthesizeRequest,
    request: Request,
    settings: Settings = Depends(get_settings),
    user: User = Depends(require_user),
):
    """Generate speech, store it, and return the URLs to fetch it back.

    Returns metadata rather than the MP3 itself: one run produces audio,
    subtitles, and a history entry, and the player needs a real URL so seeking
    and range requests keep working.
    """
    if len(payload.text) > settings.max_chars:
        raise PayloadTooLarge(
            "Text is longer than the limit",
            detail={"max_chars": settings.max_chars, "chars": len(payload.text)},
        )

    runner = request.app.state.runner
    generation, quota = await runner.run(user, payload)

    data = generation.public(full=True, base=settings.root_path)
    data["quota"] = quota.public()
    return data
