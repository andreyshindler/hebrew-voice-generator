"""Artifact storage: where audio and subtitle files live, and nothing else.

This is the only module that builds filesystem paths. Names come from a
server-generated id - **no part of a request ever reaches the filesystem** -
which is what keeps path traversal off the table entirely.
"""

from __future__ import annotations

import os
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from .errors import NotFound

__all__ = [
    "Artifacts",
    "new_generation_id",
    "GENERATION_ID_RE",
    "relative_paths",
    "write_artifacts",
    "resolve_under",
    "delete_files",
    "ensure_data_dir",
    "free_bytes",
]

#: Generation ids are 32 lowercase hex characters. Routes validate against this
#: before a handler ever runs.
GENERATION_ID_RE = r"^[0-9a-f]{32}$"
_ID_RE = re.compile(GENERATION_ID_RE)


@dataclass(frozen=True)
class Artifacts:
    """Paths of the files a generation produced, relative to the data dir."""

    audio_rel: str
    srt_rel: Optional[str] = None
    vtt_rel: Optional[str] = None
    #: Per-word cue timings, kept so subtitles can be re-rendered at another
    #: density later without re-synthesising. Never served directly.
    cues_rel: Optional[str] = None


def new_generation_id() -> str:
    """A fresh opaque id, used both as the primary key and the filename stem."""
    return secrets.token_hex(16)


def ensure_data_dir(data_dir: Path) -> None:
    """Create the data directory tree, readable only by the service user."""
    (data_dir / "audio").mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(data_dir, 0o700)
    except OSError:  # pragma: no cover - e.g. a mounted volume we don't own
        pass


def relative_paths(user_id: int, gen_id: str, *, when: Optional[float] = None) -> Artifacts:
    """Build the relative paths for a generation.

    Sharded by user and month so no single directory grows without bound.
    """
    if not _ID_RE.match(gen_id):
        raise ValueError("generation id must be 32 hex characters")
    stamp = time.gmtime(when if when is not None else time.time())
    prefix = f"audio/{user_id}/{stamp.tm_year:04d}/{stamp.tm_mon:02d}"
    return Artifacts(
        audio_rel=f"{prefix}/{gen_id}.mp3",
        srt_rel=f"{prefix}/{gen_id}.srt",
        vtt_rel=f"{prefix}/{gen_id}.vtt",
        cues_rel=f"{prefix}/{gen_id}.cues.json",
    )


def _atomic_write(path: Path, data: bytes) -> None:
    """Write via a temp file and rename, so a crash can't truncate a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def write_artifacts(
    data_dir: Path,
    artifacts: Artifacts,
    *,
    audio: bytes,
    srt: Optional[str] = None,
    vtt: Optional[str] = None,
    cues: Optional[str] = None,
) -> Artifacts:
    """Write the MP3 and any subtitles. Returns what was actually written."""
    _atomic_write(data_dir / artifacts.audio_rel, audio)
    srt_rel = None
    vtt_rel = None
    cues_rel = None
    if srt is not None and artifacts.srt_rel:
        _atomic_write(data_dir / artifacts.srt_rel, srt.encode("utf-8"))
        srt_rel = artifacts.srt_rel
    if vtt is not None and artifacts.vtt_rel:
        _atomic_write(data_dir / artifacts.vtt_rel, vtt.encode("utf-8"))
        vtt_rel = artifacts.vtt_rel
    if cues is not None and artifacts.cues_rel:
        _atomic_write(data_dir / artifacts.cues_rel, cues.encode("utf-8"))
        cues_rel = artifacts.cues_rel
    return Artifacts(
        audio_rel=artifacts.audio_rel,
        srt_rel=srt_rel,
        vtt_rel=vtt_rel,
        cues_rel=cues_rel,
    )


def resolve_under(root: Path, relative: str) -> Path:
    """Resolve ``relative`` inside ``root``, refusing anything that escapes.

    A second line of defence behind the id pattern and the ownership check:
    even a corrupted database row cannot make the app read outside the data
    directory.
    """
    root_resolved = root.resolve()
    candidate = (root_resolved / relative).resolve()
    if candidate != root_resolved and root_resolved not in candidate.parents:
        raise NotFound("artifact is outside the data directory")
    if not candidate.is_file():
        raise NotFound("artifact is missing")
    return candidate


def delete_files(data_dir: Path, relatives: Iterable[Optional[str]]) -> int:
    """Delete artifacts, tolerating ones that are already gone."""
    removed = 0
    for relative in relatives:
        if not relative:
            continue
        path = data_dir / relative
        try:
            path.unlink()
            removed += 1
        except FileNotFoundError:
            continue
        except OSError:
            continue
        # Prune now-empty month/year/user directories.
        for parent in list(path.parents):
            if parent == data_dir or data_dir not in parent.parents:
                break
            try:
                parent.rmdir()
            except OSError:
                break
    return removed


def free_bytes(path: Path) -> int:
    """Free space on the filesystem holding ``path``, for the disk warning."""
    stat = os.statvfs(path)
    return stat.f_bavail * stat.f_frsize
