"""Transcription: what is accepted, what it costs, and what it produces.

Nothing here talks to a provider. ``_post_transcription`` is replaced with a
stub that returns a fixed word list, so the tests cover our half of the
contract - the queue, the quota, the artifacts - and not Whisper's.
"""

from __future__ import annotations

import pytest

from hebrew_voice import repo, storage
from hebrew_voice.synth import Cue, load_cues
from hebrew_voice.web import transcribing
from hebrew_voice.web.transcribing import TranscriptionError, cues_from_words

from .conftest import FAKE_WORDS, csrf
from .test_media import JPEG, MP4, upload

MP3 = b"ID3\x03\x00" + b"\x00" * 64
WAV = b"RIFF\x00\x00\x00\x00WAVEfmt " + b"\x00" * 64


def start(client, media_id):
    return client.post(
        "/api/transcriptions", json={"media_id": media_id}, headers=csrf(client)
    )


async def drain(settings):
    """Run every queued transcription to completion, as the worker would."""
    while True:
        job = repo.claim_next_transcription(settings.db_path)
        if job is None:
            return
        await transcribing.transcribe_once(settings, job)


class TestWordMapping:
    def test_words_become_cues(self):
        cues = cues_from_words(FAKE_WORDS)
        assert [c.text for c in cues] == ["שלום", "עולם"]
        assert cues[0].start == 0.0 and cues[0].end == 0.4

    def test_a_malformed_entry_costs_one_word_not_the_recording(self):
        cues = cues_from_words(
            [{"word": "אחת", "start": 0.0, "end": 0.3}, {"word": "שתיים"}, "rubbish"]
        )
        assert [c.text for c in cues] == ["אחת"]

    def test_an_inverted_span_is_flattened_rather_than_kept(self):
        """A zero-length or backwards cue would make the subtitle writer lie."""
        cues = cues_from_words([{"word": "מילה", "start": 1.0, "end": 0.2}])
        assert cues[0].end == 1.0

    @pytest.mark.parametrize("payload", [None, "text", {}, []])
    def test_nothing_usable_is_an_error_not_an_empty_transcript(self, payload):
        with pytest.raises(TranscriptionError):
            cues_from_words(payload)


class TestRequesting:
    def test_refused_when_no_provider_is_configured(self, render_client):
        media = upload(render_client, MP3, "voice.mp3", duration=4.0).json()
        response = render_client.post(
            "/api/transcriptions", json={"media_id": media["id"]}, headers=csrf(render_client)
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "transcription_disabled"

    def test_queues_a_job(self, stt_client):
        media = upload(stt_client, MP3, "voice.mp3", duration=4.0).json()
        response = start(stt_client, media["id"])
        assert response.status_code == 202
        body = response.json()
        assert body["status"] == "queued"
        # Nothing to open yet, which is what the client polls for.
        assert body["generation_id"] is None

    def test_an_image_is_refused(self, stt_client):
        photo = upload(stt_client, JPEG, "photo.jpg").json()
        response = start(stt_client, photo["id"])
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "unsupported_for_transcription"

    def test_another_accounts_upload_is_not_transcribable(
        self, stt_client, stt_settings
    ):
        from fastapi.testclient import TestClient

        from hebrew_voice.web.app import create_app

        from . import fakes
        from .conftest import register_verified

        app = create_app(stt_settings)
        app.state.mailer = fakes.RecordingMailer()
        with TestClient(app) as other:
            register_verified(other, stt_settings, email="someone@example.com")
            theirs = upload(other, MP3, "theirs.mp3", duration=3.0).json()

        response = start(stt_client, theirs["id"])
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "media_unavailable"

    def test_a_recording_over_the_length_cap_is_refused(self, stt_client):
        media = upload(stt_client, MP3, "epic.mp3", duration=9999.0).json()
        response = start(stt_client, media["id"])
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "too_long_to_transcribe"

    def test_only_one_runs_at_a_time(self, stt_client):
        first = upload(stt_client, MP3, "a.mp3", duration=3.0).json()
        second = upload(stt_client, MP3, "b.mp3", duration=3.0).json()
        assert start(stt_client, first["id"]).status_code == 202
        response = start(stt_client, second["id"])
        assert response.status_code == 429
        assert response.json()["error"]["code"] == "already_transcribing"


class TestQuota:
    def test_seconds_are_reserved_not_requests(self, stt_client, stt_settings):
        """The unit is audio, not calls - the provider bills by duration."""
        from hebrew_voice.quota import quota_day

        media = upload(stt_client, MP3, "voice.mp3", duration=30.0).json()
        start(stt_client, media["id"])
        day = quota_day(stt_settings.quota_tz)
        assert repo.transcribed_today(stt_settings.db_path, 1, day) == 30
        # And it did not touch the character allowance, which pays for TTS.
        assert repo.usage_today(stt_settings.db_path, 1, day) == 0

    def test_an_unmeasured_upload_still_costs_something(self, stt_client):
        """A browser that failed to read the duration must not transcribe free."""
        media = upload(stt_client, MP3, "voice.mp3", duration=0.0).json()
        body = start(stt_client, media["id"]).json()
        assert body["seconds"] >= 1

    def test_the_daily_limit_is_enforced(self, stt_client, stt_settings):
        # The cap is 600s; one 500s recording fits, a second does not.
        first = upload(stt_client, MP3, "a.mp3", duration=250.0).json()
        assert start(stt_client, first["id"]).status_code == 202
        repo.finish_transcription(
            stt_settings.db_path,
            repo.claim_next_transcription(stt_settings.db_path).id,
            generation_id=None,
            seconds=250.0,
        )
        second = upload(stt_client, MP3, "b.mp3", duration=250.0).json()
        assert start(stt_client, second["id"]).status_code == 202
        repo.finish_transcription(
            stt_settings.db_path,
            repo.claim_next_transcription(stt_settings.db_path).id,
            generation_id=None,
            seconds=250.0,
        )
        third = upload(stt_client, MP3, "c.mp3", duration=250.0).json()
        response = start(stt_client, third["id"])
        assert response.status_code == 429
        assert response.json()["error"]["code"] == "transcription_quota_exceeded"
        assert "Retry-After" in response.headers


@pytest.mark.anyio
class TestRunning:
    async def test_it_produces_a_recording_with_cues(self, stt_client, stt_settings):
        media = upload(stt_client, MP3, "voice.mp3", duration=2.0).json()
        job = start(stt_client, media["id"]).json()
        await drain(stt_settings)

        polled = stt_client.get(f"/api/transcriptions/{job['id']}").json()
        assert polled["status"] == "done"
        assert polled["generation_id"]

        generation = stt_client.get(
            f"/api/generations/{polled['generation_id']}"
        ).json()
        assert generation["source"] == "transcription"
        assert generation["voice"] == ""
        # The whole point: the density control works on it like anything else.
        assert generation["can_regroup"] is True

    async def test_the_subtitles_are_real_and_regroupable(self, stt_client, stt_settings):
        media = upload(stt_client, MP3, "voice.mp3", duration=2.0).json()
        job = start(stt_client, media["id"]).json()
        await drain(stt_settings)
        gen_id = stt_client.get(f"/api/transcriptions/{job['id']}").json()["generation_id"]

        srt = stt_client.get(f"/api/generations/{gen_id}/subtitles.srt?words=1")
        assert srt.status_code == 200
        # One word per cue, so two cues for two words.
        assert srt.text.count("-->") == 2

    async def test_the_audio_keeps_its_own_container(self, stt_client, stt_settings):
        """A WAV served as audio/mpeg does not play."""
        media = upload(stt_client, WAV, "voice.wav", duration=2.0).json()
        job = start(stt_client, media["id"]).json()
        await drain(stt_settings)
        gen_id = stt_client.get(f"/api/transcriptions/{job['id']}").json()["generation_id"]

        generation = stt_client.get(f"/api/generations/{gen_id}").json()
        assert generation["urls"]["audio"].endswith("/audio.wav")
        served = stt_client.get(generation["urls"]["audio"])
        assert served.status_code == 200
        assert served.headers["content-type"].startswith("audio/wav")

    async def test_the_upload_survives_deleting_the_recording(
        self, stt_client, stt_settings
    ):
        """The two are hard links, so removing one must not remove the other."""
        media = upload(stt_client, MP3, "voice.mp3", duration=2.0).json()
        job = start(stt_client, media["id"]).json()
        await drain(stt_settings)
        gen_id = stt_client.get(f"/api/transcriptions/{job['id']}").json()["generation_id"]

        assert stt_client.delete(
            f"/api/generations/{gen_id}", headers=csrf(stt_client)
        ).status_code == 204
        still_there = stt_client.get(f"/api/media/{media['id']}/file")
        assert still_there.status_code == 200

    async def test_a_provider_failure_refunds_the_quota(
        self, stt_client, stt_settings, monkeypatch
    ):
        from hebrew_voice.quota import quota_day

        media = upload(stt_client, MP3, "voice.mp3", duration=60.0).json()
        job = start(stt_client, media["id"]).json()

        async def explode(settings, audio, filename, mime):
            raise TranscriptionError("transcription service unreachable: boom")

        monkeypatch.setattr(transcribing, "_post_transcription", explode)
        await drain(stt_settings)

        polled = stt_client.get(f"/api/transcriptions/{job['id']}").json()
        assert polled["status"] == "failed"
        assert "unreachable" in polled["error"]
        assert polled["generation_id"] is None
        # The allowance came back, so the next attempt is not punished.
        assert repo.transcribed_today(
            stt_settings.db_path, 1, quota_day(stt_settings.quota_tz)
        ) == 0

    async def test_an_upload_deleted_while_queued_is_a_race_not_a_crash(
        self, stt_client, stt_settings
    ):
        media = upload(stt_client, MP3, "voice.mp3", duration=2.0).json()
        job = start(stt_client, media["id"]).json()
        stt_client.delete(f"/api/media/{media['id']}", headers=csrf(stt_client))
        await drain(stt_settings)

        polled = stt_client.get(f"/api/transcriptions/{job['id']}").json()
        assert polled["status"] == "failed"
        assert "no longer available" in polled["error"]


@pytest.mark.anyio
class TestRestartSweep:
    async def test_a_job_left_running_is_failed_not_requeued(
        self, stt_client, stt_settings
    ):
        """A recording that killed the worker must not come back every boot."""
        from hebrew_voice.models import Transcription

        repo.insert_transcription(
            stt_settings.db_path,
            Transcription(
                id="f" * 32, user_id=1, media_id=None, generation_id=None,
                created_at=1, started_at=1, finished_at=None,
                status="running", error=None, seconds=5.0,
            ),
        )
        swept = repo.fail_running_transcriptions(stt_settings.db_path, "interrupted")
        assert swept == 1
        row = repo.get_transcription(stt_settings.db_path, "f" * 32, 1)
        assert row.status == "failed"


class TestBrowserMeasurement:
    def test_the_csp_lets_the_page_measure_an_upload(self, stt_client):
        """A blob: URL is how long a recording is found out.

        The app image has no media tools, so the duration can only come from
        the browser pointing a media element at the file the user just chose.
        Without blob: in media-src that fails silently, every upload reports
        zero, and both the quota reservation and the length cap become
        meaningless.
        """
        policy = stt_client.get("/").headers["Content-Security-Policy"]
        assert "media-src 'self' blob:" in policy
        assert "img-src 'self' data: blob:" in policy
        # Widened for blob: only - nothing else may load from anywhere.
        assert "script-src 'self';" in policy
        assert "connect-src 'self';" in policy


@pytest.mark.anyio
class TestCorrectingTheTranscript:
    async def _transcribed(self, client, settings):
        media = upload(client, MP3, "voice.mp3", duration=2.0).json()
        job = start(client, media["id"]).json()
        await drain(settings)
        return client.get(f"/api/transcriptions/{job['id']}").json()["generation_id"]

    def _edit(self, client, gen_id, text):
        return client.patch(
            f"/api/generations/{gen_id}/transcript",
            json={"text": text},
            headers=csrf(client),
        )

    async def test_a_correction_rewrites_the_subtitles(self, stt_client, stt_settings):
        gen_id = await self._transcribed(stt_client, stt_settings)
        response = self._edit(stt_client, gen_id, "שלום חברים")
        assert response.status_code == 200
        assert response.json()["text"] == "שלום חברים"

        srt = stt_client.get(f"/api/generations/{gen_id}/subtitles.srt").text
        assert "חברים" in srt
        assert "עולם" not in srt

    async def test_the_word_that_did_not_change_keeps_its_timing(
        self, stt_client, stt_settings
    ):
        """The whole point of aligning rather than re-spreading."""
        gen_id = await self._transcribed(stt_client, stt_settings)
        before = stt_client.get(
            f"/api/generations/{gen_id}/subtitles.srt?words=1"
        ).text.splitlines()[1]
        self._edit(stt_client, gen_id, "שלום חברים")
        after = stt_client.get(
            f"/api/generations/{gen_id}/subtitles.srt?words=1"
        ).text.splitlines()[1]
        assert before == after

    async def test_the_stored_word_timings_are_rewritten_too(
        self, stt_client, stt_settings
    ):
        """Not just the SRT: the video renderer reads the cue file."""
        gen_id = await self._transcribed(stt_client, stt_settings)
        self._edit(stt_client, gen_id, "שלום חברים")
        generation = repo.get_generation(stt_settings.db_path, gen_id, 1)
        cues = load_cues(
            storage.resolve_under(stt_settings.data_dir, generation.cues_rel).read_bytes()
        )
        assert [c.text for c in cues] == ["שלום", "חברים"]

    async def test_the_audio_is_untouched(self, stt_client, stt_settings):
        gen_id = await self._transcribed(stt_client, stt_settings)
        before = stt_client.get(f"/api/generations/{gen_id}").json()
        self._edit(stt_client, gen_id, "שלום חברים")
        after = stt_client.get(f"/api/generations/{gen_id}").json()
        assert after["duration"] == before["duration"]
        assert after["audio_bytes"] == before["audio_bytes"]

    async def test_subtitles_of_a_transcript_are_not_served_immutable(
        self, stt_client, stt_settings, fake_tts
    ):
        """An edit rewrites the file under an id that never changes.

        Cached as immutable, a browser would keep showing the words the
        recogniser got wrong however many times they were corrected.
        """
        gen_id = await self._transcribed(stt_client, stt_settings)
        response = stt_client.get(f"/api/generations/{gen_id}/subtitles.srt")
        assert "immutable" not in response.headers["Cache-Control"]
        # Synthesised subtitles cannot be edited, so they stay immutable.
        from .test_renders import make_generation

        synth_id = make_generation(stt_client)["id"]
        synthesised = stt_client.get(f"/api/generations/{synth_id}/subtitles.srt")
        assert "immutable" in synthesised.headers["Cache-Control"]

    async def test_a_synthesised_recording_cannot_be_corrected(
        self, stt_client, fake_tts
    ):
        """Its text is its input - editing here would leave the two disagreeing."""
        from .test_renders import make_generation

        generation = make_generation(stt_client)
        response = self._edit(stt_client, generation["id"], "טקסט אחר")
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "not_a_transcript"

    async def test_an_empty_transcript_is_refused(self, stt_client, stt_settings):
        gen_id = await self._transcribed(stt_client, stt_settings)
        assert self._edit(stt_client, gen_id, "   ").status_code == 422

    async def test_correcting_the_words_discards_a_stale_video(
        self, stt_client, stt_settings
    ):
        """The video has the old words burned into its frames."""
        from .test_renders import drain as drain_renders

        gen_id = await self._transcribed(stt_client, stt_settings)
        stt_client.post(
            f"/api/generations/{gen_id}/renders",
            json={"format": "mp4"},
            headers=csrf(stt_client),
        )
        await drain_renders(stt_settings)
        assert stt_client.get(f"/api/generations/{gen_id}/renders").json()["items"]

        self._edit(stt_client, gen_id, "שלום חברים")
        # Gone, so the dedupe cannot hand the old video back as if it were
        # current.
        assert stt_client.get(f"/api/generations/{gen_id}/renders").json()["items"] == []

    async def test_correcting_costs_no_quota(self, stt_client, stt_settings):
        """Nothing is synthesised and nothing is transcribed."""
        from hebrew_voice.quota import quota_day

        gen_id = await self._transcribed(stt_client, stt_settings)
        day = quota_day(stt_settings.quota_tz)
        before = repo.transcribed_today(stt_settings.db_path, 1, day)
        self._edit(stt_client, gen_id, "שלום חברים ואחרים")
        assert repo.transcribed_today(stt_settings.db_path, 1, day) == before
