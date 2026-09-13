"""Turning an uploaded recording into word timings, via a hosted provider.

The app synthesises speech and gets its subtitle timings for free, as
``WordBoundary`` events on the way past. A recording someone made themselves
has no such thing, so the words and their timings have to be recognised - and
that is a network call to a speech-to-text service, taking long enough that it
cannot happen inside the request.

So a transcription is a row in ``transcriptions`` that a background worker
picks up, exactly like a render, and for the same reason: the database is the
queue because the container restarts on every deploy.

What comes back is a list of words with times, which is precisely what the rest
of the app already runs on. A transcription therefore ends as an ordinary
generations row, and the history, the subtitle endpoints, the density control
and the video editor all work on it without knowing the difference.

The provider is an OpenAI-compatible ``/audio/transcriptions`` endpoint. Groq
and OpenAI both serve one, so which is used is a setting rather than code.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import List, Optional, Tuple

import aiohttp
from starlette.concurrency import run_in_threadpool

from .. import repo, storage
from ..config import Settings
from ..models import Generation, Transcription
from ..quota import quota_day
from ..synth import Cue, dump_cues, group_cues, to_srt, to_vtt

log = logging.getLogger("hebrew_voice.transcribing")

__all__ = ["TranscribeWorker", "transcribe_once", "TranscriptionError", "cues_from_words"]

#: Formats the providers accept as-is. Everything the uploader can produce is
#: here except QuickTime, which has to be converted first and is refused up
#: front rather than failing three minutes into a job.
SENDABLE = {"mp3", "m4a", "wav", "ogg", "mp4", "webm"}

#: Hard cap at both providers. A file over this has to be compressed, which
#: needs FFmpeg the app image does not have.
MAX_PROVIDER_BYTES = 25 * 1024 * 1024


class TranscriptionError(Exception):
    """A transcription failed for a reason worth showing the user."""


def cues_from_words(words: object) -> List[Cue]:
    """Map the provider's word array onto our cues.

    Shape is ``[{"word": "...", "start": 0.0, "end": 0.4}, ...]``. Anything
    without a usable text and a pair of numbers is dropped rather than guessed
    at: one malformed entry should cost one word, not the whole recording.
    """
    if not isinstance(words, list):
        raise TranscriptionError("the transcription service returned no word timings")
    cues: List[Cue] = []
    for item in words:
        if not isinstance(item, dict):
            continue
        text = str(item.get("word") or item.get("text") or "").strip()
        if not text:
            continue
        try:
            start = float(item["start"])
            end = float(item["end"])
        except (KeyError, TypeError, ValueError):
            continue
        # Providers occasionally emit a zero-length or inverted span at a cut.
        cues.append(Cue(start, max(start, end), text))
    if not cues:
        raise TranscriptionError("no speech was recognised in the recording")
    return cues


async def _post_transcription(
    settings: Settings, audio: bytes, filename: str, mime: str
) -> dict:
    """Send the audio and return the parsed response.

    Unlike a render, the bytes really do cross the socket - the provider is
    somewhere else entirely and has no view of our volume. That is the whole
    reason the size cap matters.
    """
    timeout = aiohttp.ClientTimeout(total=settings.stt_timeout)
    url = f"{settings.stt_url}/audio/transcriptions"
    form = aiohttp.FormData()
    form.add_field("file", audio, filename=filename, content_type=mime)
    form.add_field("model", settings.stt_model)
    form.add_field("language", settings.stt_language)
    form.add_field("response_format", "verbose_json")
    # The array form is what both providers expect, and asking for word
    # granularity is the entire point: segment timings cannot drive the
    # per-word karaoke or the density control.
    form.add_field("timestamp_granularities[]", "word")
    headers = {"Authorization": f"Bearer {settings.stt_key}"}
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, data=form, headers=headers) as response:
                body = await response.json(content_type=None)
                if response.status >= 400:
                    raise TranscriptionError(
                        f"transcription failed: {_explain(response.status, body)[:400]}"
                    )
                if not isinstance(body, dict):
                    raise TranscriptionError("the transcription service returned nothing usable")
                return body
    except asyncio.TimeoutError as exc:
        raise TranscriptionError(f"transcription exceeded {settings.stt_timeout:.0f}s") from exc
    except aiohttp.ClientError as exc:
        raise TranscriptionError(f"transcription service unreachable: {exc}") from exc


def _explain(status: int, body: object) -> str:
    """Turn the provider's error into something an operator can act on.

    A wrong or expired key is the failure this app has never had before -
    edge-tts needs no credentials - so it is worth naming rather than passing
    through as a bare 401.
    """
    detail = ""
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            detail = str(error.get("message") or "")
        elif error:
            detail = str(error)
    detail = detail or f"HTTP {status}"
    if status in (401, 403):
        return f"{detail} - check HV_STT_KEY is set and still valid"
    if status == 413:
        return f"{detail} - the recording is too large for the provider"
    if status == 429:
        return f"{detail} - the provider is rate limiting or out of credit"
    return detail


def _load_source(settings: Settings, job: Transcription) -> Tuple[bytes, str, str, str]:
    """Read the uploaded file, refusing anything that cannot be sent."""
    if not job.media_id:
        raise TranscriptionError("the recording is no longer available")
    media = repo.get_media(settings.db_path, job.media_id, job.user_id)
    if media is None:
        # Deleted while the job waited. A race, not a fault.
        raise TranscriptionError("the recording is no longer available")
    ext = Path(media.rel).suffix.lstrip(".").lower()
    if ext not in SENDABLE:
        raise TranscriptionError(
            f"{ext or 'this'} files cannot be transcribed yet - upload MP3, M4A, WAV, MP4 or WebM"
        )
    if media.bytes > MAX_PROVIDER_BYTES:
        raise TranscriptionError(
            f"the recording is {media.bytes // (1024 * 1024)}MB, over the "
            f"{MAX_PROVIDER_BYTES // (1024 * 1024)}MB the transcription service accepts"
        )
    path = storage.resolve_under(settings.data_dir, media.rel)
    return path.read_bytes(), Path(media.rel).name, media.mime, media.original_name


def _store(
    settings: Settings, job: Transcription, words: List[Cue], text: str
) -> Tuple[str, float, int]:
    """Write the artifacts and the generation row. Returns (gen_id, seconds, bytes).

    The audio is hard-linked rather than copied: the upload already sits on the
    volume, and a recording and its transcription are the same bytes.
    """
    from ..synth import READABLE_WORDS_PER_CUE

    media = repo.get_media(settings.db_path, job.media_id or "", job.user_id)
    if media is None:
        raise TranscriptionError("the recording is no longer available")

    created_at = int(time.time())
    gen_id = storage.new_generation_id()
    ext = Path(media.rel).suffix.lstrip(".").lower() or "mp3"
    paths = storage.relative_paths(job.user_id, gen_id, when=created_at, audio_ext=ext)
    cues = group_cues(words, words_per_cue=READABLE_WORDS_PER_CUE)
    duration = words[-1].end if words else 0.0

    source = storage.resolve_under(settings.data_dir, media.rel)
    audio_target = settings.data_dir / paths.audio_rel
    audio_target.parent.mkdir(parents=True, exist_ok=True)
    # A hard link, not a copy: the upload is already on this volume and the two
    # are the same bytes. The recording owns its own name so that deleting it
    # later cannot take the user's upload with it.
    storage.link_or_copy(source, audio_target)

    storage.write_bytes(settings.data_dir, paths.srt_rel, to_srt(cues).encode("utf-8"))
    storage.write_bytes(settings.data_dir, paths.vtt_rel, to_vtt(cues).encode("utf-8"))
    storage.write_bytes(settings.data_dir, paths.cues_rel, dump_cues(words).encode("utf-8"))

    # Without the extension: the title becomes the downloaded subtitle's
    # filename, and "voice.wav.srt" reads as a mistake.
    stem = Path(media.original_name).stem if media.original_name else ""
    title = stem or text[:60] or "תמלול"
    repo.insert_generation(
        settings.db_path,
        Generation(
            id=gen_id,
            user_id=job.user_id,
            created_at=created_at,
            title=title[:120],
            # The recognised words are the text. There was no input to prepare,
            # so raw and prepared are the same thing.
            text_raw=text,
            text_prepared=text,
            char_count=len(text),
            # No voice spoke this. The UI keys off source rather than trying to
            # make sense of an empty voice id.
            voice="",
            rate=0,
            pitch=0,
            volume=0,
            keep_niqqud=False,
            expand_symbols=False,
            expand_abbreviations=False,
            expand_acronyms=False,
            audio_rel=paths.audio_rel,
            srt_rel=paths.srt_rel,
            vtt_rel=paths.vtt_rel,
            cues_rel=paths.cues_rel,
            audio_bytes=audio_target.stat().st_size,
            duration_ms=int(duration * 1000),
            cue_count=len(cues),
            words_per_cue=READABLE_WORDS_PER_CUE,
            source="transcription",
        ),
    )
    return gen_id, duration, len(cues)


async def transcribe_once(settings: Settings, job: Transcription) -> None:
    """Drive one claimed transcription to done or failed.

    Never raises: the row has to reach a terminal state, or the client polls a
    job that will never move again.
    """
    try:
        audio, filename, mime, _ = await run_in_threadpool(_load_source, settings, job)
        body = await _post_transcription(settings, audio, filename, mime)
        words = cues_from_words(body.get("words"))
        text = str(body.get("text") or " ".join(cue.text for cue in words)).strip()

        gen_id, duration, cue_count = await run_in_threadpool(_store, settings, job, words, text)
        await run_in_threadpool(
            repo.finish_transcription, settings.db_path, job.id,
            generation_id=gen_id, seconds=duration,
        )
        # The reservation was made against what the browser measured. Correct
        # it now that the provider has said how long the recording really was.
        await run_in_threadpool(
            repo.settle_transcription_quota,
            settings.db_path,
            job.user_id,
            quota_day(settings.quota_tz, now=job.created_at),
            int(round(duration)) - int(round(job.seconds)),
        )
        log.info(
            "transcription %s done: %.1fs, %d cues", job.id, duration, cue_count
        )
    except Exception as exc:  # noqa: BLE001 - the row must reach a terminal state
        known = isinstance(exc, TranscriptionError)
        message = str(exc) if known else "the transcription failed"
        if known:
            log.warning("transcription %s failed: %s", job.id, message)
        else:
            log.exception("transcription %s failed unexpectedly", job.id)
        await run_in_threadpool(repo.fail_transcription, settings.db_path, job.id, message)
        # Nothing was delivered, so the whole reservation goes back.
        await run_in_threadpool(
            repo.refund_transcription_quota,
            settings.db_path,
            job.user_id,
            quota_day(settings.quota_tz, now=job.created_at),
            int(round(job.seconds)),
        )


class TranscribeWorker:
    """Polls the transcriptions table and runs whatever is queued.

    Deliberately a sibling of :class:`~hebrew_voice.web.rendering.RenderWorker`
    rather than a generalisation of it. The claim, finish and sweep queries name
    their table, so sharing one worker would mean parameterising working, tested
    code over two cases - a worse trade than seventy lines of the same shape.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._sem = asyncio.Semaphore(max(1, settings.max_concurrent_transcriptions))
        self._task: Optional[asyncio.Task] = None
        self._running: set[asyncio.Task] = set()
        self._wake = asyncio.Event()

    def notify(self) -> None:
        """Nudge the loop so a newly queued job starts without waiting."""
        self._wake.set()

    async def start(self) -> None:
        # Anything left running belongs to a process that no longer exists.
        swept = await run_in_threadpool(
            repo.fail_running_transcriptions,
            self.settings.db_path,
            "interrupted by a restart",
        )
        if swept:
            log.warning("marked %d interrupted transcription(s) failed on startup", swept)
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        for task in (self._task, *self._running):
            if task is not None:
                task.cancel()
        pending = [t for t in (self._task, *self._running) if t is not None]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def _loop(self) -> None:
        while True:
            try:
                claimed = 0
                while not self._sem.locked():
                    job = await run_in_threadpool(
                        repo.claim_next_transcription, self.settings.db_path
                    )
                    if job is None:
                        break
                    claimed += 1
                    await self._sem.acquire()
                    task = asyncio.create_task(self._run(job))
                    self._running.add(task)
                    task.add_done_callback(self._running.discard)
                if not claimed:
                    try:
                        await asyncio.wait_for(
                            self._wake.wait(), timeout=self.settings.transcribe_poll_seconds
                        )
                    except asyncio.TimeoutError:
                        pass
                    self._wake.clear()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the loop must outlive one bad pass
                log.exception("transcribe worker loop error")
                await asyncio.sleep(self.settings.transcribe_poll_seconds)

    async def _run(self, job: Transcription) -> None:
        started = time.monotonic()
        try:
            await transcribe_once(self.settings, job)
        finally:
            self._sem.release()
            log.info("transcription %s took %.1fs", job.id, time.monotonic() - started)
