"""Transcription: turning an uploaded recording into a captioned recording.

The upload itself goes through the existing media endpoints - a recording is
just another file on the volume until someone asks for it to be transcribed.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Path, Request
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .. import repo, storage
from ..config import Settings
from ..errors import NotFound, QuotaExceeded, RateLimited, UnprocessableEntity
from ..models import Transcription, User
from ..quota import day_reset_epoch, quota_day
from ..storage import GENERATION_ID_RE
from .deps import get_settings, require_csrf, require_user, require_verified
from .transcribing import MAX_PROVIDER_BYTES, SENDABLE

router = APIRouter(
    prefix="/api/transcriptions",
    tags=["transcriptions"],
    dependencies=[Depends(require_user), Depends(require_verified)],
)


class TranscribeRequest(BaseModel):
    """Which uploaded file to transcribe."""

    media_id: str = Field(min_length=1, max_length=64)


@router.post("", status_code=202, dependencies=[Depends(require_csrf)])
async def create_transcription(
    payload: TranscribeRequest,
    request: Request,
    settings: Settings = Depends(get_settings),
    user: User = Depends(require_user),
):
    """Queue a transcription and return the job to poll."""
    if not settings.transcription_enabled:
        raise UnprocessableEntity(
            "Transcription is not configured on this server",
            code="transcription_disabled",
        )

    media = await run_in_threadpool(
        repo.get_media, settings.db_path, payload.media_id, user.id
    )
    if media is None:
        raise UnprocessableEntity(
            "That upload is not available", code="media_unavailable"
        )
    if media.kind == "image":
        raise UnprocessableEntity(
            "An image has no speech to transcribe", code="unsupported_for_transcription"
        )

    ext = media.rel.rsplit(".", 1)[-1].lower() if "." in media.rel else ""
    if ext not in SENDABLE:
        # Refused here rather than three minutes into a job, because nothing
        # about the wait would make the answer different.
        raise UnprocessableEntity(
            "That file type cannot be transcribed - upload MP3, M4A, WAV, MP4 or WebM",
            code="unsupported_for_transcription",
        )
    if media.bytes > MAX_PROVIDER_BYTES:
        raise UnprocessableEntity(
            f"The recording is larger than the {MAX_PROVIDER_BYTES // (1024 * 1024)}MB "
            "the transcription service accepts",
            code="recording_too_large",
        )

    # What the browser measured. It only sizes the reservation - the provider
    # says how long the recording really was, and the charge is corrected then.
    seconds = media.duration_ms / 1000.0
    if seconds > settings.max_transcribe_seconds:
        raise UnprocessableEntity(
            f"Recordings longer than {settings.max_transcribe_seconds:.0f}s "
            "cannot be transcribed",
            code="too_long_to_transcribe",
        )

    # One at a time per account. The queue is shared, and a backlog from one
    # account would starve everyone else.
    if await run_in_threadpool(repo.has_active_transcription, settings.db_path, user.id):
        raise RateLimited(
            "A transcription is already running", code="already_transcribing"
        )

    day = quota_day(settings.quota_tz)
    # An unmeasured upload still has to cost something, or a browser that
    # failed to read the duration would transcribe for free.
    charge = max(1, int(round(seconds)))
    granted, _used = await run_in_threadpool(
        repo.reserve_transcription_quota,
        settings.db_path,
        user.id,
        day,
        charge,
        settings.daily_transcribe_seconds,
    )
    if not granted:
        raise QuotaExceeded(
            "Daily transcription limit reached",
            code="transcription_quota_exceeded",
            headers={
                "Retry-After": str(
                    max(1, day_reset_epoch(settings.quota_tz) - int(time.time()))
                )
            },
        )

    job = Transcription(
        id=storage.new_render_id(),
        user_id=user.id,
        media_id=media.id,
        generation_id=None,
        created_at=int(time.time()),
        started_at=None,
        finished_at=None,
        status="queued",
        error=None,
        seconds=float(charge),
    )
    await run_in_threadpool(repo.insert_transcription, settings.db_path, job)

    worker = getattr(request.app.state, "transcribe_worker", None)
    if worker is not None:
        worker.notify()
    return job.public()


@router.get("/{job_id}")
async def get_transcription(
    job_id: str = Path(pattern=GENERATION_ID_RE),
    settings: Settings = Depends(get_settings),
    user: User = Depends(require_user),
):
    """Status for polling."""
    job = await run_in_threadpool(repo.get_transcription, settings.db_path, job_id, user.id)
    if job is None:
        raise NotFound("No such transcription")
    return job.public()
