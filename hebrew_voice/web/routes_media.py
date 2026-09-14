"""Uploaded photos and clips: taking them in, listing, serving, deleting."""

from __future__ import annotations

import time
from dataclasses import replace

from fastapi import APIRouter, Depends, File, Form, Path, Query, Response, UploadFile
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from .. import media as media_types
from .. import repo, storage
from ..config import Settings
from ..errors import NotFound, PayloadTooLarge, QuotaExceeded, UnprocessableEntity
from ..models import Media, User
from ..storage import GENERATION_ID_RE
from .deps import get_settings, require_csrf, require_user, require_verified

router = APIRouter(
    prefix="/api/media",
    tags=["media"],
    dependencies=[Depends(require_user), Depends(require_verified)],
)

#: Ids are opaque, so a stored file never changes under its URL.
_IMMUTABLE = "private, max-age=31536000, immutable"

#: Read the upload in chunks and stop the moment it exceeds the cap. Reading it
#: whole and measuring afterwards would mean a 4GB request could still cost
#: 4GB of memory before being refused.
_CHUNK = 1 << 20


async def _read_capped(upload: UploadFile, limit: int) -> bytes:
    parts = []
    total = 0
    while True:
        chunk = await upload.read(_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise PayloadTooLarge(
                f"Files are limited to {limit // (1 << 20)} MB",
                code="upload_too_large",
            )
        parts.append(chunk)
    return b"".join(parts)


@router.post("", status_code=201, dependencies=[Depends(require_csrf)])
async def upload_media(
    file: UploadFile = File(...),
    # What the browser measured off the file before sending it. We have no
    # media tools in this image, so there is nothing to check it against - it
    # only ever influences layout.
    duration: float = Form(default=0.0),
    settings: Settings = Depends(get_settings),
    user: User = Depends(require_user),
):
    """Store one photo, clip or recording for this account.

    The same endpoint backs three things: the shots behind the captions, the
    background music, and the recordings sent for transcription. What a file is
    for is decided later, by whoever asks for it - here it is only sniffed,
    measured and stored.
    """
    data = await _read_capped(file, settings.max_upload_bytes)
    if not data:
        raise UnprocessableEntity("The file is empty", code="empty_upload")

    # The name and the browser's content type are both the client's to invent,
    # so neither decides anything: the first bytes do.
    kind = media_types.sniff(data[: media_types.HEADER_BYTES])
    if kind is None:
        raise UnprocessableEntity(
            "Only JPEG, PNG, GIF or WebP images, MP4, WebM or MOV video, "
            "and MP3, M4A, WAV or OGG audio can be used",
            code="unsupported_media",
        )

    used = await run_in_threadpool(repo.media_bytes_used, settings.db_path, user.id)
    if used + len(data) > settings.media_quota_bytes:
        raise QuotaExceeded(
            f"That would exceed your {settings.media_quota_bytes // (1 << 20)} MB "
            "of storage. Delete something first.",
            code="media_quota_exceeded",
        )

    item = Media(
        id=storage.new_media_id(),
        user_id=user.id,
        created_at=int(time.time()),
        kind=kind.kind,
        mime=kind.mime,
        rel="",
        bytes=len(data),
        duration_ms=max(0, min(int(duration * 1000), 24 * 3600 * 1000)),
        original_name=(file.filename or "")[:120],
    )
    item = replace(item, rel=storage.media_relative_path(user.id, item.id, kind.ext))

    await run_in_threadpool(storage.write_bytes, settings.data_dir, item.rel, data)
    await run_in_threadpool(repo.insert_media, settings.db_path, item)
    return item.public(base=settings.root_path)


@router.get("")
async def list_media(
    settings: Settings = Depends(get_settings),
    user: User = Depends(require_user),
    limit: int = Query(default=100, ge=1, le=200),
):
    """This account's uploads, newest first, with what they cost."""
    items = await run_in_threadpool(repo.list_media, settings.db_path, user.id, limit)
    used = await run_in_threadpool(repo.media_bytes_used, settings.db_path, user.id)
    return {
        "items": [m.public(base=settings.root_path) for m in items],
        "bytes_used": used,
        "bytes_quota": settings.media_quota_bytes,
    }


@router.get("/{media_id}/file")
async def get_media_file(
    media_id: str = Path(pattern=GENERATION_ID_RE),
    settings: Settings = Depends(get_settings),
    user: User = Depends(require_user),
):
    """Serve an upload back to its owner, for the thumbnail strip."""
    item = await run_in_threadpool(repo.get_media, settings.db_path, media_id, user.id)
    if item is None:
        raise NotFound("No such file")
    path = storage.resolve_under(settings.data_dir, item.rel)
    return FileResponse(
        path,
        media_type=item.mime,
        headers={
            "Cache-Control": _IMMUTABLE,
            # Never let a stored upload be interpreted as a document, whatever
            # the browser makes of its bytes.
            "Content-Disposition": "inline",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.delete("/{media_id}", status_code=204, dependencies=[Depends(require_csrf)])
async def delete_media(
    media_id: str = Path(pattern=GENERATION_ID_RE),
    settings: Settings = Depends(get_settings),
    user: User = Depends(require_user),
):
    """Remove an upload and its file.

    Renders that already used it keep their finished video - the composition
    was baked long ago - but cannot be reproduced, which is the honest
    outcome of deleting a source.
    """
    rel = await run_in_threadpool(repo.delete_media, settings.db_path, media_id, user.id)
    if rel is None:
        raise NotFound("No such file")
    await run_in_threadpool(storage.delete_files, settings.data_dir, (rel,))
    return Response(status_code=204)
