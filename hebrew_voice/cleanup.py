"""Retention sweep: keep history bounded so a VPS disk stays predictable."""

from __future__ import annotations

import logging
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from . import repo, storage
from .config import Settings

__all__ = ["SweepResult", "sweep"]

log = logging.getLogger("hebrew_voice.cleanup")


@dataclass
class SweepResult:
    generations_deleted: int = 0
    files_deleted: int = 0
    sessions_purged: int = 0
    tokens_purged: int = 0
    workdirs_removed: int = 0

    def __str__(self) -> str:
        return (
            f"{self.generations_deleted} generations, {self.files_deleted} files, "
            f"{self.workdirs_removed} render workdirs, "
            f"{self.sessions_purged} expired sessions, {self.tokens_purged} email tokens"
        )


def sweep(settings: Settings, *, dry_run: bool = False) -> SweepResult:
    """Delete history past the keep-N / max-age policy, and expired sessions.

    Rows and files go together: the row is removed only after its files are,
    so a crash mid-sweep leaves orphaned files (harmless, retried next sweep)
    rather than history pointing at nothing.
    """
    result = SweepResult()
    doomed = repo.expired_generations(
        settings.db_path,
        keep_per_user=settings.history_keep,
        max_age_days=settings.history_max_age_days,
    )
    for generation in doomed:
        if dry_run:
            result.generations_deleted += 1
            continue
        # Render rows go with the generation via ON DELETE CASCADE, so their
        # video paths are read before the row is removed, not after.
        videos = repo.render_video_paths(settings.db_path, [generation.id])
        result.files_deleted += storage.delete_files(
            settings.data_dir,
            (
                generation.audio_rel,
                generation.srt_rel,
                generation.vtt_rel,
                generation.cues_rel,
                *videos,
            ),
        )
        if repo.delete_generation(settings.db_path, generation.id):
            result.generations_deleted += 1

    if not dry_run:
        result.workdirs_removed = _sweep_workdirs(settings)
        result.sessions_purged = repo.purge_expired_sessions(settings.db_path)
        result.tokens_purged = repo.purge_expired_tokens(settings.db_path)

    if (
        result.generations_deleted
        or result.sessions_purged
        or result.tokens_purged
        or result.workdirs_removed
    ):
        log.info("retention sweep removed %s", result)
    _warn_on_low_disk(settings.data_dir)
    return result


def _sweep_workdirs(settings: Settings, *, max_age_seconds: int = 2 * 3600) -> int:
    """Delete render scratch directories nothing is using any more.

    A render removes its own on the way out, so these are the leavings of a
    process that died mid-render - and they hold hard links to the uploads,
    which means the disk is not returned when the originals are deleted. The
    age floor is well past any render timeout, so a running render is never
    swept out from under itself.
    """
    work = settings.data_dir / "work"
    if not work.is_dir():
        return 0
    cutoff = time.time() - max_age_seconds
    removed = 0
    for entry in work.iterdir():
        if not entry.is_dir():
            continue
        try:
            if entry.stat().st_mtime > cutoff:
                continue
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1
        except OSError:
            continue
    return removed


def _warn_on_low_disk(data_dir: Path, *, threshold: int = 1 << 30) -> None:
    try:
        free = storage.free_bytes(data_dir)
    except OSError:  # pragma: no cover - unusual filesystems
        return
    if free < threshold:
        log.warning("low disk space: %.1f GB free under %s", free / (1 << 30), data_dir)
