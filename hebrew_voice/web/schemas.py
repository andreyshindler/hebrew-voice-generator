"""Request and response models for the JSON API."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, field_validator

from ..synth import MAX_WORDS_PER_CUE, READABLE_WORDS_PER_CUE

__all__ = [
    "SignupRequest",
    "LoginRequest",
    "ResendRequest",
    "CleanupOptions",
    "PreviewRequest",
    "SynthesizeRequest",
]

#: Deliberately permissive - the goal is to catch typos, not to police the
#: RFC. Deliverability is proven by the invite code, not by a regex.
_EMAIL_RE = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"

MIN_PASSWORD_LENGTH = 10


class CleanupOptions(BaseModel):
    """The Hebrew text-preparation switches exposed in the advanced panel."""

    keep_niqqud: bool = False
    expand_symbols: bool = True
    expand_abbreviations: bool = True
    expand_acronyms: bool = True


class SignupRequest(BaseModel):
    email: str = Field(pattern=_EMAIL_RE, max_length=254)
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=1024)
    invite_code: str = Field(default="", max_length=200)


class LoginRequest(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=1024)


class ResendRequest(BaseModel):
    email: str = Field(max_length=254)


class PreviewRequest(CleanupOptions):
    text: str = ""
    rate: int = Field(default=0, ge=-50, le=100)


class SynthesizeRequest(CleanupOptions):
    """A synthesis request.

    Rate, pitch and volume arrive as plain numbers with real bounds; the server
    formats them into edge-tts's ``+10%`` / ``-5Hz`` wire strings, so the client
    never has to know that format exists.
    """

    text: str
    voice: Optional[str] = None
    rate: int = Field(default=0, ge=-50, le=100)
    pitch: int = Field(default=0, ge=-50, le=50)
    volume: int = Field(default=0, ge=-50, le=50)
    subtitles: bool = True
    #: Words per subtitle cue in the stored files. 1 is the word-by-word
    #: "karaoke" style video editors want. Only the *initial* density: the word
    #: timings are kept, so the subtitle endpoints can re-render at any other
    #: value afterwards.
    #:
    #: The web UI never sends this - it takes the default and lets the result
    #: card re-render, so the density on disk is the one the plain (cacheable)
    #: subtitle URLs serve. API and CLI callers can still choose up front.
    words_per_cue: int = Field(
        default=READABLE_WORDS_PER_CUE, ge=1, le=MAX_WORDS_PER_CUE
    )

    @field_validator("text")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("text must not be empty")
        return value
