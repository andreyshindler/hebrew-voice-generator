"""Turning a finished generation into a video, via the HyperFrames sidecar.

Synthesis is fast enough to do inside the request; a render is not. Chromium
has to seek every frame and FFmpeg has to encode them, which is tens of seconds
at best and minutes for anything long. So a render is a row in ``renders`` that
a background worker picks up, and the client polls.

The database is the queue rather than an in-memory list, because the container
restarts on every deploy and an in-memory queue would lose the work silently.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path, PurePosixPath
from typing import Optional

import aiohttp
from starlette.concurrency import run_in_threadpool

from .. import repo, storage
from ..composition import COMPOSITION_FILENAME_SUFFIX, build_composition
from ..config import Settings
from ..models import Render
from ..quota import quota_day
from ..synth import group_cues, load_cues

log = logging.getLogger("hebrew_voice.rendering")

__all__ = ["RenderWorker", "render_once", "RenderError"]


class RenderError(Exception):
    """A render failed for a reason worth showing the user."""


def _composition_rel(video_rel: str) -> str:
    """The composition HTML lives beside its video and is deleted after."""
    return video_rel.rsplit(".", 1)[0] + COMPOSITION_FILENAME_SUFFIX


async def _post_render(settings: Settings, payload: dict) -> None:
    """Ask the sidecar to render, and raise with its message if it refuses.

    The payload shape is @hyperframes/producer's own ``POST /render`` body,
    taken from the package rather than from the documentation site, which
    describes a flatter ``{inputPath, width, height}`` request the code does
    not accept.
    """
    timeout = aiohttp.ClientTimeout(total=settings.render_timeout)
    url = f"{settings.render_url}/render"
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload) as response:
                body = await response.json(content_type=None)
                if response.status >= 400 or not (body or {}).get("success", True):
                    detail = (body or {}).get("error") or f"HTTP {response.status}"
                    raise RenderError(f"renderer failed: {str(detail)[:300]}")
    except asyncio.TimeoutError as exc:
        raise RenderError(f"render exceeded {settings.render_timeout:.0f}s") from exc
    except aiohttp.ClientError as exc:
        # Almost always the sidecar being down or unreachable, which is an
        # operational problem rather than anything the user did.
        raise RenderError(f"renderer unreachable: {exc}") from exc


def _prepare(settings: Settings, render: Render) -> tuple[str, str, float]:
    """Write the composition. Returns (composition_rel, video_rel, duration).

    Synchronous - the caller runs it in a thread, like the other file work.
    """
    generation = repo.get_generation(settings.db_path, render.generation_id, render.user_id)
    if generation is None:
        # Deleted while the render sat in the queue, which is a normal race.
        raise RenderError("the recording was deleted")
    if not generation.audio_rel:
        raise RenderError("the recording has no audio")
    if not generation.cues_rel:
        raise RenderError("this recording has no word timings")

    cues_path = storage.resolve_under(settings.data_dir, generation.cues_rel)
    word_cues = load_cues(cues_path.read_text(encoding="utf-8"))
    grouped = group_cues(word_cues, words_per_cue=render.words_per_cue)

    video_rel = storage.render_relative_path(generation.audio_rel, render.id, render.format)
    composition_rel = _composition_rel(video_rel)

    transparent = render.format != "mp4"
    html = build_composition(
        grouped,
        duration=generation.duration,
        width=render.width,
        height=render.height,
        # Resolved by the browser relative to the composition file, so both
        # containers agree on it without sharing an absolute path.
        audio_src="" if transparent else Path(generation.audio_rel).name,
        transparent=transparent,
    )
    target = settings.data_dir / composition_rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(html, encoding="utf-8")
    return composition_rel, video_rel, generation.duration


def _cleanup(settings: Settings, *relatives: Optional[str]) -> None:
    storage.delete_files(settings.data_dir, [r for r in relatives if r])


async def render_once(settings: Settings, render: Render) -> None:
    """Drive one claimed render to done or failed.

    Never raises: a render that blows up has to leave the row in a terminal
    state, or the client polls a job that will never move again.
    """
    composition_rel: Optional[str] = None
    video_rel: Optional[str] = None
    try:
        composition_rel, video_rel, duration = await run_in_threadpool(
            _prepare, settings, render
        )
        if duration > settings.max_render_seconds:
            raise RenderError(
                f"recording is longer than the {settings.max_render_seconds:.0f}s render limit"
            )

        renderer_root = settings.renderer_data_dir
        composition = PurePosixPath(composition_rel)
        await _post_render(
            settings,
            {
                # A real directory on the shared volume plus a name inside it,
                # which is what lets the composition load its audio as a plain
                # relative filename.
                "projectDir": str(renderer_root / composition.parent),
                "entryFile": composition.name,
                "outputPath": str(renderer_root / video_rel),
                "fps": render.fps,
                "quality": settings.render_quality,
                "format": render.format,
                # Frame size comes from the composition's own data-width and
                # data-height, so it is not repeated here.
                "gpu": False,
                "debug": False,
            },
        )

        produced = settings.data_dir / video_rel
        if not produced.is_file():
            # The sidecar answered 200 but nothing landed on the shared volume,
            # which normally means the two containers disagree about /data.
            raise RenderError("the renderer reported success but wrote no file")
        size = produced.stat().st_size
        await run_in_threadpool(
            repo.finish_render, settings.db_path, render.id, video_rel=video_rel,
            video_bytes=size,
        )
        log.info("render %s done: %s (%d bytes)", render.id, render.format, size)
    except Exception as exc:  # noqa: BLE001 - the row must reach a terminal state
        message = str(exc) if isinstance(exc, RenderError) else "the render failed"
        if not isinstance(exc, RenderError):
            log.exception("render %s failed unexpectedly", render.id)
        else:
            log.warning("render %s failed: %s", render.id, message)
        await run_in_threadpool(repo.fail_render, settings.db_path, render.id, message)
        # Nothing was delivered, so the allowance goes back.
        await run_in_threadpool(
            repo.refund_render_quota,
            settings.db_path,
            render.user_id,
            quota_day(settings.quota_tz, now=render.created_at),
        )
        await run_in_threadpool(_cleanup, settings, video_rel)
    finally:
        # The composition is scaffolding; it is never served and never kept.
        await run_in_threadpool(_cleanup, settings, composition_rel)


class RenderWorker:
    """Polls the renders table and runs whatever is queued.

    Started from the app lifespan next to the retention sweep. One task, with
    at most ``max_concurrent_renders`` renders in flight.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._sem = asyncio.Semaphore(max(1, settings.max_concurrent_renders))
        self._task: Optional[asyncio.Task] = None
        self._running: set[asyncio.Task] = set()
        self._wake = asyncio.Event()

    def notify(self) -> None:
        """Nudge the loop so a newly queued render starts without waiting."""
        self._wake.set()

    async def start(self) -> None:
        # Anything left running belongs to a process that no longer exists.
        swept = await run_in_threadpool(
            repo.requeue_or_fail_running,
            self.settings.db_path,
            "interrupted by a restart",
        )
        if swept:
            log.warning("marked %d interrupted render(s) failed on startup", swept)
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
                    render = await run_in_threadpool(
                        repo.claim_next_render, self.settings.db_path
                    )
                    if render is None:
                        break
                    claimed += 1
                    await self._sem.acquire()
                    task = asyncio.create_task(self._run(render))
                    self._running.add(task)
                    task.add_done_callback(self._running.discard)
                if not claimed:
                    # Sleep until something is queued or the poll interval
                    # elapses, so a fresh request starts immediately.
                    try:
                        await asyncio.wait_for(
                            self._wake.wait(), timeout=self.settings.render_poll_seconds
                        )
                    except asyncio.TimeoutError:
                        pass
                    self._wake.clear()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the loop must outlive one bad pass
                log.exception("render worker loop error")
                await asyncio.sleep(self.settings.render_poll_seconds)

    async def _run(self, render: Render) -> None:
        started = time.monotonic()
        try:
            await render_once(self.settings, render)
        finally:
            self._sem.release()
            log.info("render %s took %.1fs", render.id, time.monotonic() - started)
