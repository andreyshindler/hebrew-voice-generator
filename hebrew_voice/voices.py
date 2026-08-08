"""Hebrew voice catalog and name resolution."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

__all__ = [
    "HebrewVoice",
    "HEBREW_VOICES",
    "DEFAULT_VOICE",
    "resolve_voice",
    "is_known_voice",
    "catalog",
    "list_hebrew_voices",
    "list_hebrew_voices_sync",
]


@dataclass(frozen=True)
class HebrewVoice:
    """A Hebrew voice offered by the Edge TTS service."""

    short_name: str
    gender: str
    label: str
    description: str
    aliases: Tuple[str, ...] = ()

    @property
    def display(self) -> str:
        return f"{self.short_name} ({self.gender}, {self.label})"


#: The Hebrew (he-IL) voices the service exposes. Kept as a static catalog so
#: the web UI, the CLI, and ``--voice`` validation all work offline; use
#: :func:`list_hebrew_voices` to query the live list.
HEBREW_VOICES: List[HebrewVoice] = [
    HebrewVoice(
        "he-IL-HilaNeural",
        "Female",
        "הילה",
        "קול נשי, בהיר וברור",
        ("hila", "female", "f", "נקבה", "הילה"),
    ),
    HebrewVoice(
        "he-IL-AvriNeural",
        "Male",
        "אברי",
        "קול גברי, חם ורגוע",
        ("avri", "male", "m", "זכר", "אברי"),
    ),
]

DEFAULT_VOICE = "he-IL-HilaNeural"

_BY_NAME: Dict[str, HebrewVoice] = {v.short_name: v for v in HEBREW_VOICES}

_ALIASES: Dict[str, str] = {}
for _voice in HEBREW_VOICES:
    _ALIASES[_voice.short_name.lower()] = _voice.short_name
    for _alias in _voice.aliases:
        _ALIASES.setdefault(_alias.lower(), _voice.short_name)
del _voice, _alias


def is_known_voice(name: str) -> bool:
    """True if ``name`` is one of the catalogued Hebrew voices."""
    return name in _BY_NAME


def resolve_voice(name: Optional[str], *, allow_unknown: bool = True) -> str:
    """Turn a friendly name into a full voice short name.

    ``"hila"``, ``"female"``, ``"הילה"`` and ``"he-IL-HilaNeural"`` all resolve
    to ``he-IL-HilaNeural``.

    Args:
        name: A short name or alias. ``None`` gives :data:`DEFAULT_VOICE`.
        allow_unknown: When true (the CLI), any name containing a ``-`` is
            passed through so non-Hebrew voices still work. The web layer sets
            this to false so a request can't drive the service at an arbitrary
            locale.

    Raises:
        ValueError: If the name cannot be resolved.
    """
    if not name:
        return DEFAULT_VOICE
    key = name.strip()
    resolved = _ALIASES.get(key.lower())
    if resolved:
        return resolved
    if allow_unknown and "-" in key:  # e.g. en-US-AriaNeural
        return key
    known = ", ".join(sorted(_ALIASES))
    raise ValueError(f"unknown voice {name!r}; try one of: {known}")


def catalog() -> List[dict]:
    """JSON-safe voice list for the API and the page bootstrap."""
    return [
        {
            "id": v.short_name,
            "gender": v.gender.lower(),
            "label": v.label,
            "description": v.description,
            "default": v.short_name == DEFAULT_VOICE,
        }
        for v in HEBREW_VOICES
    ]


async def list_hebrew_voices(*, proxy: Optional[str] = None) -> List[dict]:
    """Fetch the live voice list and return only the Hebrew ones.

    Requires network access to the Edge TTS service.
    """
    import edge_tts

    voices = await edge_tts.list_voices(proxy=proxy)
    return [v for v in voices if str(v.get("Locale", "")).lower().startswith("he")]


def list_hebrew_voices_sync(*, proxy: Optional[str] = None) -> List[dict]:
    """Blocking wrapper around :func:`list_hebrew_voices`."""
    return asyncio.run(list_hebrew_voices(proxy=proxy))
