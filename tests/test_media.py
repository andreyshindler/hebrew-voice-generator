"""Uploads: what is accepted, what it costs, and how it reaches a render."""

from __future__ import annotations

import pytest

from hebrew_voice import media as media_types
from hebrew_voice import repo, storage
from hebrew_voice.composition import Shot, build_composition, plan_shots
from hebrew_voice.synth import Cue
from hebrew_voice.web import rendering

from .conftest import csrf
from .test_renders import drain, make_generation  # noqa: F401 - fixtures reused

# Smallest byte sequences that still identify as each type. The sniffer only
# ever reads the header, which is the point.
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
MP4 = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 64


@pytest.fixture
def tight_client(render_settings, monkeypatch):
    """A client whose upload cap and storage quota are small enough to hit."""
    from dataclasses import replace as replace_settings

    from fastapi.testclient import TestClient

    from hebrew_voice.web.app import create_app

    from . import fakes
    from .conftest import register_verified

    settings = replace_settings(
        render_settings,
        max_upload_bytes=1024,
        media_quota_bytes=len(JPEG) + 8,
    )
    app = create_app(settings)
    app.state.mailer = fakes.RecordingMailer()

    async def no_worker(self):
        return None

    monkeypatch.setattr(rendering.RenderWorker, "start", no_worker)
    monkeypatch.setattr(rendering.RenderWorker, "stop", no_worker)
    with TestClient(app) as client:
        register_verified(client, settings)
        yield client


def upload(client, data: bytes, name: str, *, duration: float = 0.0):
    return client.post(
        "/api/media",
        files={"file": (name, data, "application/octet-stream")},
        data={"duration": str(duration)},
        headers=csrf(client),
    )


class TestSniffing:
    @pytest.mark.parametrize(
        "head,kind",
        [
            (JPEG, "image"),
            (PNG, "image"),
            (b"GIF89a" + b"\x00" * 20, "image"),
            (b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 10, "image"),
            (MP4, "video"),
            (b"\x1a\x45\xdf\xa3" + b"\x00" * 20, "video"),
            (b"\x00\x00\x00\x20ftypqt  " + b"\x00" * 20, "video"),
        ],
    )
    def test_recognises_what_it_should(self, head, kind):
        assert media_types.sniff(head).kind == kind

    @pytest.mark.parametrize(
        "head",
        [
            b"<!doctype html><script>alert(1)</script>",
            b"#!/bin/sh\nrm -rf /\n" + b"\x00" * 20,
            b"%PDF-1.7" + b"\x00" * 20,
            b"abc",
            b"",
        ],
    )
    def test_refuses_everything_else(self, head):
        assert media_types.sniff(head) is None


class TestUpload:
    def test_stores_a_photo(self, render_client):
        response = upload(render_client, JPEG, "beach.jpg")
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["kind"] == "image"
        assert body["mime"] == "image/jpeg"
        assert body["bytes"] == len(JPEG)
        assert body["name"] == "beach.jpg"

    def test_keeps_the_measured_clip_length(self, render_client):
        body = upload(render_client, MP4, "clip.mp4", duration=4.5).json()
        assert body["kind"] == "video"
        assert body["duration"] == pytest.approx(4.5)

    def test_the_extension_comes_from_the_bytes_not_the_name(
        self, render_client, render_settings
    ):
        """A .jpg full of MP4 is stored as an MP4, and vice versa."""
        body = upload(render_client, MP4, "totally-a-photo.jpg").json()
        item = repo.get_media(render_settings.db_path, body["id"], 1)
        assert item.rel.endswith(".mp4")
        assert item.kind == "video"

    def test_refuses_a_script_dressed_as_an_image(self, render_client):
        response = upload(render_client, b"<script>alert(1)</script>" * 4, "x.png")
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "unsupported_media"

    def test_refuses_an_empty_file(self, render_client):
        response = upload(render_client, b"", "empty.jpg")
        assert response.status_code in (413, 422)

    def test_enforces_the_size_cap(self, tight_client):
        """The cap is applied while reading, so a huge body never lands whole."""
        response = upload(tight_client, JPEG + b"\x00" * 4096, "big.jpg")
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "upload_too_large"

    def test_a_file_under_the_cap_still_round_trips(self, tight_client):
        response = upload(tight_client, JPEG, "small.jpg")
        assert response.status_code == 201
        assert response.json()["bytes"] == len(JPEG)

    def test_enforces_the_storage_quota(self, tight_client):
        # The first fits; the second would take the account past its allowance.
        assert upload(tight_client, JPEG, "one.jpg").status_code == 201
        response = upload(tight_client, PNG, "two.png")
        assert response.status_code == 429
        assert response.json()["error"]["code"] == "media_quota_exceeded"

    def test_another_account_cannot_read_it(
        self, render_client, render_app, render_settings
    ):
        from fastapi.testclient import TestClient

        from .conftest import register_verified

        body = upload(render_client, JPEG, "mine.jpg").json()
        with TestClient(render_app) as other:
            register_verified(other, render_settings, email="thief@example.com")
            assert other.get(f"/api/media/{body['id']}/file").status_code == 404
            assert other.delete(
                f"/api/media/{body['id']}", headers=csrf(other)
            ).status_code == 404

    def test_deleting_removes_the_file(self, render_client, render_settings):
        body = upload(render_client, JPEG, "gone.jpg").json()
        item = repo.get_media(render_settings.db_path, body["id"], 1)
        assert (render_settings.data_dir / item.rel).is_file()

        assert render_client.delete(
            f"/api/media/{body['id']}", headers=csrf(render_client)
        ).status_code == 204
        assert not (render_settings.data_dir / item.rel).exists()


class TestShotPlanning:
    def test_splits_the_voiceover_evenly(self):
        shots = plan_shots([("image", "a"), ("image", "b"), ("image", "c")], 9.0)
        assert [s.start for s in shots] == [0.0, 3.0, 6.0]
        assert all(s.duration == pytest.approx(3.0) for s in shots)

    def test_the_last_shot_reaches_the_end(self):
        """Rounding must not leave a sliver of black at the tail."""
        shots = plan_shots([("image", "a")] * 3, 10.0)
        assert shots[-1].start + shots[-1].duration == pytest.approx(10.0)

    def test_no_media_means_no_shots(self):
        assert plan_shots([], 10.0) == []

    def test_video_is_muted_in_the_composition(self):
        html = build_composition(
            [Cue(0.0, 1.0, "טקסט")], duration=2.0, width=1080, height=1920,
            audio_src="audio.mp3",
            shots=[Shot(kind="video", src="shot0.mp4", start=0.0, duration=2.0)],
        )
        # The clip's own sound would fight the narration.
        assert "<video class=\"shot\" muted" in html

    def test_photos_cover_the_frame(self):
        html = build_composition(
            [], duration=2.0, width=1080, height=1920,
            shots=[Shot(kind="image", src="shot0.jpg", start=0.0, duration=2.0)],
        )
        assert 'img class="shot"' in html
        assert "object-fit: cover" in html


class TestMediaInRenders:
    def test_rejects_someone_elses_file(
        self, render_client, render_app, render_settings, fake_tts
    ):
        from fastapi.testclient import TestClient

        from .conftest import register_verified

        with TestClient(render_app) as other:
            register_verified(other, render_settings, email="other@example.com")
            theirs = upload(other, JPEG, "theirs.jpg").json()

        generation = make_generation(render_client)
        response = render_client.post(
            f"/api/generations/{generation['id']}/renders",
            json={"format": "mp4", "media_ids": [theirs["id"]]},
            headers=csrf(render_client),
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "media_unavailable"

    def test_rejects_more_than_the_cap(self, render_client, fake_tts, render_settings):
        generation = make_generation(render_client)
        ids = [storage.new_media_id() for _ in range(render_settings.max_media_per_render + 1)]
        response = render_client.post(
            f"/api/generations/{generation['id']}/renders",
            json={"format": "mp4", "media_ids": ids},
            headers=csrf(render_client),
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "too_much_media"

    def test_the_order_asked_for_is_the_order_stored(self, render_client, fake_tts):
        first = upload(render_client, JPEG, "1.jpg").json()
        second = upload(render_client, PNG, "2.png").json()
        generation = make_generation(render_client)
        # Deliberately not upload order.
        wanted = [second["id"], first["id"]]
        created = render_client.post(
            f"/api/generations/{generation['id']}/renders",
            json={"format": "mp4", "media_ids": wanted},
            headers=csrf(render_client),
        ).json()
        assert created["media_ids"] == wanted

    def test_a_different_set_of_shots_is_a_different_render(
        self, render_client, render_settings, fake_tts
    ):
        photo = upload(render_client, JPEG, "1.jpg").json()
        generation = make_generation(render_client)

        plain = render_client.post(
            f"/api/generations/{generation['id']}/renders",
            json={"format": "mp4"}, headers=csrf(render_client),
        ).json()
        import anyio

        anyio.run(drain, render_settings)

        with_photo = render_client.post(
            f"/api/generations/{generation['id']}/renders",
            json={"format": "mp4", "media_ids": [photo["id"]]},
            headers=csrf(render_client),
        ).json()
        assert with_photo["id"] != plain["id"]


@pytest.mark.anyio
class TestStaging:
    async def test_the_work_directory_holds_everything_and_is_cleaned_up(
        self, render_client, render_settings, fake_tts, monkeypatch
    ):
        """The renderer serves one directory, so the inputs must all be in it."""
        photo = upload(render_client, JPEG, "1.jpg").json()
        clip = upload(render_client, MP4, "2.mp4", duration=3.0).json()
        generation = make_generation(render_client)

        seen = {}

        async def capture(settings, payload):
            project = settings.data_dir / payload["projectDir"].removeprefix(
                str(settings.renderer_data_dir) + "/"
            )
            seen["names"] = sorted(p.name for p in project.iterdir())
            seen["html"] = (project / "index.html").read_text(encoding="utf-8")
            target = settings.data_dir / payload["outputPath"].removeprefix(
                str(settings.renderer_data_dir) + "/"
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"video")

        monkeypatch.setattr(rendering, "_post_render", capture)
        render_client.post(
            f"/api/generations/{generation['id']}/renders",
            json={"format": "mp4", "media_ids": [photo["id"], clip["id"]]},
            headers=csrf(render_client),
        )
        await drain(render_settings)

        assert seen["names"] == ["audio.mp3", "index.html", "shot0.jpg", "shot1.mp4"]
        # Referenced by plain relative names, never by where they really live.
        assert 'src="shot0.jpg"' in seen["html"]
        assert 'src="shot1.mp4"' in seen["html"]
        assert "media/" not in seen["html"]
        # Scratch only: gone whatever happened.
        assert not (render_settings.data_dir / "work").exists() or not list(
            (render_settings.data_dir / "work").iterdir()
        )

    async def test_staging_links_rather_than_copies(
        self, render_client, render_settings, fake_tts, monkeypatch
    ):
        """A 200MB clip must not be duplicated on disk for every render."""
        clip = upload(render_client, MP4, "big.mp4", duration=2.0).json()
        item = repo.get_media(render_settings.db_path, clip["id"], 1)
        original = render_settings.data_dir / item.rel
        generation = make_generation(render_client)

        inodes = {}

        async def capture(settings, payload):
            project = settings.data_dir / payload["projectDir"].removeprefix(
                str(settings.renderer_data_dir) + "/"
            )
            inodes["staged"] = (project / "shot0.mp4").stat().st_ino
            target = settings.data_dir / payload["outputPath"].removeprefix(
                str(settings.renderer_data_dir) + "/"
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"video")

        monkeypatch.setattr(rendering, "_post_render", capture)
        render_client.post(
            f"/api/generations/{generation['id']}/renders",
            json={"format": "mp4", "media_ids": [clip["id"]]},
            headers=csrf(render_client),
        )
        await drain(render_settings)
        assert inodes["staged"] == original.stat().st_ino
