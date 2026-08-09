"""The synthesis endpoint, preview, and artifact downloads."""

import pytest

from . import fakes
from .conftest import csrf


def generate(client, **overrides):
    body = {"text": "שלום עולם"}
    body.update(overrides)
    return client.post("/api/synthesize", json=body, headers=csrf(client))


class TestSynthesize:
    def test_happy_path(self, auth_client, fake_tts):
        response = generate(auth_client)
        assert response.status_code == 200
        data = response.json()
        assert data["char_count"] == len("שלום עולם")
        assert data["duration"] > 0
        assert data["audio_bytes"] > 0
        assert data["urls"]["audio"].endswith("/audio.mp3")
        assert data["quota"]["used_today"] == data["char_count"]

    def test_text_is_cleaned_before_it_is_spoken(self, auth_client, fake_tts):
        response = generate(auth_client, text='שָׁלוֹם ע"י דני')
        assert response.json()["prepared_text"] == "שלום על ידי דני"

    def test_slider_values_reach_the_engine(self, auth_client, fake_tts):
        generate(auth_client, voice="he-IL-AvriNeural", rate=10, pitch=-5, volume=3)
        call = fakes.FakeCommunicate.instances[-1]
        assert call.voice == "he-IL-AvriNeural"
        assert (call.kwargs["rate"], call.kwargs["pitch"], call.kwargs["volume"]) == (
            "+10%",
            "-5Hz",
            "+3%",
        )

    def test_cleanup_toggles_are_honoured(self, auth_client, fake_tts):
        response = generate(auth_client, text="עולה 50%", expand_symbols=False)
        assert "%" in response.json()["prepared_text"]

    def test_niqqud_can_be_kept(self, auth_client, fake_tts):
        response = generate(auth_client, text="שָׁלוֹם", keep_niqqud=True)
        assert response.json()["prepared_text"] == "שָׁלוֹם"

    def test_rejects_text_over_the_limit(self, auth_client, fake_tts, settings):
        response = generate(auth_client, text="א" * (settings.max_chars + 1))
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "text_too_long"

    def test_rejects_blank_text(self, auth_client, fake_tts):
        assert generate(auth_client, text="   ").status_code == 422

    def test_rejects_a_non_hebrew_voice(self, auth_client, fake_tts):
        response = generate(auth_client, voice="en-US-AriaNeural")
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "unknown_voice"

    @pytest.mark.parametrize(
        "field,value", [("rate", 500), ("pitch", -900), ("volume", 99)]
    )
    def test_rejects_out_of_range_sliders(self, auth_client, fake_tts, field, value):
        assert generate(auth_client, **{field: value}).status_code == 422

    def test_upstream_failure_becomes_502(self, auth_client, monkeypatch):
        from hebrew_voice import synth

        fakes.install(monkeypatch, synth, fakes.failing_factory(99))
        response = generate(auth_client)
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "tts_upstream_failed"

    def test_silent_upstream_becomes_502(self, auth_client, monkeypatch):
        from hebrew_voice import synth

        fakes.install(monkeypatch, synth, fakes.silent_factory)
        assert generate(auth_client).status_code == 502

    def test_a_failed_run_does_not_consume_quota(self, auth_client, monkeypatch):
        from hebrew_voice import synth

        fakes.install(monkeypatch, synth, fakes.failing_factory(99))
        generate(auth_client)
        assert auth_client.get("/api/auth/me").json()["limits"]["used_today"] == 0

    def test_subtitles_can_be_skipped(self, auth_client, fake_tts):
        data = generate(auth_client, subtitles=False).json()
        assert data["urls"]["srt"] is None
        assert auth_client.get(f"/api/generations/{data['id']}/subtitles.srt").status_code == 404


class TestArtifacts:
    @pytest.fixture
    def generation(self, auth_client, fake_tts):
        return generate(auth_client).json()

    def test_audio_is_served_as_mp3(self, auth_client, generation):
        response = auth_client.get(generation["urls"]["audio"])
        assert response.status_code == 200
        assert response.headers["content-type"] == "audio/mpeg"
        assert len(response.content) == generation["audio_bytes"]

    def test_audio_supports_range_requests(self, auth_client, generation):
        response = auth_client.get(
            generation["urls"]["audio"], headers={"Range": "bytes=0-9"}
        )
        assert response.status_code == 206
        assert len(response.content) == 10

    def test_download_sets_a_filename_that_survives_hebrew(self, auth_client, generation):
        header = auth_client.get(
            generation["urls"]["audio"] + "?download=1"
        ).headers["content-disposition"]
        assert header.startswith("attachment")
        assert "filename*=UTF-8''" in header

    def test_srt_and_vtt_are_served(self, auth_client, generation):
        srt = auth_client.get(generation["urls"]["srt"])
        vtt = auth_client.get(generation["urls"]["vtt"])
        assert srt.text.startswith("1\n")
        assert vtt.text.startswith("WEBVTT")
        # The <track> element cannot load an attachment.
        assert "attachment" not in vtt.headers.get("content-disposition", "")
        assert vtt.headers["content-type"].startswith("text/vtt")


def cue_count(body: str) -> int:
    """Cues in an SRT body, counted by its blank-line-separated blocks."""
    return len([block for block in body.strip().split("\n\n") if block.strip()])


def cue_texts(body: str) -> list:
    """Just the spoken lines of an SRT body - no indices, no timestamps."""
    return [
        block.strip().split("\n")[-1]
        for block in body.strip().split("\n\n")
        if "-->" in block
    ]


class TestCaptionDensity:
    """Re-rendering subtitles from the stored word timings."""

    @pytest.fixture
    def generation(self, auth_client, fake_tts):
        # Six words, so a default 7-word cue holds all of them.
        return generate(auth_client, text="אחת שתיים שלוש ארבע חמש שש").json()

    def test_a_new_generation_advertises_that_it_can_be_regrouped(self, generation):
        assert generation["can_regroup"] is True

    def test_no_parameters_serves_the_stored_file_untouched(self, auth_client, generation):
        response = auth_client.get(generation["urls"]["srt"])
        assert cue_count(response.text) == 1
        # Still off disk, so still immutable.
        assert "immutable" in response.headers["cache-control"]

    def test_one_word_per_cue_gives_a_cue_per_word(self, auth_client, generation):
        response = auth_client.get(generation["urls"]["srt"] + "?words=1")
        assert response.status_code == 200
        assert cue_count(response.text) == 6
        assert response.headers["content-type"].startswith("text/plain")

    def test_a_regrouped_body_is_not_cached_as_immutable(self, auth_client, generation):
        response = auth_client.get(generation["urls"]["srt"] + "?words=1")
        assert "immutable" not in response.headers["cache-control"]
        # ...but it is still a download with a sensible filename.
        assert response.headers["content-disposition"].startswith("attachment")

    def test_the_vtt_track_can_be_regrouped_too(self, auth_client, generation):
        response = auth_client.get(generation["urls"]["vtt"] + "?words=2")
        assert response.text.startswith("WEBVTT")
        assert response.headers["content-type"].startswith("text/vtt")
        assert "attachment" not in response.headers.get("content-disposition", "")
        assert cue_count(response.text) == 4  # the header block, then three cues

    def test_punctuation_can_be_stripped_without_changing_the_density(
        self, auth_client, fake_tts
    ):
        created = generate(auth_client, text="שלום, עולם.").json()
        plain = cue_texts(auth_client.get(created["urls"]["srt"] + "?words=1").text)
        stripped = cue_texts(
            auth_client.get(created["urls"]["srt"] + "?words=1&strip_punctuation=1").text
        )
        assert plain == ["שלום,", "עולם."]
        assert stripped == ["שלום", "עולם"]

    def test_a_minimum_duration_stretches_short_cues(self, auth_client, generation):
        # The fake's cues butt up against each other, so only the last one has
        # room to grow - which is exactly the guarantee worth checking here.
        plain = auth_client.get(generation["urls"]["srt"] + "?words=1").text
        stretched = auth_client.get(
            generation["urls"]["srt"] + "?words=1&min_duration=1"
        ).text
        assert plain.rstrip().endswith("00:00:01,750 --> 00:00:02,100\nשש")
        assert stretched.rstrip().endswith("00:00:01,750 --> 00:00:02,750\nשש")

    @pytest.mark.parametrize("bad", ["0", "21", "-3", "many"])
    def test_rejects_a_density_outside_the_range(self, auth_client, generation, bad):
        assert auth_client.get(
            generation["urls"]["srt"] + f"?words={bad}"
        ).status_code == 422

    def test_a_recording_from_before_the_feature_cannot_be_regrouped(
        self, auth_client, settings, generation
    ):
        # Simulate a row written before migration 3 added the cue timings.
        from hebrew_voice.db import connect

        with connect(settings.db_path) as conn:
            conn.execute(
                "UPDATE generations SET cues_rel = NULL WHERE id = ?", (generation["id"],)
            )
        assert auth_client.get(f"/api/generations/{generation['id']}").json()[
            "can_regroup"
        ] is False
        response = auth_client.get(generation["urls"]["srt"] + "?words=1")
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "cues_unavailable"
        # The stored file is still served, so nothing regresses for old rows.
        assert auth_client.get(generation["urls"]["srt"]).status_code == 200

    def test_a_missing_cue_file_is_reported_not_crashed(
        self, auth_client, settings, generation
    ):
        for stray in (settings.data_dir / "audio").rglob("*.cues.json"):
            stray.unlink()
        response = auth_client.get(generation["urls"]["srt"] + "?words=1")
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "cues_unavailable"

    def test_the_initial_density_is_baked_into_the_stored_files(
        self, auth_client, fake_tts
    ):
        created = generate(auth_client, text="אחת שתיים שלוש ארבע", words_per_cue=1).json()
        assert cue_count(auth_client.get(created["urls"]["srt"]).text) == 4

    def test_the_stored_density_is_reported_back(self, auth_client, fake_tts):
        created = generate(auth_client, words_per_cue=2).json()
        assert created["words_per_cue"] == 2
        # ...and survives into the history list, so replaying a recording
        # shows the density it was actually rendered at.
        listed = auth_client.get("/api/generations").json()["items"][0]
        assert listed["words_per_cue"] == 2

    def test_the_density_defaults_to_readable_lines(self, generation):
        assert generation["words_per_cue"] == 7

    @pytest.mark.parametrize("bad", [0, 21, -1])
    def test_rejects_an_initial_density_outside_the_range(self, auth_client, fake_tts, bad):
        assert generate(auth_client, words_per_cue=bad).status_code == 422

    def test_a_generation_without_subtitles_keeps_no_cue_file(
        self, auth_client, settings, fake_tts
    ):
        created = generate(auth_client, subtitles=False).json()
        assert created["can_regroup"] is False
        assert not list((settings.data_dir / "audio").rglob("*.cues.json"))


class TestPreview:
    def test_shows_the_prepared_text_without_calling_the_engine(self, auth_client):
        # Note: no fake installed - a network call would fail the test.
        response = auth_client.post("/api/preview", json={"text": 'שָׁלוֹם ע"י דני'})
        assert response.status_code == 200
        data = response.json()
        assert data["prepared"] == "שלום על ידי דני"
        assert data["has_hebrew"] is True
        assert data["estimated_seconds"] > 0

    def test_respects_the_toggles(self, auth_client):
        response = auth_client.post(
            "/api/preview", json={"text": "עולה 50%", "expand_symbols": False}
        )
        assert "%" in response.json()["prepared"]

    def test_costs_no_quota(self, auth_client):
        auth_client.post("/api/preview", json={"text": "שלום עולם"})
        assert auth_client.get("/api/auth/me").json()["limits"]["used_today"] == 0

    def test_rejects_text_over_the_limit(self, auth_client, settings):
        response = auth_client.post(
            "/api/preview", json={"text": "א" * (settings.max_chars + 1)}
        )
        assert response.status_code == 413


class TestVoices:
    def test_catalog_is_served_without_the_network(self, auth_client):
        voices = auth_client.get("/api/voices").json()["voices"]
        assert {v["id"] for v in voices} == {"he-IL-HilaNeural", "he-IL-AvriNeural"}
        assert sum(1 for v in voices if v["default"]) == 1
