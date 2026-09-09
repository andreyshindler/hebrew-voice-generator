"""Video rendering: the queue, the API, and cleaning up after it.

Nothing here talks to a renderer. ``_post_render`` is replaced with a stub that
writes a file where the real sidecar would have written one, so the whole
pipeline around it - claiming, quota, dedupe, failure, deletion - is exercised
offline.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from hebrew_voice import cleanup, repo, storage
from hebrew_voice.composition import build_composition
from hebrew_voice.config import Settings
from hebrew_voice.models import Render
from hebrew_voice.synth import Cue
from hebrew_voice.web import rendering
from hebrew_voice.web.app import create_app

from . import fakes
from .conftest import INVITE, csrf, register_verified

FAKE_VIDEO = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64


@pytest.fixture
def render_settings(settings) -> Settings:
    """Settings with a renderer configured, but no worker running."""
    return Settings(
        **{
            **settings.__dict__,
            "render_url": "http://renderer:8080",
            "daily_render_quota": 3,
            "max_render_seconds": 300.0,
            "render_width": 640,
            "render_height": 360,
        }
    )


@pytest.fixture
def render_app(render_settings):
    application = create_app(render_settings)
    application.state.mailer = fakes.RecordingMailer()
    return application


@pytest.fixture
def render_client(render_app, render_settings, monkeypatch):
    """A logged-in client whose renderer writes a file instead of encoding one.

    The app's own worker is stubbed out: it would otherwise claim queued rows
    from its own event loop and race whatever the test is asserting. Tests
    drive the queue explicitly with :func:`drain`, and the worker loop itself
    is covered separately in :class:`TestRenderWorker`.
    """

    async def fake_post(settings, payload):
        target = settings.data_dir / payload["outputPath"].removeprefix(
            str(settings.renderer_data_dir) + "/"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(FAKE_VIDEO)

    async def no_worker(self):
        return None

    monkeypatch.setattr(rendering, "_post_render", fake_post)
    monkeypatch.setattr(rendering.RenderWorker, "start", no_worker)
    monkeypatch.setattr(rendering.RenderWorker, "stop", no_worker)
    with TestClient(render_app) as client:
        register_verified(client, render_settings)
        yield client


def make_generation(client, text="שלום עולם ומה שלומך היום"):
    response = client.post("/api/synthesize", json={"text": text}, headers=csrf(client))
    assert response.status_code == 200, response.text
    return response.json()


async def drain(settings):
    """Run every queued render to completion, as the worker would."""
    while True:
        render = repo.claim_next_render(settings.db_path)
        if render is None:
            return
        await rendering.render_once(settings, render)


class TestComposition:
    def test_escapes_the_spoken_text(self):
        html = build_composition(
            [Cue(0.0, 1.0, "<script>אבוי</script>")],
            duration=2.0, width=640, height=360,
        )
        assert "<script>אבוי" not in html
        assert "&lt;script&gt;" in html

    def test_clamps_cues_to_the_audio_length(self):
        html = build_composition(
            [Cue(0.0, 9.0, "ארוך")], duration=2.0, width=640, height=360
        )
        assert 'data-duration="2.000"' in html

    def test_drops_cues_that_start_after_the_end(self):
        html = build_composition(
            [Cue(0.0, 1.0, "בפנים"), Cue(5.0, 6.0, "בחוץ")],
            duration=2.0, width=640, height=360,
        )
        assert "בפנים" in html and "בחוץ" not in html

    def test_overlay_has_no_audio_and_no_background(self):
        html = build_composition(
            [Cue(0.0, 1.0, "שקוף")], duration=2.0, width=640, height=360,
            transparent=True,
        )
        assert "<audio" not in html
        assert "background: transparent" in html

    def test_burned_in_video_carries_the_audio(self):
        html = build_composition(
            [Cue(0.0, 1.0, "רגיל")], duration=2.0, width=640, height=360,
            audio_src="clip.mp3",
        )
        assert '<audio data-start="0"' in html and 'src="clip.mp3"' in html


class TestRenderPaths:
    def test_video_sits_beside_its_audio(self):
        gen_id = storage.new_generation_id()
        render_id = storage.new_render_id()
        audio = storage.relative_paths(7, gen_id).audio_rel
        path = storage.render_relative_path(audio, render_id, "webm")
        assert path == audio.replace(".mp3", f".{render_id}.webm")

    def test_rejects_a_bogus_render_id(self):
        with pytest.raises(ValueError):
            storage.render_relative_path("audio/1/2026/01/x.mp3", "../etc", "mp4")

    def test_rejects_a_bogus_format(self):
        with pytest.raises(ValueError):
            storage.render_relative_path(
                "audio/1/2026/01/x.mp3", storage.new_render_id(), "../sh"
            )


class TestRenderApi:
    def test_refused_when_no_renderer_is_configured(self, auth_client, fake_tts):
        generation = make_generation(auth_client)
        response = auth_client.post(
            f"/api/generations/{generation['id']}/renders",
            json={"format": "mp4"},
            headers=csrf(auth_client),
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "rendering_disabled"

    def test_queues_a_render(self, render_client, fake_tts):
        generation = make_generation(render_client)
        response = render_client.post(
            f"/api/generations/{generation['id']}/renders",
            json={"format": "mp4"},
            headers=csrf(render_client),
        )
        assert response.status_code == 202, response.text
        body = response.json()
        assert body["status"] == "queued"
        # Nothing to download until it has actually run.
        assert body["url"] is None

    def test_rejects_an_unknown_format(self, render_client, fake_tts):
        generation = make_generation(render_client)
        response = render_client.post(
            f"/api/generations/{generation['id']}/renders",
            json={"format": "avi"},
            headers=csrf(render_client),
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "unsupported_format"

    def test_rejects_a_recording_without_word_timings(
        self, render_client, render_settings, fake_tts
    ):
        generation = make_generation(render_client)
        # Predates the density work: no cues file, so nothing to lay out.
        with repo.connect(render_settings.db_path) as conn:
            conn.execute(
                "UPDATE generations SET cues_rel = NULL WHERE id = ?", (generation["id"],)
            )
        response = render_client.post(
            f"/api/generations/{generation['id']}/renders",
            json={"format": "mp4"},
            headers=csrf(render_client),
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "cues_unavailable"

    def test_refuses_a_second_render_while_one_is_running(self, render_client, fake_tts):
        generation = make_generation(render_client)
        first = render_client.post(
            f"/api/generations/{generation['id']}/renders",
            json={"format": "mp4"}, headers=csrf(render_client),
        )
        assert first.status_code == 202
        second = render_client.post(
            f"/api/generations/{generation['id']}/renders",
            json={"format": "webm"}, headers=csrf(render_client),
        )
        assert second.status_code == 429
        assert second.json()["error"]["code"] == "already_rendering"

    def test_render_quota_is_enforced(self, render_client, render_settings, fake_tts):
        for index in range(render_settings.daily_render_quota):
            generation = make_generation(render_client, text=f"טקסט מספר {index}")
            response = render_client.post(
                f"/api/generations/{generation['id']}/renders",
                json={"format": "mp4"}, headers=csrf(render_client),
            )
            assert response.status_code == 202, response.text
        generation = make_generation(render_client, text="אחד יותר מדי")
        response = render_client.post(
            f"/api/generations/{generation['id']}/renders",
            json={"format": "mp4"}, headers=csrf(render_client),
        )
        assert response.status_code == 429
        assert response.json()["error"]["code"] == "render_quota_exceeded"

    def test_another_account_cannot_read_the_render(
        self, render_client, render_app, render_settings, fake_tts
    ):
        generation = make_generation(render_client)
        created = render_client.post(
            f"/api/generations/{generation['id']}/renders",
            json={"format": "mp4"}, headers=csrf(render_client),
        ).json()
        with TestClient(render_app) as other:
            register_verified(other, render_settings, email="other@example.com")
            assert other.get(f"/api/renders/{created['id']}").status_code == 404


@pytest.mark.anyio
class TestRenderPipeline:
    async def test_render_completes_and_can_be_downloaded(
        self, render_client, render_settings, fake_tts
    ):
        generation = make_generation(render_client)
        created = render_client.post(
            f"/api/generations/{generation['id']}/renders",
            json={"format": "mp4"}, headers=csrf(render_client),
        ).json()

        await drain(render_settings)

        polled = render_client.get(f"/api/renders/{created['id']}").json()
        assert polled["status"] == "done", polled
        assert polled["video_bytes"] == len(FAKE_VIDEO)
        assert polled["url"]

        download = render_client.get(polled["url"] + "?download=1")
        assert download.status_code == 200
        assert download.content == FAKE_VIDEO
        assert download.headers["content-type"] == "video/mp4"
        assert "attachment" in download.headers["content-disposition"]

    async def test_the_composition_is_not_left_behind(
        self, render_client, render_settings, fake_tts
    ):
        generation = make_generation(render_client)
        render_client.post(
            f"/api/generations/{generation['id']}/renders",
            json={"format": "mp4"}, headers=csrf(render_client),
        )
        await drain(render_settings)
        leftovers = list(render_settings.data_dir.rglob("*.render.html"))
        assert leftovers == []

    async def test_an_identical_request_reuses_the_finished_render(
        self, render_client, render_settings, fake_tts
    ):
        generation = make_generation(render_client)
        first = render_client.post(
            f"/api/generations/{generation['id']}/renders",
            json={"format": "mp4"}, headers=csrf(render_client),
        ).json()
        await drain(render_settings)

        before = repo.renders_today(render_settings.db_path, 1, _today(render_settings))
        again = render_client.post(
            f"/api/generations/{generation['id']}/renders",
            json={"format": "mp4"}, headers=csrf(render_client),
        )
        assert again.status_code == 202
        assert again.json()["id"] == first["id"]
        # Handing back an existing file must not spend another render.
        assert repo.renders_today(render_settings.db_path, 1, _today(render_settings)) == before

    async def test_a_different_density_renders_again(
        self, render_client, render_settings, fake_tts
    ):
        generation = make_generation(render_client)
        first = render_client.post(
            f"/api/generations/{generation['id']}/renders",
            json={"format": "mp4", "words_per_cue": 7}, headers=csrf(render_client),
        ).json()
        await drain(render_settings)
        second = render_client.post(
            f"/api/generations/{generation['id']}/renders",
            json={"format": "mp4", "words_per_cue": 1}, headers=csrf(render_client),
        ).json()
        assert second["id"] != first["id"]

    async def test_a_failing_renderer_refunds_the_quota(
        self, render_client, render_settings, fake_tts, monkeypatch
    ):
        generation = make_generation(render_client)
        created = render_client.post(
            f"/api/generations/{generation['id']}/renders",
            json={"format": "mp4"}, headers=csrf(render_client),
        ).json()
        spent = repo.renders_today(render_settings.db_path, 1, _today(render_settings))

        async def boom(settings, payload):
            raise rendering.RenderError("renderer unreachable: nope")

        monkeypatch.setattr(rendering, "_post_render", boom)
        await drain(render_settings)

        polled = render_client.get(f"/api/renders/{created['id']}").json()
        assert polled["status"] == "failed"
        assert "unreachable" in polled["error"]
        assert polled["url"] is None
        assert repo.renders_today(render_settings.db_path, 1, _today(render_settings)) == spent - 1

    async def test_a_silent_renderer_is_not_reported_as_success(
        self, render_client, render_settings, fake_tts, monkeypatch
    ):
        """200 from the sidecar but no file means the volumes disagree."""
        generation = make_generation(render_client)
        created = render_client.post(
            f"/api/generations/{generation['id']}/renders",
            json={"format": "mp4"}, headers=csrf(render_client),
        ).json()

        async def writes_nothing(settings, payload):
            return None

        monkeypatch.setattr(rendering, "_post_render", writes_nothing)
        await drain(render_settings)

        polled = render_client.get(f"/api/renders/{created['id']}").json()
        assert polled["status"] == "failed"
        assert "wrote no file" in polled["error"]

    async def test_deleting_a_recording_removes_its_video(
        self, render_client, render_settings, fake_tts
    ):
        generation = make_generation(render_client)
        render_client.post(
            f"/api/generations/{generation['id']}/renders",
            json={"format": "mp4"}, headers=csrf(render_client),
        )
        await drain(render_settings)
        videos = repo.render_video_paths(render_settings.db_path, [generation["id"]])
        assert videos and (render_settings.data_dir / videos[0]).is_file()

        deleted = render_client.delete(
            f"/api/generations/{generation['id']}", headers=csrf(render_client)
        )
        assert deleted.status_code == 204
        assert not (render_settings.data_dir / videos[0]).exists()

    async def test_the_retention_sweep_removes_videos(
        self, render_client, render_settings, fake_tts
    ):
        generation = make_generation(render_client)
        render_client.post(
            f"/api/generations/{generation['id']}/renders",
            json={"format": "mp4"}, headers=csrf(render_client),
        )
        await drain(render_settings)
        video = repo.render_video_paths(render_settings.db_path, [generation["id"]])[0]

        swept = Settings(**{**render_settings.__dict__, "history_keep": 0})
        cleanup.sweep(swept)
        assert not (render_settings.data_dir / video).exists()


@pytest.mark.anyio
class TestRendererContract:
    """What we actually send @hyperframes/producer's POST /render.

    Its documentation site describes a flat {inputPath, width, height} body the
    code does not accept - the real one takes a project directory plus an entry
    file. This pins the shape so a passing fake cannot hide a wrong request.
    """

    async def test_the_payload_matches_the_producer_api(
        self, render_client, render_settings, fake_tts, monkeypatch
    ):
        seen = {}

        async def capture(settings, payload):
            seen.update(payload)
            target = settings.data_dir / payload["outputPath"].removeprefix(
                str(settings.renderer_data_dir) + "/"
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(FAKE_VIDEO)

        monkeypatch.setattr(rendering, "_post_render", capture)
        generation = make_generation(render_client)
        render_client.post(
            f"/api/generations/{generation['id']}/renders",
            json={"format": "mp4"}, headers=csrf(render_client),
        )
        await drain(render_settings)

        assert set(seen) >= {
            "projectDir", "entryFile", "outputPath", "fps", "quality", "format"
        }
        # The flat shape from the docs would be silently ignored by the server.
        assert "inputPath" not in seen and "width" not in seen
        assert seen["quality"] in ("draft", "standard", "high")
        assert seen["fps"] in (24, 30, 60)
        assert seen["entryFile"].endswith(".render.html")
        # projectDir must be a real directory and entryFile a name inside it,
        # or the renderer rejects the request outright.
        assert "/" not in seen["entryFile"]
        assert seen["outputPath"].startswith(seen["projectDir"] + "/")


@pytest.mark.anyio
class TestRenderWorker:
    """The loop itself, which the API tests deliberately stub out."""

    async def test_it_picks_up_queued_work_and_finishes_it(
        self, render_settings, monkeypatch
    ):
        from hebrew_voice import db

        db.migrate(render_settings.db_path)
        user = repo.create_user(
            render_settings.db_path, email="w@x.y", password_hash="h", invite_code=INVITE
        )
        gen_id = storage.new_generation_id()
        _insert_bare_generation(render_settings, gen_id, user.id)
        # The worker reads real cue and audio files off disk.
        artifacts = storage.relative_paths(user.id, gen_id)
        storage.write_artifacts(
            render_settings.data_dir,
            artifacts,
            audio=b"\xff\xfb\x90\x00",
            cues='{"version":1,"cues":[[0.0,0.4,"\\u05e9\\u05dc\\u05d5\\u05dd"]]}',
        )

        async def fake_post(settings, payload):
            target = settings.data_dir / payload["outputPath"].removeprefix(
                str(settings.renderer_data_dir) + "/"
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(FAKE_VIDEO)

        monkeypatch.setattr(rendering, "_post_render", fake_post)

        render_id = storage.new_render_id()
        repo.insert_render(
            render_settings.db_path,
            Render(
                id=render_id, generation_id=gen_id, user_id=user.id, created_at=1,
                started_at=0, finished_at=0, status="queued", error=None,
                format="mp4", words_per_cue=7, width=640, height=360, fps=30,
            ),
        )

        worker = rendering.RenderWorker(render_settings)
        await worker.start()
        worker.notify()
        try:
            await _wait_until(
                lambda: repo.get_render(render_settings.db_path, render_id).is_finished
            )
        finally:
            await worker.stop()

        finished = repo.get_render(render_settings.db_path, render_id)
        assert finished.status == "done", finished.error
        assert finished.video_bytes == len(FAKE_VIDEO)


async def _wait_until(predicate, *, timeout: float = 5.0) -> None:
    """Poll until the worker has done its thing, rather than sleeping blindly."""
    import asyncio
    import time as _time

    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("timed out waiting for the render worker")


class TestRestartRecovery:
    def test_a_render_left_running_is_failed_on_startup(self, render_settings):
        """A row stuck in running has no worker behind it after a restart."""
        from hebrew_voice import db

        db.migrate(render_settings.db_path)
        user = repo.create_user(
            render_settings.db_path, email="x@y.z", password_hash="h", invite_code=INVITE
        )
        gen_id = storage.new_generation_id()
        _insert_bare_generation(render_settings, gen_id, user.id)
        render_id = storage.new_render_id()
        repo.insert_render(
            render_settings.db_path,
            Render(
                id=render_id, generation_id=gen_id, user_id=user.id, created_at=1,
                started_at=0, finished_at=0, status="queued", error=None,
                format="mp4", words_per_cue=7, width=640, height=360, fps=30,
            ),
        )
        repo.claim_next_render(render_settings.db_path)
        assert repo.get_render(render_settings.db_path, render_id).status == "running"

        assert repo.requeue_or_fail_running(render_settings.db_path, "interrupted") == 1
        recovered = repo.get_render(render_settings.db_path, render_id)
        assert recovered.status == "failed"
        assert recovered.error == "interrupted"


def _today(settings: Settings) -> str:
    from hebrew_voice.quota import quota_day

    return quota_day(settings.quota_tz)


def _insert_bare_generation(settings: Settings, gen_id: str, user_id: int) -> None:
    from hebrew_voice.models import Generation

    artifacts = storage.relative_paths(user_id, gen_id)
    repo.insert_generation(
        settings.db_path,
        Generation(
            id=gen_id, user_id=user_id, created_at=1, title="t", text_raw="a",
            text_prepared="a", char_count=1, voice="he-IL-HilaNeural", rate=0,
            pitch=0, volume=0, keep_niqqud=False, expand_symbols=True,
            expand_abbreviations=True, expand_acronyms=True,
            audio_rel=artifacts.audio_rel, srt_rel=None, vtt_rel=None,
            audio_bytes=1, duration_ms=1000, cue_count=1,
            cues_rel=artifacts.cues_rel,
        ),
    )
