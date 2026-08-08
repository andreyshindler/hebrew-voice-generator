"""Generation history: listing, artifact downloads, deletion."""

from __future__ import annotations

from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, Path, Query, Request, Response
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from .. import repo, storage
from ..config import Settings
from ..errors import NotFound
from ..models import Generation, User
from ..storage import GENERATION_ID_RE
from .deps import get_settings, require_csrf, require_user

router = APIRouter(
    prefix="/api/generations", tags=["history"], dependencies=[Depends(require_user)]
)

#: Ids are opaque hex, so a cached artifact can never change under its URL.
_IMMUTABLE = "private, max-age=31536000, immutable"


async def _load(settings: Settings, gen_id: str, user: User) -> Generation:
    generation = await run_in_threadpool(
        repo.get_generation, settings.db_path, gen_id, user.id
    )
    if generation is None:
        # 404 rather than 403 for someone else's id, so ids can't be probed.
        raise NotFound("No such generation")
    return generation


@router.get("")
async def list_generations(
    settings: Settings = Depends(get_settings),
    user: User = Depends(require_user),
    limit: int = Query(default=20, ge=1, le=100),
    before: Optional[int] = Query(default=None, ge=0),
    full: bool = Query(default=False),
):
    """Newest first, keyset-paginated - no OFFSET scans as history grows."""
    items = await run_in_threadpool(
        repo.list_generations, settings.db_path, user.id, limit=limit + 1, before=before
    )
    has_more = len(items) > limit
    items = items[:limit]
    return {
        "items": [g.public(full=full, base=settings.root_path) for g in items],
        "has_more": has_more,
        "next_before": items[-1].created_at if items and has_more else None,
    }


@router.get("/{gen_id}")
async def get_generation(
    gen_id: str = Path(pattern=GENERATION_ID_RE),
    settings: Settings = Depends(get_settings),
    user: User = Depends(require_user),
):
    """Full record, including the original text - used to reload the composer."""
    generation = await _load(settings, gen_id, user)
    return generation.public(full=True, base=settings.root_path)


def _content_disposition(title: str, extension: str, *, attachment: bool) -> str:
    """Build a header that survives a Hebrew title.

    Sends an ASCII fallback plus the RFC 5987 ``filename*`` form, so browsers
    that don't understand the latter still get a sensible name.
    """
    disposition = "attachment" if attachment else "inline"
    safe = "".join(ch for ch in title if (ch.isascii() and ch.isalnum()) or ch in " -_").strip()
    # A Hebrew title reduces to punctuation and stray digits; a generic name
    # beats "50.mp3" for the browsers that only read the ASCII fallback.
    if len(safe) < 3:
        safe = "hebrew-voice"
    safe = safe[:60]
    pretty = (title.strip() or "audio")[:60]
    return (
        f"{disposition}; filename=\"{safe}.{extension}\"; "
        f"filename*=UTF-8''{quote(pretty + '.' + extension)}"
    )


@router.get("/{gen_id}/audio.mp3")
async def get_audio(
    request: Request,
    gen_id: str = Path(pattern=GENERATION_ID_RE),
    download: bool = Query(default=False),
    settings: Settings = Depends(get_settings),
    user: User = Depends(require_user),
):
    """Serve the MP3. ``FileResponse`` gives Range support, so seeking works."""
    generation = await _load(settings, gen_id, user)
    if not generation.audio_rel:
        raise NotFound("This generation has no audio")
    path = storage.resolve_under(settings.data_dir, generation.audio_rel)
    return FileResponse(
        path,
        media_type="audio/mpeg",
        headers={
            "Cache-Control": _IMMUTABLE,
            "Content-Disposition": _content_disposition(
                generation.title, "mp3", attachment=download
            ),
        },
    )


@router.get("/{gen_id}/subtitles.srt")
async def get_srt(
    gen_id: str = Path(pattern=GENERATION_ID_RE),
    settings: Settings = Depends(get_settings),
    user: User = Depends(require_user),
):
    generation = await _load(settings, gen_id, user)
    if not generation.srt_rel:
        raise NotFound("This generation has no subtitles")
    path = storage.resolve_under(settings.data_dir, generation.srt_rel)
    return FileResponse(
        path,
        media_type="text/plain; charset=utf-8",
        headers={
            "Cache-Control": _IMMUTABLE,
            "Content-Disposition": _content_disposition(
                generation.title, "srt", attachment=True
            ),
        },
    )


@router.get("/{gen_id}/subtitles.vtt")
async def get_vtt(
    gen_id: str = Path(pattern=GENERATION_ID_RE),
    settings: Settings = Depends(get_settings),
    user: User = Depends(require_user),
):
    """Served inline - the ``<track>`` element can't use an attachment."""
    generation = await _load(settings, gen_id, user)
    if not generation.vtt_rel:
        raise NotFound("This generation has no subtitles")
    path = storage.resolve_under(settings.data_dir, generation.vtt_rel)
    return FileResponse(
        path,
        media_type="text/vtt; charset=utf-8",
        headers={"Cache-Control": _IMMUTABLE},
    )


@router.delete("/{gen_id}", status_code=204, dependencies=[Depends(require_csrf)])
async def delete_generation(
    gen_id: str = Path(pattern=GENERATION_ID_RE),
    settings: Settings = Depends(get_settings),
    user: User = Depends(require_user),
):
    """Remove a generation and its files."""
    generation = await _load(settings, gen_id, user)
    await run_in_threadpool(
        storage.delete_files,
        settings.data_dir,
        (generation.audio_rel, generation.srt_rel, generation.vtt_rel),
    )
    await run_in_threadpool(repo.delete_generation, settings.db_path, gen_id, user.id)
    return Response(status_code=204)
