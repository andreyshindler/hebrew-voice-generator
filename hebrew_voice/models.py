"""Row objects returned by :mod:`hebrew_voice.repo`."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Dict, Optional

__all__ = ["User", "Session", "Generation", "UsageDay"]


@dataclass(frozen=True)
class User:
    id: int
    email: str
    password_hash: str
    created_at: int
    is_active: bool
    is_admin: bool
    invite_code: Optional[str]
    daily_char_quota: Optional[int]
    failed_logins: int
    locked_until: int
    email_verified_at: int = 0

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "User":
        return cls(
            id=row["id"],
            email=row["email"],
            password_hash=row["password_hash"],
            created_at=row["created_at"],
            is_active=bool(row["is_active"]),
            is_admin=bool(row["is_admin"]),
            invite_code=row["invite_code"],
            daily_char_quota=row["daily_char_quota"],
            failed_logins=row["failed_logins"],
            locked_until=row["locked_until"],
            email_verified_at=row["email_verified_at"],
        )

    @property
    def is_verified(self) -> bool:
        return self.email_verified_at > 0

    def public(self) -> Dict[str, Any]:
        """The shape the API hands to the browser - never the hash."""
        return {
            "id": self.id,
            "email": self.email,
            "is_admin": self.is_admin,
            "created_at": self.created_at,
            "email_verified": self.is_verified,
        }


@dataclass(frozen=True)
class Session:
    id: str
    user_id: int
    csrf_token: str
    created_at: int
    last_seen_at: int
    expires_at: int
    user_agent: Optional[str]
    ip: Optional[str]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Session":
        return cls(
            id=row["id"],
            user_id=row["user_id"],
            csrf_token=row["csrf_token"],
            created_at=row["created_at"],
            last_seen_at=row["last_seen_at"],
            expires_at=row["expires_at"],
            user_agent=row["user_agent"],
            ip=row["ip"],
        )


@dataclass(frozen=True)
class Generation:
    id: str
    user_id: int
    created_at: int
    title: str
    text_raw: str
    text_prepared: str
    char_count: int
    voice: str
    rate: int
    pitch: int
    volume: int
    keep_niqqud: bool
    expand_symbols: bool
    expand_abbreviations: bool
    expand_acronyms: bool
    audio_rel: Optional[str]
    srt_rel: Optional[str]
    vtt_rel: Optional[str]
    audio_bytes: int
    duration_ms: int
    cue_count: int
    #: Word-level cue timings on disk. Absent on rows written before the
    #: density control existed, and on generations made without subtitles.
    cues_rel: Optional[str] = None
    #: Words per cue in the stored subtitle files.
    words_per_cue: int = 7

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Generation":
        return cls(
            id=row["id"],
            user_id=row["user_id"],
            created_at=row["created_at"],
            title=row["title"],
            text_raw=row["text_raw"],
            text_prepared=row["text_prepared"],
            char_count=row["char_count"],
            voice=row["voice"],
            rate=row["rate"],
            pitch=row["pitch"],
            volume=row["volume"],
            keep_niqqud=bool(row["keep_niqqud"]),
            expand_symbols=bool(row["expand_symbols"]),
            expand_abbreviations=bool(row["expand_abbreviations"]),
            expand_acronyms=bool(row["expand_acronyms"]),
            audio_rel=row["audio_rel"],
            srt_rel=row["srt_rel"],
            vtt_rel=row["vtt_rel"],
            audio_bytes=row["audio_bytes"],
            duration_ms=row["duration_ms"],
            cue_count=row["cue_count"],
            cues_rel=row["cues_rel"],
            words_per_cue=row["words_per_cue"],
        )

    @property
    def duration(self) -> float:
        return self.duration_ms / 1000.0

    def public(self, *, full: bool = False, base: str = "") -> Dict[str, Any]:
        """JSON shape for the API. ``full`` adds the text, for replay.

        ``base`` is the app's URL prefix (``/voice-gen``). These URLs go
        straight into ``<audio src>``, the ``<track>`` element, and the download
        links, so they have to be correct for the deployment, not just for the
        API.
        """
        data: Dict[str, Any] = {
            "id": self.id,
            "created_at": self.created_at,
            "title": self.title,
            "char_count": self.char_count,
            "voice": self.voice,
            "rate": self.rate,
            "pitch": self.pitch,
            "volume": self.volume,
            "duration": self.duration,
            "audio_bytes": self.audio_bytes,
            "cue_count": self.cue_count,
            # Whether the word timings were kept, and so whether the subtitle
            # endpoints will honour ?words=. False for anything made before
            # the density control existed - the UI hides the control rather
            # than offering one that can't work.
            "can_regroup": self.cues_rel is not None,
            # The density the stored files were rendered at, so the result
            # card opens showing what is actually on disk.
            "words_per_cue": self.words_per_cue,
            "urls": {
                "audio": f"{base}/api/generations/{self.id}/audio.mp3",
                "srt": f"{base}/api/generations/{self.id}/subtitles.srt" if self.srt_rel else None,
                "vtt": f"{base}/api/generations/{self.id}/subtitles.vtt" if self.vtt_rel else None,
            },
        }
        if full:
            data.update(
                {
                    "text": self.text_raw,
                    "prepared_text": self.text_prepared,
                    "options": {
                        "keep_niqqud": self.keep_niqqud,
                        "expand_symbols": self.expand_symbols,
                        "expand_abbreviations": self.expand_abbreviations,
                        "expand_acronyms": self.expand_acronyms,
                    },
                }
            )
        return data


@dataclass(frozen=True)
class UsageDay:
    user_id: int
    day: str
    chars: int
    requests: int
