"""What an uploaded file actually is.

The filename and the browser's Content-Type are both attacker-controlled, so
neither decides anything here: the type comes from the first bytes of the file.
That is what stops a script being stored as ``.jpg`` and served back, and what
keeps anything the renderer's Chromium is pointed at to a known short list.

Pure stdlib on purpose - the app image has no compiler and no media tools, and
this is the wrong place to acquire either.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

__all__ = ["MediaType", "sniff", "MEDIA_KINDS", "HEADER_BYTES"]

#: Enough of the file to identify every type below. MP4's `ftyp` box sits at
#: offset 4 and its brand runs to 12.
HEADER_BYTES = 32

MEDIA_KINDS = ("image", "video", "audio")


@dataclass(frozen=True)
class MediaType:
    kind: str
    #: Extension we store it under. Never taken from the uploaded name.
    ext: str
    mime: str


_IMAGE_JPEG = MediaType("image", "jpg", "image/jpeg")
_IMAGE_PNG = MediaType("image", "png", "image/png")
_IMAGE_GIF = MediaType("image", "gif", "image/gif")
_IMAGE_WEBP = MediaType("image", "webp", "image/webp")
_VIDEO_MP4 = MediaType("video", "mp4", "video/mp4")
_VIDEO_WEBM = MediaType("video", "webm", "video/webm")
_VIDEO_MOV = MediaType("video", "mov", "video/quicktime")
_AUDIO_MP3 = MediaType("audio", "mp3", "audio/mpeg")
_AUDIO_M4A = MediaType("audio", "m4a", "audio/mp4")
_AUDIO_WAV = MediaType("audio", "wav", "audio/wav")
_AUDIO_OGG = MediaType("audio", "ogg", "audio/ogg")

#: MP4 brands that are really QuickTime. Both play in Chromium; they differ
#: only in what we call the file.
_QUICKTIME_BRANDS = (b"qt  ",)

#: ISO base media brands that carry audio rather than video. Same container as
#: an MP4, so only the brand tells them apart - and calling a music file a
#: video would put it in the shot list instead of under the narration.
_AUDIO_BRANDS = (b"M4A ", b"M4B ", b"M4P ")


def sniff(head: bytes) -> Optional[MediaType]:
    """Identify an upload from its leading bytes, or None if unrecognised.

    Deliberately strict: anything not on this list is refused rather than
    stored and hoped about.
    """
    if len(head) < 12:
        return None

    if head[:3] == b"\xff\xd8\xff":
        return _IMAGE_JPEG
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return _IMAGE_PNG
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return _IMAGE_GIF
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return _IMAGE_WEBP
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return _AUDIO_WAV

    if head[:3] == b"ID3":
        return _AUDIO_MP3
    # A bare MP3 with no tag starts at a frame sync: eleven set bits.
    if head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return _AUDIO_MP3
    if head[:4] == b"OggS":
        return _AUDIO_OGG

    # Matroska/WebM share the EBML magic; Chromium plays both.
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return _VIDEO_WEBM

    # ISO base media: a `ftyp` box at offset 4, brand at 8.
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in _AUDIO_BRANDS:
            return _AUDIO_M4A
        if brand in _QUICKTIME_BRANDS:
            return _VIDEO_MOV
        return _VIDEO_MP4

    return None
