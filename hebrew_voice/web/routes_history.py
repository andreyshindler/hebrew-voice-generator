"""Generation history: listing, artifact downloads, deletion."""

from __future__ import annotations

from typing import List, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, Path, Query, Request, Response
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from .. import repo, storage
from ..config import Settings
from ..errors import NotFound, UnprocessableEntity
from ..models import Generation, User
from ..storage import GENERATION_ID_RE
from ..synth import (
    MAX_WORDS_PER_CUE,
    READABLE_WORDS_PER_CUE,
    Cue,
    group_cues,
    load_cues,
    to_srt,
    to_vtt,
)
from .deps import get_settings, require_csrf, require_user, require_verified

router = APIRouter(
    prefix="/api/generations",
    tags=["history"],
    dependencies=[Depends(require_user), Depends(require_verified)],
)

#: Ids are opaque hex, so a cached artifact can never change under its URL.
_IMMUTABLE = "private, max-age=31536000, immutable"

#: Regrouped subtitles are computed from the query string, which breaks the
#: premise above - the same URL path now has more than one right answer.
_REVALIDATE = "private, max-age=0, must-revalidate"


#: Density knobs shared by both subtitle endpoints. ``words=None`` means "serve
#: the file exactly as it was stored", which is the common case and the only
#: one that touches neither the cue file nor the CPU.
_WORDS = Query(
    default=None,
    ge=1,
    le=MAX_WORDS_PER_CUE,
    description="Words per cue. 1 gives the word-by-word karaoke style.",
)
_STRIP = Query(default=False, description="Drop trailing punctuation from each cue.")
_MIN_DURATION = Query(
    default=0.0,
    ge=0.0,
    le=5.0,
    description="Stretch cues shorter than this many seconds, up to the next cue.",
)


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


def _regrouped(
    settings: Settings,
    generation: Generation,
    *,
    words: int,
    strip_punctuation: bool,
    min_duration: float,
) -> List[Cue]:
    """Re-render this generation's cues at a different density.

    The per-word timings were saved alongside the audio, so this costs a small
    file read and no quota - the alternative would be synthesising again.
    """
    if not generation.cues_rel:
        # Made before the word timings were kept, or made without subtitles.
        # A distinct code so the UI can hide the control instead of showing a
        # broken one.
        raise UnprocessableEntity(
            "This recording was made before subtitle density could be changed",
            code="cues_unavailable",
        )
    try:
        path = storage.resolve_under(settings.data_dir, generation.cues_rel)
        word_cues = load_cues(path.read_bytes())
    except (NotFound, OSError, ValueError) as exc:
        # A missing or corrupt cue file is not a missing *generation*: the
        # audio and the stored subtitles are still there and still work.
        raise UnprocessableEntity(
            "The stored cue timings could not be read", code="cues_unavailable"
        ) from exc
    return group_cues(
        word_cues,
        words_per_cue=words,
        strip_punctuation=strip_punctuation,
        min_duration=min_duration,
    )


async def _serve_subtitles(
    settings: Settings,
    generation: Generation,
    *,
    stored_rel: Optional[str],
    render,
    media_type: str,
    disposition: Optional[str],
    words: Optional[int],
    strip_punctuation: bool,
    min_duration: float,
) -> Response:
    """Serve subtitles, off disk when possible and recomputed when asked."""
    if not stored_rel:
        raise NotFound("This generation has no subtitles")
    headers = {"Cache-Control": _IMMUTABLE}
    if disposition:
        headers["Content-Disposition"] = disposition

    # No density asked for: hand back the file exactly as it was written.
    if words is None and not strip_punctuation and min_duration <= 0:
        path = storage.resolve_under(settings.data_dir, stored_rel)
        return FileResponse(path, media_type=media_type, headers=headers)

    cues = await run_in_threadpool(
        _regrouped,
        settings,
        generation,
        words=READABLE_WORDS_PER_CUE if words is None else words,
        strip_punctuation=strip_punctuation,
        min_duration=min_duration,
    )
    # The body now depends on the query string, so it is no longer immutable.
    headers["Cache-Control"] = _REVALIDATE
    return Response(render(cues), media_type=media_type, headers=headers)


@router.get("/{gen_id}/subtitles.srt")
async def get_srt(
    gen_id: str = Path(pattern=GENERATION_ID_RE),
    words: Optional[int] = _WORDS,
    strip_punctuation: bool = _STRIP,
    min_duration: float = _MIN_DURATION,
    settings: Settings = Depends(get_settings),
    user: User = Depends(require_user),
):
    generation = await _load(settings, gen_id, user)
    return await _serve_subtitles(
        settings,
        generation,
        stored_rel=generation.srt_rel,
        render=to_srt,
        media_type="text/plain; charset=utf-8",
        disposition=_content_disposition(generation.title, "srt", attachment=True),
        words=words,
        strip_punctuation=strip_punctuation,
        min_duration=min_duration,
    )


@router.get("/{gen_id}/subtitles.vtt")
async def get_vtt(
    gen_id: str = Path(pattern=GENERATION_ID_RE),
    words: Optional[int] = _WORDS,
    strip_punctuation: bool = _STRIP,
    min_duration: float = _MIN_DURATION,
    settings: Settings = Depends(get_settings),
    user: User = Depends(require_user),
):
    """Served inline - the ``<track>`` element can't use an attachment."""
    generation = await _load(settings, gen_id, user)
    return await _serve_subtitles(
        settings,
        generation,
        stored_rel=generation.vtt_rel,
        render=to_vtt,
        media_type="text/vtt; charset=utf-8",
        disposition=None,
        words=words,
        strip_punctuation=strip_punctuation,
        min_duration=min_duration,
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
        (
            generation.audio_rel,
            generation.srt_rel,
            generation.vtt_rel,
            generation.cues_rel,
        ),
    )
    await run_in_threadpool(repo.delete_generation, settings.db_path, gen_id, user.id)
    return Response(status_code=204)
