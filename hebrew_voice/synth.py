"""Speech synthesis on top of edge-tts, tuned for Hebrew."""

from __future__ import annotations

import asyncio
import inspect
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Iterable, List, Optional, Sequence, Tuple, Union

from . import text as text_utils
from .voices import DEFAULT_VOICE, resolve_voice

__all__ = [
    "SynthesisOptions",
    "Cue",
    "Result",
    "synthesize",
    "synthesize_sync",
    "synthesize_split",
    "synthesize_batch",
    "to_srt",
    "to_vtt",
    "merge_cues",
    "group_cues",
    "dump_cues",
    "load_cues",
    "READABLE_WORDS_PER_CUE",
    "MAX_WORDS_PER_CUE",
]

ProgressFn = Callable[[str], None]

_PERCENT_RE = re.compile(r"^([+-]?\d+(?:\.\d+)?)%?$")
_HZ_RE = re.compile(r"^([+-]?\d+(?:\.\d+)?)(?:hz)?$", re.IGNORECASE)
_ST_RE = re.compile(r"^([+-]?\d+(?:\.\d+)?)st$", re.IGNORECASE)

#: Offsets from edge-tts are in 100-nanosecond ticks.
TICKS_PER_SECOND = 10_000_000


def _normalize_percent(value: Union[str, int, float], name: str) -> str:
    """``10`` / ``"10"`` / ``"+10%"`` -> ``"+10%"``."""
    if isinstance(value, (int, float)):
        value = f"{value:+g}%"
    match = _PERCENT_RE.match(str(value).strip())
    if not match:
        raise ValueError(f"{name} must look like '+10%' or '-5%', got {value!r}")
    return f"{float(match.group(1)):+g}%"


def _normalize_pitch(value: Union[str, int, float]) -> str:
    """``5`` / ``"5Hz"`` / ``"+5hz"`` -> ``"+5Hz"``. Semitones are accepted too."""
    if isinstance(value, (int, float)):
        value = f"{value:+g}Hz"
    raw = str(value).strip()
    if semitones := _ST_RE.match(raw):
        return f"{float(semitones.group(1)):+g}st"
    match = _HZ_RE.match(raw)
    if not match:
        raise ValueError(f"pitch must look like '+5Hz' or '-10Hz', got {value!r}")
    return f"{float(match.group(1)):+g}Hz"


@dataclass
class SynthesisOptions:
    """Everything that shapes how a line is spoken."""

    voice: str = DEFAULT_VOICE
    rate: str = "+0%"
    volume: str = "+0%"
    pitch: str = "+0Hz"
    boundary: str = "WordBoundary"
    #: Text-preparation switches, passed through to :func:`hebrew_voice.text.prepare`.
    keep_niqqud: bool = False
    expand_symbols: bool = True
    expand_abbreviations: bool = True
    expand_acronyms: bool = True
    #: Transport
    proxy: Optional[str] = None
    retries: int = 3
    retry_backoff: float = 2.0
    connect_timeout: int = 10
    receive_timeout: int = 60

    def __post_init__(self) -> None:
        self.voice = resolve_voice(self.voice)
        self.rate = _normalize_percent(self.rate, "rate")
        self.volume = _normalize_percent(self.volume, "volume")
        self.pitch = _normalize_pitch(self.pitch)
        if self.boundary not in ("WordBoundary", "SentenceBoundary"):
            raise ValueError("boundary must be 'WordBoundary' or 'SentenceBoundary'")
        if self.retries < 1:
            raise ValueError("retries must be >= 1")

    @classmethod
    def from_numbers(
        cls,
        voice: str,
        *,
        rate: int = 0,
        pitch: int = 0,
        volume: int = 0,
        **flags,
    ) -> "SynthesisOptions":
        """Build options from plain numbers, e.g. from a web form or sliders.

        Callers never have to know that the wire format is ``"+10%"``/``"-5Hz"``.
        """
        return cls(
            voice=voice,
            rate=f"{rate:+d}%",
            pitch=f"{pitch:+d}Hz",
            volume=f"{volume:+d}%",
            **flags,
        )

    def prepare_text(self, raw: str) -> str:
        """Apply the Hebrew cleanup pipeline configured on these options."""
        return text_utils.prepare(
            raw,
            keep_niqqud=self.keep_niqqud,
            symbols=self.expand_symbols,
            abbreviations=self.expand_abbreviations,
            acronyms=self.expand_acronyms,
        )


@dataclass
class Cue:
    """A timed subtitle cue."""

    start: float  # seconds
    end: float  # seconds
    text: str

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def shifted(self, offset: float) -> "Cue":
        return Cue(self.start + offset, self.end + offset, self.text)


@dataclass
class Result:
    """What a synthesis run produced."""

    path: Optional[Path]
    audio: bytes = b""
    cues: List[Cue] = field(default_factory=list)
    word_cues: List[Cue] = field(default_factory=list)
    text: str = ""

    @property
    def audio_bytes(self) -> int:
        """Size of the MP3. Zero when the audio was written but not retained."""
        return len(self.audio)

    @property
    def duration(self) -> float:
        """Spoken duration in seconds, as reported by the boundary events.

        This is the end of the last cue, so it excludes any trailing silence in
        the MP3 - treat it as an estimate and prefer the player's own duration
        once the audio has loaded.
        """
        return self.cues[-1].end if self.cues else 0.0


def _format_timestamp(seconds: float, *, sep: str = ",") -> str:
    if seconds < 0 or math.isnan(seconds):
        seconds = 0.0
    millis = int(round(seconds * 1000))
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{sep}{millis:03d}"


def merge_cues(
    cues: Sequence[Cue],
    *,
    max_words: int = 7,
    max_chars: int = 42,
    max_gap: float = 0.6,
) -> List[Cue]:
    """Merge word-level cues into readable subtitle lines.

    Args:
        cues: Word (or sentence) cues in order.
        max_words: Most words allowed in one cue.
        max_chars: Soft character limit per cue.
        max_gap: Start a new cue when the silence between words exceeds this.
    """
    merged: List[Cue] = []
    for cue in cues:
        if not merged:
            merged.append(Cue(cue.start, cue.end, cue.text))
            continue
        last = merged[-1]
        candidate = f"{last.text} {cue.text}"
        too_long = len(candidate) > max_chars or len(candidate.split()) > max_words
        long_pause = cue.start - last.end > max_gap
        ends_sentence = last.text.endswith((".", "!", "?", ":"))
        if too_long or long_pause or ends_sentence:
            merged.append(Cue(cue.start, cue.end, cue.text))
        else:
            merged[-1] = Cue(last.start, cue.end, candidate)
    return merged


#: The default density: comfortable reading lines, and what every generation
#: was rendered at before the density control existed.
READABLE_WORDS_PER_CUE = 7
#: Beyond this the "cue" stops being a caption and starts being a paragraph.
MAX_WORDS_PER_CUE = 20

#: Above the readable preset, the char cap gets out of the way. ``merge_cues``
#: applies ``max_words`` and ``max_chars`` as *independent* limits, so leaving
#: it at 42 would cap a 12-word request at roughly six Hebrew words and make
#: the control look broken. Long Hebrew words run to about this many
#: characters, so this is the width at which the word count is the only limit
#: that can bite.
_ROOMY_CHARS_PER_WORD = 16
#: Trailing marks some editors misplace when a caption is dropped into a
#: right-to-left run. Stripping them cannot change the audio: the punctuation
#: was spoken before the cues were ever split.
_TRAILING_PUNCTUATION = ".,!?:;"


def _chars_for(words_per_cue: int) -> int:
    """The char cap that lets ``words_per_cue`` actually be the limit."""
    if words_per_cue <= READABLE_WORDS_PER_CUE:
        # At or below the default the char cap is the point, not an accident:
        # a 42-character line is what makes seven words readable.
        return 42
    return _ROOMY_CHARS_PER_WORD * words_per_cue


def _strip_trailing_punctuation(text: str) -> str:
    stripped = text.rstrip(_TRAILING_PUNCTUATION + " \t")
    # A cue that was *only* punctuation keeps what it had; an empty caption is
    # worse than a stray comma.
    return stripped or text


def _enforce_min_duration(cues: List[Cue], minimum: float) -> List[Cue]:
    """Stretch cues that are too short to read, never into the next one.

    One-word cues on short Hebrew words can run to 150 ms, which flickers.
    """
    if minimum <= 0:
        return cues
    for index, cue in enumerate(cues):
        if cue.duration >= minimum:
            continue
        wanted = cue.start + minimum
        if index + 1 < len(cues):
            wanted = min(wanted, cues[index + 1].start)
        # min() above must never make a cue *shorter* than it already was.
        cues[index] = Cue(cue.start, max(cue.end, wanted), cue.text)
    return cues


def group_cues(
    word_cues: Sequence[Cue],
    *,
    words_per_cue: int = READABLE_WORDS_PER_CUE,
    strip_punctuation: bool = False,
    min_duration: float = 0.0,
    max_gap: float = 0.6,
) -> List[Cue]:
    """Regroup word-level cues at a chosen density.

    The knob behind the caption styles: ``words_per_cue=1`` gives the
    word-by-word "karaoke" look short-form video editors want, 2-3 gives the
    two-word trailing style, and the default gives readable subtitle lines.

    Args:
        word_cues: Per-word cues in order, as produced with
            ``boundary="WordBoundary"``.
        words_per_cue: Most words in one cue, 1 to :data:`MAX_WORDS_PER_CUE`.
        strip_punctuation: Drop trailing ``. , ! ? : ;`` from each cue.
        min_duration: Stretch cues shorter than this, up to the next cue.
        max_gap: Break a cue when the silence between words exceeds this.

    Raises:
        ValueError: If ``words_per_cue`` is outside the supported range.
    """
    if not 1 <= words_per_cue <= MAX_WORDS_PER_CUE:
        raise ValueError(f"words_per_cue must be 1..{MAX_WORDS_PER_CUE}, got {words_per_cue}")

    grouped = merge_cues(
        word_cues,
        max_words=words_per_cue,
        max_chars=_chars_for(words_per_cue),
        max_gap=max_gap,
    )
    if strip_punctuation:
        # After merging, never before: merge_cues breaks a line on a word that
        # ends a sentence, and stripping first would hide every one of them.
        grouped = [Cue(c.start, c.end, _strip_trailing_punctuation(c.text)) for c in grouped]
    return _enforce_min_duration(grouped, min_duration)


#: Bumped if the on-disk cue format ever changes shape.
CUES_FORMAT_VERSION = 1


def dump_cues(cues: Sequence[Cue]) -> str:
    """Serialise word cues for storage, so they can be regrouped later.

    Positional triples rather than objects: this file is written once per
    generation and read on every subtitle download, and the keys would be
    three quarters of it.
    """
    payload = {
        "version": CUES_FORMAT_VERSION,
        "cues": [[round(c.start, 3), round(c.end, 3), c.text] for c in cues],
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def load_cues(raw: Union[str, bytes]) -> List[Cue]:
    """Parse what :func:`dump_cues` wrote.

    Raises:
        ValueError: If the payload isn't a cue file this version understands.
    """
    try:
        payload = json.loads(raw)
        if payload.get("version") != CUES_FORMAT_VERSION:
            raise ValueError(f"unsupported cue format version {payload.get('version')!r}")
        return [Cue(float(start), float(end), str(text)) for start, end, text in payload["cues"]]
    except (json.JSONDecodeError, AttributeError, KeyError, TypeError) as exc:
        raise ValueError(f"not a readable cue file: {exc}") from exc


def to_srt(cues: Sequence[Cue]) -> str:
    """Render cues as SubRip (.srt)."""
    blocks = []
    for index, cue in enumerate(cues, start=1):
        start = _format_timestamp(cue.start)
        end = _format_timestamp(max(cue.end, cue.start))
        blocks.append(f"{index}\n{start} --> {end}\n{cue.text}\n")
    return "\n".join(blocks)


def to_vtt(cues: Sequence[Cue]) -> str:
    """Render cues as WebVTT (.vtt)."""
    blocks = ["WEBVTT\n"]
    for index, cue in enumerate(cues, start=1):
        start = _format_timestamp(cue.start, sep=".")
        end = _format_timestamp(max(cue.end, cue.start), sep=".")
        blocks.append(f"{index}\n{start} --> {end}\n{cue.text}\n")
    return "\n".join(blocks)


def build_communicate(prepared: str, opts: SynthesisOptions):
    """Construct an ``edge_tts.Communicate``, tolerating older signatures.

    This is the single seam where the package touches the network, so tests
    monkeypatch it rather than reaching into edge-tts.
    """
    import edge_tts

    kwargs = {
        "rate": opts.rate,
        "volume": opts.volume,
        "pitch": opts.pitch,
        "boundary": opts.boundary,
        "proxy": opts.proxy,
        "connect_timeout": opts.connect_timeout,
        "receive_timeout": opts.receive_timeout,
    }
    try:
        accepted = set(inspect.signature(edge_tts.Communicate.__init__).parameters)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        accepted = set(kwargs) | {"self", "text", "voice"}
    kwargs = {k: v for k, v in kwargs.items() if k in accepted and v is not None}
    return edge_tts.Communicate(prepared, opts.voice, **kwargs)


async def _stream_once(prepared: str, opts: SynthesisOptions, sink) -> List[Cue]:
    """One attempt: stream audio into ``sink`` and collect boundary cues."""
    cues: List[Cue] = []
    # Resolved through the module namespace so monkeypatching the seam works.
    communicate = globals()["build_communicate"](prepared, opts)
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            data = chunk.get("data")
            if data:
                sink.write(data)
        elif chunk["type"] in ("WordBoundary", "SentenceBoundary"):
            start = chunk["offset"] / TICKS_PER_SECOND
            end = (chunk["offset"] + chunk["duration"]) / TICKS_PER_SECOND
            cues.append(Cue(start, end, chunk["text"]))
    return cues


def _retryable_types() -> tuple:
    """Exception classes worth a second attempt, resolved once and cached.

    Deliberately narrow: a local ``OSError`` (full disk, bad permissions) is
    not transient, and neither is a rejected voice.
    """
    global _RETRYABLE
    if _RETRYABLE is None:
        from edge_tts import exceptions as edge_exc

        _RETRYABLE = (
            edge_exc.WebSocketError,
            edge_exc.UnexpectedResponse,
            edge_exc.UnknownResponse,
            edge_exc.NoAudioReceived,
            edge_exc.SkewAdjustmentError,
            asyncio.TimeoutError,
            ConnectionError,
        )
    return _RETRYABLE


_RETRYABLE: Optional[tuple] = None


def _is_retryable(exc: BaseException) -> bool:
    try:
        return isinstance(exc, _retryable_types())
    except Exception:  # pragma: no cover - never mask the original failure
        return False


class _CountingSink:
    """Buffers audio so a failed attempt never leaves a half-written file."""

    def __init__(self) -> None:
        self.parts: List[bytes] = []
        self.size = 0

    def write(self, data: bytes) -> None:
        self.parts.append(data)
        self.size += len(data)

    def value(self) -> bytes:
        return b"".join(self.parts)


async def synthesize(
    raw_text: str,
    output: Optional[Union[str, Path]] = None,
    opts: Optional[SynthesisOptions] = None,
    *,
    srt: Optional[Union[str, Path]] = None,
    vtt: Optional[Union[str, Path]] = None,
    merge_subtitles: bool = True,
    words_per_cue: int = READABLE_WORDS_PER_CUE,
    retain_audio: bool = True,
    already_prepared: bool = False,
    progress: Optional[ProgressFn] = None,
) -> Result:
    """Speak ``raw_text`` and return the audio, optionally writing it to disk.

    Args:
        raw_text: Hebrew text; it is cleaned up before being sent.
        output: Destination ``.mp3`` path. ``None`` keeps the audio in memory.
        opts: Voice and transport settings.
        srt: Optional path for SubRip subtitles.
        vtt: Optional path for WebVTT subtitles.
        merge_subtitles: Merge word cues into lines before writing. Turn this
            off to keep one cue per boundary event exactly as it arrived.
        words_per_cue: Density of the merged cues. ``1`` is word-by-word.
        retain_audio: Keep the MP3 in :attr:`Result.audio`. Batch callers set
            this to ``False`` so a long book doesn't sit in memory.
        already_prepared: Skip the Hebrew cleanup pipeline (the caller ran it).
        progress: Called with short status strings.

    Returns:
        A :class:`Result` with the audio, the cues, and the prepared text.

    Raises:
        ValueError: If the text is empty after preparation.
        edge_tts.exceptions.EdgeTTSException: If every attempt fails.
    """
    opts = opts or SynthesisOptions()
    prepared = raw_text.strip() if already_prepared else opts.prepare_text(raw_text)
    if not prepared:
        raise ValueError("nothing to speak: the text is empty after preparation")

    last_error: Optional[BaseException] = None
    for attempt in range(1, opts.retries + 1):
        sink = _CountingSink()
        try:
            cues = await _stream_once(prepared, opts, sink)
            break
        except Exception as exc:  # noqa: BLE001 - re-raised below
            last_error = exc
            if attempt >= opts.retries or not _is_retryable(exc):
                raise
            delay = opts.retry_backoff ** (attempt - 1)
            if progress:
                progress(f"attempt {attempt} failed ({exc.__class__.__name__}); retrying in {delay:.0f}s")
            await asyncio.sleep(delay)
    else:  # pragma: no cover - loop always breaks or raises
        raise last_error  # type: ignore[misc]

    audio = sink.value()
    path = Path(output) if output else None
    if path is not None:
        await asyncio.to_thread(_write_bytes, path, audio)
        if progress:
            progress(f"wrote {path} ({len(audio):,} bytes)")

    if merge_subtitles and opts.boundary == "WordBoundary":
        out_cues = group_cues(cues, words_per_cue=words_per_cue)
    else:
        out_cues = list(cues)
    if srt:
        await asyncio.to_thread(_write_text, Path(srt), to_srt(out_cues))
        if progress:
            progress(f"wrote {srt}")
    if vtt:
        await asyncio.to_thread(_write_text, Path(vtt), to_vtt(out_cues))
        if progress:
            progress(f"wrote {vtt}")

    return Result(
        path=path,
        audio=audio if retain_audio else b"",
        cues=out_cues,
        word_cues=cues,
        text=prepared,
    )


def _write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _write_text(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data, encoding="utf-8")


def synthesize_sync(*args, **kwargs) -> Result:
    """Blocking wrapper around :func:`synthesize`."""
    return asyncio.run(synthesize(*args, **kwargs))


async def synthesize_split(
    raw_text: str,
    out_dir: Union[str, Path],
    opts: Optional[SynthesisOptions] = None,
    *,
    by: str = "paragraph",
    max_chars: int = 1200,
    prefix: str = "part",
    concurrency: int = 2,
    progress: Optional[ProgressFn] = None,
) -> List[Result]:
    """Split long text into chunks and write one audio file per chunk.

    Useful for books or articles, where separate tracks are easier to review
    and to re-generate than one long file.
    """
    opts = opts or SynthesisOptions()
    prepared = opts.prepare_text(raw_text)
    chunks = text_utils.chunk_text(prepared, max_chars=max_chars, by=by)
    if not chunks:
        raise ValueError("nothing to speak: the text is empty after preparation")

    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    width = max(3, len(str(len(chunks))))

    async def one(index: int, chunk: str) -> Result:
        target = directory / f"{prefix}-{index:0{width}d}.mp3"
        if progress:
            progress(f"[{index}/{len(chunks)}] {chunk[:40]}…")
        # The text went through the pipeline already; don't run it twice.
        return await synthesize(
            chunk, target, opts, already_prepared=True, retain_audio=False
        )

    return await _gather_limited(
        [one(i, chunk) for i, chunk in enumerate(chunks, start=1)], concurrency
    )


async def synthesize_batch(
    items: Iterable[Tuple[str, Union[str, Path]]],
    opts: Optional[SynthesisOptions] = None,
    *,
    concurrency: int = 3,
    progress: Optional[ProgressFn] = None,
) -> List[Result]:
    """Synthesize many ``(text, output_path)`` pairs, a few at a time."""
    opts = opts or SynthesisOptions()
    pairs = list(items)

    async def one(index: int, item: Tuple[str, Union[str, Path]]) -> Result:
        line, target = item
        if progress:
            progress(f"[{index}/{len(pairs)}] {Path(target).name}")
        return await synthesize(line, target, opts, retain_audio=False)

    return await _gather_limited(
        [one(i, item) for i, item in enumerate(pairs, start=1)], concurrency
    )


async def _gather_limited(coros: List[Awaitable[Result]], limit: int) -> List[Result]:
    """Run coroutines with a concurrency cap, preserving input order.

    On the first failure the remaining tasks are cancelled and the original
    exception is raised, so a half-finished batch never leaves orphaned tasks
    writing files in the background.
    """
    semaphore = asyncio.Semaphore(max(1, limit))

    async def guarded(coro: Awaitable[Result]) -> Result:
        async with semaphore:
            return await coro

    tasks = [asyncio.ensure_future(guarded(c)) for c in coros]
    try:
        return list(await asyncio.gather(*tasks))
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
