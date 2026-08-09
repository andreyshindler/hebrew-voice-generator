"""Orchestrates one synthesis request: gates, quota, engine, persistence.

Holds all the process-wide concurrency state. Everything here assumes a
**single uvicorn worker** - the semaphores and rate limiters are in-process, so
``--workers 4`` would silently quadruple the effective caps.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Dict, Optional

from starlette.concurrency import run_in_threadpool

from .. import repo, storage, text as text_utils
from ..config import Settings
from ..errors import (
    QuotaExceeded,
    RateLimited,
    ServerBusy,
    SynthesisTimeout,
    UnprocessableEntity,
    UpstreamError,
)
from ..models import Generation, User
from ..quota import RateLimiter, day_reset_epoch, quota_day
from ..synth import SynthesisOptions, dump_cues, synthesize, to_srt, to_vtt
from ..voices import DEFAULT_VOICE, is_known_voice, resolve_voice
from .schemas import SynthesizeRequest

log = logging.getLogger("hebrew_voice.runner")

__all__ = ["SynthRunner", "QuotaState"]

#: A job that takes longer than this is worth a log line - it usually means
#: nginx's proxy_read_timeout needs raising too.
_SLOW_JOB_SECONDS = 60


@dataclass
class QuotaState:
    used: int
    limit: int
    resets_at: int

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    def public(self) -> Dict[str, int]:
        return {
            "used_today": self.used,
            "limit": self.limit,
            "remaining": self.remaining,
            "resets_at": self.resets_at,
        }


class SynthRunner:
    """Admission control, quota accounting, and the call into edge-tts."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.global_sem = asyncio.Semaphore(settings.max_concurrent_synth)
        self.hash_sem = asyncio.Semaphore(settings.hash_concurrency)
        self.rate_limiter = RateLimiter(settings.rate_limit_per_min, settings.rate_limit_burst)
        self.login_limiter = RateLimiter(per_minute=10, burst=10)
        self.signup_limiter = RateLimiter(per_minute=1, burst=5)
        self.preview_limiter = RateLimiter(per_minute=60, burst=20)
        # Mail is expensive and abusable; 3 an hour is plenty for a resend.
        self.resend_limiter = RateLimiter(per_minute=1, burst=3)
        self._user_gates: Dict[int, asyncio.Semaphore] = {}
        self._waiting = 0

    # -- quota ------------------------------------------------------------

    def quota_limit_for(self, user: User) -> int:
        return (
            user.daily_char_quota
            if user.daily_char_quota is not None
            else self.settings.daily_char_quota
        )

    async def quota_state(self, user: User) -> QuotaState:
        day = quota_day(self.settings.quota_tz)
        used = await run_in_threadpool(repo.usage_today, self.settings.db_path, user.id, day)
        return QuotaState(
            used=used,
            limit=self.quota_limit_for(user),
            resets_at=day_reset_epoch(self.settings.quota_tz),
        )

    # -- gates ------------------------------------------------------------

    def _user_gate(self, user_id: int) -> asyncio.Semaphore:
        gate = self._user_gates.get(user_id)
        if gate is None:
            gate = asyncio.Semaphore(self.settings.max_concurrent_per_user)
            self._user_gates[user_id] = gate
        return gate

    def check_rate(self, user: User) -> None:
        allowed, retry_after = self.rate_limiter.check(f"user:{user.id}")
        if not allowed:
            raise RateLimited(
                "Too many requests",
                detail={"retry_after": round(retry_after, 1)},
                headers={"Retry-After": str(max(1, int(retry_after)))},
            )

    # -- the job ----------------------------------------------------------

    async def run(self, user: User, request: SynthesizeRequest) -> tuple[Generation, QuotaState]:
        """Validate, meter, synthesize, persist. Returns the stored generation."""
        settings = self.settings
        try:
            voice = resolve_voice(request.voice or DEFAULT_VOICE, allow_unknown=False)
        except ValueError as exc:
            raise UnprocessableEntity(
                f"Unknown voice: {request.voice}", code="unknown_voice"
            ) from exc
        if not is_known_voice(voice):
            raise UnprocessableEntity(f"Unknown voice: {request.voice}", code="unknown_voice")

        raw = request.text
        chars = text_utils.billable_chars(raw)
        if chars == 0:
            raise UnprocessableEntity(
                "Nothing to speak", code="empty_after_preparation"
            )

        opts = SynthesisOptions.from_numbers(
            voice,
            rate=request.rate,
            pitch=request.pitch,
            volume=request.volume,
            keep_niqqud=request.keep_niqqud,
            expand_symbols=request.expand_symbols,
            expand_abbreviations=request.expand_abbreviations,
            expand_acronyms=request.expand_acronyms,
            proxy=settings.edge_proxy,
            retries=settings.synth_retries,
        )
        prepared = opts.prepare_text(raw)
        if not prepared:
            raise UnprocessableEntity(
                "Nothing left to speak after cleaning the text",
                code="empty_after_preparation",
            )

        self.check_rate(user)

        day = quota_day(settings.quota_tz)
        limit = self.quota_limit_for(user)
        granted, used = await run_in_threadpool(
            repo.reserve_quota, settings.db_path, user.id, day, chars, limit
        )
        resets_at = day_reset_epoch(settings.quota_tz)
        if not granted:
            raise QuotaExceeded(
                "Daily character quota exhausted",
                detail={
                    "used_today": used,
                    "limit": limit,
                    "remaining": max(0, limit - used),
                    "resets_at": resets_at,
                },
                headers={"Retry-After": str(max(1, resets_at - int(time.time())))},
            )

        try:
            generation = await self._synthesize_and_store(user, request, opts, prepared, chars)
        except BaseException:
            # Never charge for audio the user didn't get.
            await run_in_threadpool(repo.refund_quota, settings.db_path, user.id, day, chars)
            raise

        return generation, QuotaState(used=used, limit=limit, resets_at=resets_at)

    async def _synthesize_and_store(
        self,
        user: User,
        request: SynthesizeRequest,
        opts: SynthesisOptions,
        prepared: str,
        chars: int,
    ) -> Generation:
        settings = self.settings

        # Admission control before parking on the semaphore, so a flood is
        # rejected outright instead of piling up thousands of coroutines.
        if self._waiting >= settings.max_concurrent_synth + settings.max_queue:
            raise ServerBusy(
                "Server is busy", headers={"Retry-After": "10"}
            )

        user_gate = self._user_gate(user.id)
        if user_gate.locked():
            raise RateLimited(
                "A generation is already running for this account",
                code="already_running",
                headers={"Retry-After": "5"},
            )

        self._waiting += 1
        try:
            async with user_gate:
                try:
                    async with asyncio.timeout(settings.queue_wait_timeout):
                        await self.global_sem.acquire()
                except (asyncio.TimeoutError, TimeoutError) as exc:
                    raise ServerBusy(
                        "Server is busy", headers={"Retry-After": "10"}
                    ) from exc
                try:
                    return await self._do_work(user, request, opts, prepared, chars)
                finally:
                    self.global_sem.release()
        finally:
            self._waiting -= 1

    async def _do_work(
        self,
        user: User,
        request: SynthesizeRequest,
        opts: SynthesisOptions,
        prepared: str,
        chars: int,
    ) -> Generation:
        settings = self.settings
        started = time.monotonic()
        try:
            async with asyncio.timeout(settings.synth_timeout):
                result = await synthesize(
                    prepared,
                    None,
                    opts,
                    already_prepared=True,
                    words_per_cue=request.words_per_cue,
                )
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise SynthesisTimeout("Synthesis took too long") from exc
        except Exception as exc:
            log.warning("synthesis failed for user %s: %r", user.id, exc)
            raise UpstreamError(f"The speech service failed: {exc}") from exc

        elapsed = time.monotonic() - started
        if elapsed > _SLOW_JOB_SECONDS:
            log.warning(
                "slow synthesis: %.0fs for %d chars - check nginx proxy_read_timeout",
                elapsed,
                chars,
            )
        if not result.audio:
            raise UpstreamError("The speech service returned no audio")

        gen_id = storage.new_generation_id()
        created_at = int(time.time())
        paths = storage.relative_paths(user.id, gen_id, when=created_at)
        written = await run_in_threadpool(
            storage.write_artifacts,
            settings.data_dir,
            paths,
            audio=result.audio,
            srt=to_srt(result.cues) if request.subtitles else None,
            vtt=to_vtt(result.cues) if request.subtitles else None,
            # The raw word timings, kept beside the rendered files so the
            # subtitle endpoints can re-render at another density later
            # without re-synthesising or spending quota again.
            cues=dump_cues(result.word_cues) if request.subtitles else None,
        )

        generation = Generation(
            id=gen_id,
            user_id=user.id,
            created_at=created_at,
            title=_make_title(request.text),
            text_raw=request.text,
            text_prepared=prepared,
            char_count=chars,
            voice=opts.voice,
            rate=request.rate,
            pitch=request.pitch,
            volume=request.volume,
            keep_niqqud=request.keep_niqqud,
            expand_symbols=request.expand_symbols,
            expand_abbreviations=request.expand_abbreviations,
            expand_acronyms=request.expand_acronyms,
            audio_rel=written.audio_rel,
            srt_rel=written.srt_rel,
            vtt_rel=written.vtt_rel,
            cues_rel=written.cues_rel,
            words_per_cue=request.words_per_cue,
            audio_bytes=len(result.audio),
            duration_ms=int(result.duration * 1000),
            cue_count=len(result.cues),
        )
        await run_in_threadpool(repo.insert_generation, settings.db_path, generation)
        return generation


def _make_title(raw: str, limit: int = 70) -> str:
    """A short label for the history list, taken from the first line."""
    flat = " ".join(raw.split())
    if len(flat) <= limit:
        return flat or "ללא כותרת"
    return flat[: limit - 1].rstrip() + "…"
