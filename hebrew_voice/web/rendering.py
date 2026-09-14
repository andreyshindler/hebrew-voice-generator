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
from pathlib import Path
from typing import Optional

import aiohttp
from starlette.concurrency import run_in_threadpool

from .. import repo, storage
from ..composition import Shot, build_composition
from ..editing import shot_durations
from ..config import Settings
from ..errors import NotFound
from ..models import Render
from ..quota import quota_day
from ..synth import group_cues, load_cues

log = logging.getLogger("hebrew_voice.rendering")

__all__ = ["RenderWorker", "render_once", "RenderError"]


class RenderError(Exception):
    """A render failed for a reason worth showing the user."""


def _explain(detail: str) -> str:
    """Add the cause to renderer errors whose wording hides it.

    The renderer reports a directory it may not enter exactly as it reports one
    that is not there, so the same message covers a missing volume mount and a
    uid mismatch - and neither is guessable from the text.
    """
    if "Project directory not found" in detail:
        return (
            f"{detail} - the renderer cannot see the artifacts volume. Check it "
            "mounts the same volume at /data as the app, and that it runs as the "
            "same uid, since /data is 0700."
        )
    return detail


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
                    detail = str((body or {}).get("error") or f"HTTP {response.status}")
                    raise RenderError(f"renderer failed: {_explain(detail)[:400]}")
    except asyncio.TimeoutError as exc:
        raise RenderError(f"render exceeded {settings.render_timeout:.0f}s") from exc
    except aiohttp.ClientError as exc:
        # Almost always the sidecar being down or unreachable, which is an
        # operational problem rather than anything the user did.
        raise RenderError(f"renderer unreachable: {exc}") from exc


def _prepare(settings: Settings, render: Render) -> tuple[str, str, float]:
    """Assemble the render's work directory.

    Returns (workdir_rel, video_rel, duration). Synchronous - the caller runs
    it in a thread, like the other file work.

    Everything the renderer reads goes in one directory: the composition as
    ``index.html``, the audio, and one entry per upload. The renderer serves
    files from the directory it is given, so a composition referencing the
    media where it actually lives would be reaching outside that root. Links
    rather than copies, so a 200MB clip costs nothing to stage.
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
    workdir_rel = storage.render_workdir(render.id)
    workdir = settings.data_dir / workdir_rel
    workdir.mkdir(parents=True, exist_ok=True)

    transparent = render.format != "mp4"
    audio_name = ""
    if not transparent:
        audio_name = "audio" + Path(generation.audio_rel).suffix
        storage.link_or_copy(
            storage.resolve_under(settings.data_dir, generation.audio_rel),
            workdir / audio_name,
        )

    # Staged in the order the user arranged them; anything since deleted is
    # simply absent, and the remaining shots divide the time between them.
    staged = []
    for index, item in enumerate(
        repo.get_media_many(settings.db_path, list(render.media_ids), render.user_id)
    ):
        name = f"shot{index}{Path(item.rel).suffix}"
        try:
            storage.link_or_copy(
                storage.resolve_under(settings.data_dir, item.rel), workdir / name
            )
        except NotFound:
            continue
        staged.append((item.kind, name))

    holds = shot_durations(render.plan, shot_count=len(staged), total=generation.duration)
    shots = []
    start = 0.0
    for (kind, name), hold in zip(staged, holds):
        shots.append(Shot(kind=kind, src=name, start=start, duration=hold))
        start += hold

    music_name = ""
    music = (render.plan.get("music") or {}) if not transparent else {}
    if music.get("id"):
        found = repo.get_media_many(settings.db_path, [music["id"]], render.user_id)
        if found:
            music_name = "music" + Path(found[0].rel).suffix
            try:
                storage.link_or_copy(
                    storage.resolve_under(settings.data_dir, found[0].rel),
                    workdir / music_name,
                )
            except NotFound:
                music_name = ""

    html = build_composition(
        grouped,
        duration=generation.duration,
        width=render.width,
        height=render.height,
        audio_src=audio_name,
        transparent=transparent,
        shots=shots,
        plan=render.plan,
        word_cues=word_cues,
        music_src=music_name,
    )
    (workdir / "index.html").write_text(html, encoding="utf-8")
    return workdir_rel, video_rel, generation.duration


def _cleanup(settings: Settings, *relatives: Optional[str]) -> None:
    storage.delete_files(settings.data_dir, [r for r in relatives if r])


async def render_once(settings: Settings, render: Render) -> None:
    """Drive one claimed render to done or failed.

    Never raises: a render that blows up has to leave the row in a terminal
    state, or the client polls a job that will never move again.
    """
    workdir_rel: Optional[str] = None
    video_rel: Optional[str] = None
    try:
        workdir_rel, video_rel, duration = await run_in_threadpool(
            _prepare, settings, render
        )
        if duration > settings.max_render_seconds:
            raise RenderError(
                f"recording is longer than the {settings.max_render_seconds:.0f}s render limit"
            )

        renderer_root = settings.renderer_data_dir
        await _post_render(
            settings,
            {
                # One directory holding the composition, the audio and every
                # upload, which is what lets them all be plain relative names.
                "projectDir": str(renderer_root / workdir_rel),
                "entryFile": "index.html",
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
        # The work directory is scaffolding: links and one HTML file, never
        # served and never kept, however the render ended.
        if workdir_rel:
            await run_in_threadpool(storage.remove_tree, settings.data_dir, workdir_rel)


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
