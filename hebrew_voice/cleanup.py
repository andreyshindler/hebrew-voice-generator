"""Retention sweep: keep history bounded so a VPS disk stays predictable."""

from __future__ import annotations

import logging
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

    def __str__(self) -> str:
        return (
            f"{self.generations_deleted} generations, {self.files_deleted} files, "
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
        result.files_deleted += storage.delete_files(
            settings.data_dir,
            (
                generation.audio_rel,
                generation.srt_rel,
                generation.vtt_rel,
                generation.cues_rel,
            ),
        )
        if repo.delete_generation(settings.db_path, generation.id):
            result.generations_deleted += 1

    if not dry_run:
        result.sessions_purged = repo.purge_expired_sessions(settings.db_path)
        result.tokens_purged = repo.purge_expired_tokens(settings.db_path)

    if result.generations_deleted or result.sessions_purged or result.tokens_purged:
        log.info("retention sweep removed %s", result)
    _warn_on_low_disk(settings.data_dir)
    return result


def _warn_on_low_disk(data_dir: Path, *, threshold: int = 1 << 30) -> None:
    try:
        free = storage.free_bytes(data_dir)
    except OSError:  # pragma: no cover - unusual filesystems
        return
    if free < threshold:
        log.warning("low disk space: %.1f GB free under %s", free / (1 << 30), data_dir)
