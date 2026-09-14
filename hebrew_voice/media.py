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

__all__ = ["MediaType", "sniff", "mime_for_ext", "MEDIA_KINDS", "HEADER_BYTES"]

#: Enough of the file to identify every type below.
#:
#: The magic numbers all live in the first dozen bytes, but telling an
#: audio-only WebM or MP4 from one with pictures needs the track list, which
#: sits further in - measured at byte 220 for a browser's Opus recording and
#: 344 for its MP4. 4KB is comfortable room for both without reading a whole
#: upload to decide.
HEADER_BYTES = 4096

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
_AUDIO_WEBM = MediaType("audio", "webm", "audio/webm")
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

#: Matroska CodecIDs, stored as plain ASCII in the track list near the front of
#: the file. A browser recording the microphone writes a WebM whose only track
#: is A_OPUS - the same container a screen recording uses, so the magic number
#: alone cannot tell them apart.
_MATROSKA_VIDEO = (b"V_VP8", b"V_VP9", b"V_AV1", b"V_MPEG4", b"V_MPEGH", b"V_THEORA")
_MATROSKA_AUDIO = (b"A_OPUS", b"A_VORBIS", b"A_AAC", b"A_MPEG", b"A_FLAC", b"A_PCM")


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

    # Matroska/WebM share the EBML magic; Chromium plays both. Which it is
    # depends on the tracks, not the container - see _tracks_are_audio_only.
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return _AUDIO_WEBM if _matroska_is_audio_only(head) else _VIDEO_WEBM

    # ISO base media: a `ftyp` box at offset 4, brand at 8.
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in _AUDIO_BRANDS:
            return _AUDIO_M4A
        if brand in _QUICKTIME_BRANDS:
            return _VIDEO_MOV
        # A brand of `isom` covers both a film and a voice memo. Safari records
        # the microphone to exactly this.
        return _AUDIO_M4A if _iso_is_audio_only(head) else _VIDEO_MP4

    return None


def _matroska_is_audio_only(head: bytes) -> bool:
    """Whether a WebM's track list mentions sound and never pictures.

    Positive evidence only: a file whose tracks are not in the window we read
    is left as video, which is what it was before this existed. Guessing the
    other way would drop a real clip out of the shot list.
    """
    if any(codec in head for codec in _MATROSKA_VIDEO):
        return False
    return any(codec in head for codec in _MATROSKA_AUDIO)


def _iso_is_audio_only(head: bytes) -> bool:
    """The same question for an MP4, read off its `hdlr` boxes.

    The handler type sits twelve bytes past the box name - four each for the
    version and flags, the reserved field, and then the type itself. Read at
    that offset rather than searching for "soun" loose in the bytes, which
    would also match the middle of a codec name or a chunk of audio.

    Many real videos put their `moov` at the end of the file, so neither
    handler appears here at all: those stay video, as they did before.
    """
    seen_audio = False
    at = head.find(b"hdlr")
    while at != -1:
        handler = head[at + 12 : at + 16]
        if handler == b"vide":
            return False
        if handler == b"soun":
            seen_audio = True
        at = head.find(b"hdlr", at + 4)
    return seen_audio


#: Every extension this module will ever store, by name. Serving a file needs
#: the reverse of sniffing: the bytes were identified once, on upload, and what
#: is left on disk is the extension we chose.
#: ``.webm`` names both an audio-only recording and a film, so this mapping is
#: ambiguous by construction. The audio entries come last, and so win, because
#: the only caller is serving a recording's audio track - where "audio/webm" is
#: right by definition and "video/webm" would be a guess about a file we
#: already know the role of.
_BY_EXT = {
    t.ext: t
    for t in (
        _IMAGE_JPEG, _IMAGE_PNG, _IMAGE_GIF, _IMAGE_WEBP,
        _VIDEO_MP4, _VIDEO_WEBM, _VIDEO_MOV,
        _AUDIO_MP3, _AUDIO_M4A, _AUDIO_WAV, _AUDIO_OGG, _AUDIO_WEBM,
    )
}


def mime_for_ext(ext: str) -> str:
    """The content type to serve a recording's audio under.

    Falls back to MP3, which is what every generation before transcription
    existed was, and what an unknown extension is most likely to be.
    """
    found = _BY_EXT.get(ext.lstrip(".").lower())
    return found.mime if found else "audio/mpeg"
