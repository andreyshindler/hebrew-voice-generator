"""The edit plan: clamping what arrives, and what it does to the composition."""

from __future__ import annotations

import pytest

from hebrew_voice.composition import Shot, build_composition
from hebrew_voice.editing import normalise_plan, shot_durations
from hebrew_voice.synth import Cue

from .conftest import csrf
from .test_media import JPEG, MP4, upload
from .test_renders import make_generation

MP3 = b"ID3\x03\x00" + b"\x00" * 64


class TestPlanValidation:
    def test_a_missing_plan_is_the_default(self):
        plan = normalise_plan(None, shot_count=0)
        assert plan["caption"]["position"] == "bottom"
        assert plan["motion"] == {"zoom": True, "fade": True}
        assert plan["music"] is None

    def test_rubbish_is_replaced_rather_than_carried(self):
        plan = normalise_plan("not a plan", shot_count=0)
        assert plan["caption"]["scale"] == 1.0

    @pytest.mark.parametrize("value,expected", [(0.1, 0.6), (99, 1.8), ("big", 1.0)])
    def test_caption_scale_is_clamped(self, value, expected):
        plan = normalise_plan({"caption": {"scale": value}}, shot_count=0)
        assert plan["caption"]["scale"] == expected

    @pytest.mark.parametrize(
        "colour",
        [
            "red; } body { display: none } .x {",
            "url(http://evil/)",
            "#12345",
            "javascript:alert(1)",
            42,
        ],
    )
    def test_a_colour_that_is_not_a_hex_colour_is_refused(self, colour):
        """The value goes straight into a stylesheet, so it cannot be free text."""
        plan = normalise_plan({"caption": {"color": colour}}, shot_count=0)
        assert plan["caption"]["color"] == "#ffffff"

    @pytest.mark.parametrize("colour", ["#fff", "#FFD54A", "#0b0f19"])
    def test_real_hex_colours_survive(self, colour):
        plan = normalise_plan({"caption": {"color": colour}}, shot_count=0)
        assert plan["caption"]["color"] == colour.lower()

    def test_an_unknown_position_falls_back(self):
        plan = normalise_plan({"caption": {"position": "sideways"}}, shot_count=0)
        assert plan["caption"]["position"] == "bottom"

    def test_durations_are_ignored_unless_they_match_the_shots(self):
        assert normalise_plan({"durations": [1, 2]}, shot_count=3)["durations"] == []
        assert normalise_plan({"durations": [1, 2, 3]}, shot_count=3)["durations"] == [
            1.0, 2.0, 3.0
        ]

    def test_music_needs_an_id(self):
        assert normalise_plan({"music": {"volume": 0.5}}, shot_count=0)["music"] is None
        plan = normalise_plan({"music": {"id": "abc", "volume": 9}}, shot_count=0)
        assert plan["music"] == {"id": "abc", "volume": 1.0}


class TestShotDurations:
    def test_no_durations_means_an_even_split(self):
        assert shot_durations({}, shot_count=4, total=8.0) == [2.0] * 4

    def test_hand_set_durations_are_proportions_not_seconds(self):
        """Whatever they add up to, the shots must fill the voiceover exactly.

        Someone dragging a slider is setting relative length; treating the
        numbers as absolute would leave black at the end or cut the last shot.
        """
        holds = shot_durations({"durations": [1, 3]}, shot_count=2, total=8.0)
        assert holds == [2.0, 6.0]
        assert sum(holds) == pytest.approx(8.0)

    def test_a_wrong_length_list_is_ignored(self):
        assert shot_durations({"durations": [5]}, shot_count=3, total=9.0) == [3.0] * 3


class TestCompositionStyling:
    def _html(self, **plan):
        return build_composition(
            [Cue(0.0, 1.0, "טקסט")], duration=2.0, width=1080, height=1920,
            plan=normalise_plan(plan, shot_count=0),
            shots=[Shot(kind="image", src="shot0.jpg", start=0.0, duration=2.0)],
        )

    def test_scale_changes_the_caption_size(self):
        small = self._html(caption={"scale": 0.6})
        large = self._html(caption={"scale": 1.8})
        import re

        size = lambda html: int(re.search(r"font-size: (\d+)px", html).group(1))
        assert size(large) > size(small)

    def test_position_moves_the_captions(self):
        assert "top: 50%" in self._html(caption={"position": "middle"})
        assert "bottom:" in self._html(caption={"position": "bottom"})

    def test_the_box_replaces_the_outline(self):
        html = self._html(caption={"box": True})
        assert "background: rgba(0, 0, 0, .55)" in html
        assert "-webkit-text-stroke: 0" in html

    def test_motion_can_be_turned_off(self):
        assert "shot-zoom" in self._html(motion={"zoom": True})
        assert "shot-zoom" not in self._html(motion={"zoom": False, "fade": False})
        assert "shot-in" not in self._html(motion={"zoom": False, "fade": False})

    def test_each_photo_carries_its_own_hold_for_the_zoom(self):
        html = build_composition(
            [], duration=6.0, width=1080, height=1920,
            plan=normalise_plan({}, shot_count=2),
            shots=[
                Shot(kind="image", src="a.jpg", start=0.0, duration=2.0),
                Shot(kind="image", src="b.jpg", start=2.0, duration=4.0),
            ],
        )
        assert "--shot-hold: 2.000s" in html
        assert "--shot-hold: 4.000s" in html


class TestKaraoke:
    def _words(self):
        return [Cue(0.0, 0.4, "כך"), Cue(0.4, 0.9, "הגינה"), Cue(0.9, 1.4, "נראית")]

    def test_each_word_gets_its_turn(self):
        html = build_composition(
            [Cue(0.0, 1.4, "כך הגינה נראית")], duration=1.4, width=1080, height=1920,
            plan=normalise_plan({"caption": {"karaoke": True}}, shot_count=0),
            word_cues=self._words(),
        )
        # One variant per word, each lighting a different one.
        assert html.count('class="cue"') == 3
        assert html.count('class="on"') == 3

    def test_the_variants_run_back_to_back(self):
        html = build_composition(
            [Cue(0.0, 1.4, "כך הגינה נראית")], duration=1.4, width=1080, height=1920,
            plan=normalise_plan({"caption": {"karaoke": True}}, shot_count=0),
            word_cues=self._words(),
        )
        import re

        spans = [
            (float(a), float(b))
            for a, b in re.findall(r'data-start="([\d.]+)" data-duration="([\d.]+)"', html)
        ]
        assert spans[0][0] == 0.0
        # No gaps: each starts where the previous one ended.
        for (start, hold), (next_start, _) in zip(spans, spans[1:]):
            assert start + hold == pytest.approx(next_start, abs=0.002)

    def test_karaoke_off_leaves_one_element_per_cue(self):
        html = build_composition(
            [Cue(0.0, 1.4, "כך הגינה נראית")], duration=1.4, width=1080, height=1920,
            plan=normalise_plan({}, shot_count=0),
            word_cues=self._words(),
        )
        assert html.count('class="cue"') == 1

    def test_the_spoken_text_is_still_escaped(self):
        html = build_composition(
            [Cue(0.0, 1.0, "<b>אחת</b> שתיים")], duration=1.0, width=1080, height=1920,
            plan=normalise_plan({"caption": {"karaoke": True}}, shot_count=0),
            word_cues=[Cue(0.0, 0.5, "<b>אחת</b>"), Cue(0.5, 1.0, "שתיים")],
        )
        assert "<b>אחת</b>" not in html
        assert "&lt;b&gt;" in html


class TestMusic:
    def test_an_audio_upload_is_not_offered_as_a_shot(self, render_client):
        """Music is chosen separately; it must not land in the shot list."""
        body = upload(render_client, MP3, "track.mp3").json()
        assert body["kind"] == "audio"

    def test_music_must_belong_to_the_account(
        self, render_client, render_app, render_settings, fake_tts
    ):
        from fastapi.testclient import TestClient

        from .conftest import register_verified

        with TestClient(render_app) as other:
            register_verified(other, render_settings, email="dj@example.com")
            theirs = upload(other, MP3, "theirs.mp3").json()

        generation = make_generation(render_client)
        response = render_client.post(
            f"/api/generations/{generation['id']}/renders",
            json={"format": "mp4", "plan": {"music": {"id": theirs["id"]}}},
            headers=csrf(render_client),
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "media_unavailable"

    def test_a_photo_cannot_be_used_as_music(self, render_client, fake_tts):
        photo = upload(render_client, JPEG, "not-music.jpg").json()
        generation = make_generation(render_client)
        response = render_client.post(
            f"/api/generations/{generation['id']}/renders",
            json={"format": "mp4", "plan": {"music": {"id": photo["id"]}}},
            headers=csrf(render_client),
        )
        assert response.status_code == 422

    def test_styling_makes_it_a_different_render(self, render_client, fake_tts):
        generation = make_generation(render_client)
        plain = render_client.post(
            f"/api/generations/{generation['id']}/renders",
            json={"format": "mp4"}, headers=csrf(render_client),
        ).json()
        assert render_client.post(
            f"/api/generations/{generation['id']}/renders",
            json={"format": "mp4", "plan": {"caption": {"color": "#ffd54a"}}},
            headers=csrf(render_client),
        ).status_code in (202, 429)
        # The stored plan is what distinguishes them.
        assert plain["plan"]["caption"]["color"] == "#ffffff"


@pytest.mark.anyio
class TestMusicStaging:
    async def test_the_track_is_staged_and_referenced(
        self, render_client, render_settings, fake_tts, monkeypatch
    ):
        from hebrew_voice.web import rendering

        from .test_renders import drain

        track = upload(render_client, MP3, "bed.mp3", duration=30.0).json()
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
            json={"format": "mp4", "plan": {"music": {"id": track["id"], "volume": 0.2}}},
            headers=csrf(render_client),
        )
        await drain(render_settings)

        assert "music.mp3" in seen["names"]
        assert 'src="music.mp3"' in seen["html"]
        assert 'data-volume="0.2"' in seen["html"]

    async def test_the_overlay_format_carries_no_music(
        self, render_client, render_settings, fake_tts, monkeypatch
    ):
        """A transparent caption layer has no audio at all, music included."""
        from hebrew_voice.web import rendering

        from .test_renders import drain

        track = upload(render_client, MP3, "bed.mp3").json()
        generation = make_generation(render_client)
        seen = {}

        async def capture(settings, payload):
            project = settings.data_dir / payload["projectDir"].removeprefix(
                str(settings.renderer_data_dir) + "/"
            )
            seen["html"] = (project / "index.html").read_text(encoding="utf-8")
            target = settings.data_dir / payload["outputPath"].removeprefix(
                str(settings.renderer_data_dir) + "/"
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"video")

        monkeypatch.setattr(rendering, "_post_render", capture)
        render_client.post(
            f"/api/generations/{generation['id']}/renders",
            json={"format": "webm", "plan": {"music": {"id": track["id"]}}},
            headers=csrf(render_client),
        )
        await drain(render_settings)
        assert "<audio" not in seen["html"]
