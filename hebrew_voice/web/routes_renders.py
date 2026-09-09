"""Video renders: requesting one, polling it, downloading the result."""

from __future__ import annotations

import json
import time
from typing import List, Optional

from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .. import repo, storage
from ..config import Settings
from ..errors import NotFound, QuotaExceeded, RateLimited, UnprocessableEntity
from ..models import RENDER_FORMATS, RENDER_SIZES, Render, User
from ..quota import day_reset_epoch, quota_day
from ..storage import GENERATION_ID_RE
from ..synth import MAX_WORDS_PER_CUE
from .deps import get_settings, require_csrf, require_user, require_verified
from .routes_history import content_disposition

router = APIRouter(
    tags=["renders"],
    dependencies=[Depends(require_user), Depends(require_verified)],
)

_MEDIA_TYPES = {"mp4": "video/mp4", "webm": "video/webm"}

#: A render is minutes of CPU, so the same URL cannot have two answers, but the
#: file never changes once written either.
_IMMUTABLE = "private, max-age=31536000, immutable"


class RenderRequest(BaseModel):
    """What to render. Everything else comes from the server's settings."""

    format: str = Field(default="mp4")
    #: Caption density, same knob as the subtitle endpoints. Defaults to the
    #: density the generation's stored subtitles were written at.
    words_per_cue: Optional[int] = Field(default=None, ge=1, le=MAX_WORDS_PER_CUE)
    #: Frame shape by name. An overlay has to match the footage it goes over,
    #: so this matters more than it looks. Defaults to the server's setting.
    size: Optional[str] = Field(default=None)
    #: Uploaded photos and clips to show behind the captions, in running
    #: order. Empty means captions over a plain background, as before.
    media_ids: List[str] = Field(default_factory=list)


async def _checked_media(settings: Settings, user: User, media_ids: List[str]) -> List[str]:
    """Validate the requested uploads and return them in running order.

    Every id has to resolve to a file this account owns. Silently dropping a
    stranger's id would render a video quietly missing a shot, and accepting it
    would show one account another's photos.
    """
    if not media_ids:
        return []
    if len(media_ids) > settings.max_media_per_render:
        raise UnprocessableEntity(
            f"At most {settings.max_media_per_render} files can be used in one video",
            code="too_much_media",
        )
    found = await run_in_threadpool(
        repo.get_media_many, settings.db_path, media_ids, user.id
    )
    if len(found) != len(media_ids):
        raise UnprocessableEntity(
            "One of those files no longer exists", code="media_unavailable"
        )
    return [item.id for item in found]


def _require_rendering(settings: Settings) -> None:
    if not settings.rendering_enabled:
        raise UnprocessableEntity(
            "Video rendering is not configured on this server",
            code="rendering_disabled",
        )


@router.post("/api/generations/{gen_id}/renders", status_code=202,
             dependencies=[Depends(require_csrf)])
async def request_render(
    payload: RenderRequest,
    gen_id: str = Path(pattern=GENERATION_ID_RE),
    request: Request = None,  # type: ignore[assignment]
    settings: Settings = Depends(get_settings),
    user: User = Depends(require_user),
):
    """Queue a render, or hand back an identical one that already exists."""
    _require_rendering(settings)
    if payload.format not in RENDER_FORMATS:
        raise UnprocessableEntity(
            f"Unsupported format {payload.format!r}", code="unsupported_format"
        )

    generation = await run_in_threadpool(
        repo.get_generation, settings.db_path, gen_id, user.id
    )
    if generation is None:
        raise NotFound("No such generation")
    if not generation.cues_rel:
        # Same gate the density control uses: without word timings there is
        # nothing to lay captions out from.
        raise UnprocessableEntity(
            "This recording has no word timings, so it cannot be rendered",
            code="cues_unavailable",
        )
    if generation.duration > settings.max_render_seconds:
        raise UnprocessableEntity(
            f"Recordings longer than {settings.max_render_seconds:.0f} seconds "
            "cannot be rendered",
            code="too_long_to_render",
        )

    size = (payload.size or settings.render_size).lower()
    if size not in RENDER_SIZES:
        raise UnprocessableEntity(
            f"Unsupported size {size!r}", code="unsupported_size"
        )
    width, height = RENDER_SIZES[size]

    media_ids = await _checked_media(settings, user, payload.media_ids)

    words = payload.words_per_cue or generation.words_per_cue
    existing = await run_in_threadpool(
        repo.find_reusable_render,
        settings.db_path,
        gen_id,
        fmt=payload.format,
        words_per_cue=words,
        width=width,
        height=height,
        fps=settings.render_fps,
        media_ids=json.dumps(media_ids),
    )
    if existing is not None:
        # Byte-identical output; charging for it again would be theft of quota.
        return existing.public(base=settings.root_path)

    # One in flight per user: renders are the most expensive thing here and a
    # queue of them from one account would starve everyone else.
    active = [
        r
        for r in await run_in_threadpool(
            repo.renders_for_generation, settings.db_path, gen_id
        )
        if r.status in ("queued", "running")
    ]
    if active:
        raise RateLimited(
            "A render is already running for this recording",
            code="already_rendering",
        )

    day = quota_day(settings.quota_tz)
    granted, used = await run_in_threadpool(
        repo.reserve_render_quota, settings.db_path, user.id, day, settings.daily_render_quota
    )
    if not granted:
        raise QuotaExceeded(
            "Daily render limit reached",
            code="render_quota_exceeded",
            headers={"Retry-After": str(max(1, day_reset_epoch(settings.quota_tz) - int(time.time())))},
        )

    render = Render(
        id=storage.new_render_id(),
        generation_id=gen_id,
        user_id=user.id,
        created_at=int(time.time()),
        started_at=0,
        finished_at=0,
        status="queued",
        error=None,
        format=payload.format,
        words_per_cue=words,
        width=width,
        height=height,
        fps=settings.render_fps,
        media_ids=tuple(media_ids),
    )
    await run_in_threadpool(repo.insert_render, settings.db_path, render)

    worker = getattr(request.app.state, "render_worker", None) if request else None
    if worker is not None:
        worker.notify()
    return render.public(base=settings.root_path)


@router.get("/api/generations/{gen_id}/renders")
async def list_renders(
    gen_id: str = Path(pattern=GENERATION_ID_RE),
    settings: Settings = Depends(get_settings),
    user: User = Depends(require_user),
):
    """Every render of one recording, newest first."""
    generation = await run_in_threadpool(
        repo.get_generation, settings.db_path, gen_id, user.id
    )
    if generation is None:
        raise NotFound("No such generation")
    items = await run_in_threadpool(repo.renders_for_generation, settings.db_path, gen_id)
    return {"items": [r.public(base=settings.root_path) for r in items]}


@router.get("/api/renders/{render_id}")
async def get_render(
    render_id: str = Path(pattern=GENERATION_ID_RE),
    settings: Settings = Depends(get_settings),
    user: User = Depends(require_user),
):
    """Status for polling."""
    render = await run_in_threadpool(
        repo.get_render, settings.db_path, render_id, user.id
    )
    if render is None:
        raise NotFound("No such render")
    return render.public(base=settings.root_path)


@router.get("/api/renders/{render_id}/video.{extension}")
async def get_render_video(
    render_id: str = Path(pattern=GENERATION_ID_RE),
    extension: str = Path(pattern=r"^(mp4|webm)$"),
    download: bool = Query(default=False),
    settings: Settings = Depends(get_settings),
    user: User = Depends(require_user),
):
    """Serve the finished file. ``FileResponse`` gives Range support."""
    render = await run_in_threadpool(
        repo.get_render, settings.db_path, render_id, user.id
    )
    if render is None or render.video_rel is None or render.status != "done":
        raise NotFound("No such render")
    if extension != render.format:
        # The extension is part of the URL for the browser's benefit; it still
        # has to match what was actually produced.
        raise NotFound("No such render")

    generation = await run_in_threadpool(
        repo.get_generation, settings.db_path, render.generation_id, user.id
    )
    title = generation.title if generation else "hebrew-voice"
    path = storage.resolve_under(settings.data_dir, render.video_rel)
    return FileResponse(
        path,
        media_type=_MEDIA_TYPES.get(render.format, "application/octet-stream"),
        headers={
            "Cache-Control": _IMMUTABLE,
            "Content-Disposition": content_disposition(
                title, render.format, attachment=download
            ),
        },
    )
