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

MEDIA_KINDS = ("image", "video")


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

#: MP4 brands that are really QuickTime. Both play in Chromium; they differ
#: only in what we call the file.
_QUICKTIME_BRANDS = (b"qt  ",)


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

    # Matroska/WebM share the EBML magic; Chromium plays both.
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return _VIDEO_WEBM

    # ISO base media: a `ftyp` box at offset 4, brand at 8.
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in _QUICKTIME_BRANDS:
            return _VIDEO_MOV
        return _VIDEO_MP4

    return None
