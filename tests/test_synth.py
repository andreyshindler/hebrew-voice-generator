"""The synthesis core, against the fake engine."""

import asyncio

import pytest

from hebrew_voice import synth
from hebrew_voice.synth import (
    Cue,
    SynthesisOptions,
    dump_cues,
    group_cues,
    load_cues,
    merge_cues,
    synthesize,
    to_srt,
    to_vtt,
)

from . import fakes


def word_cues(words, *, step=0.35, length=0.3):
    """Per-word cues laid out like the engine's boundary events."""
    return [Cue(i * step, i * step + length, word) for i, word in enumerate(words)]


class TestOptions:
    def test_normalizes_loose_values(self):
        opts = SynthesisOptions(voice="hila", rate="10", pitch="-5hz", volume="+0")
        assert (opts.rate, opts.pitch, opts.volume) == ("+10%", "-5Hz", "+0%")
        assert opts.voice == "he-IL-HilaNeural"

    def test_from_numbers_builds_the_wire_format(self):
        opts = SynthesisOptions.from_numbers("avri", rate=10, pitch=-5, volume=3)
        assert (opts.rate, opts.pitch, opts.volume) == ("+10%", "-5Hz", "+3%")

    @pytest.mark.parametrize("field,value", [("rate", "fast"), ("pitch", "high"), ("volume", "?")])
    def test_rejects_nonsense(self, field, value):
        with pytest.raises(ValueError):
            SynthesisOptions(**{field: value})

    def test_rejects_an_unknown_voice(self):
        with pytest.raises(ValueError):
            SynthesisOptions(voice="nosuchvoice")


class TestSubtitles:
    def test_srt_format(self):
        cues = [Cue(0.0, 1.5, "שלום"), Cue(1.5, 2.25, "עולם")]
        assert to_srt(cues) == (
            "1\n00:00:00,000 --> 00:00:01,500\nשלום\n\n"
            "2\n00:00:01,500 --> 00:00:02,250\nעולם\n"
        )

    def test_vtt_starts_with_the_signature_and_uses_dots(self):
        vtt = to_vtt([Cue(0.0, 1.5, "שלום")])
        assert vtt.startswith("WEBVTT")
        assert "00:00:00.000 --> 00:00:01.500" in vtt

    def test_merge_respects_the_word_limit(self):
        cues = [Cue(i * 0.3, i * 0.3 + 0.3, f"מ{i}") for i in range(10)]
        merged = merge_cues(cues, max_words=3, max_chars=999)
        assert all(len(c.text.split()) <= 3 for c in merged)

    def test_merge_breaks_on_a_long_pause(self):
        cues = [Cue(0, 0.4, "שלום"), Cue(3.0, 3.4, "עולם")]
        assert len(merge_cues(cues, max_gap=0.5)) == 2

    def test_merge_breaks_after_a_sentence_end(self):
        cues = [Cue(0, 0.4, "שלום."), Cue(0.4, 0.8, "עולם")]
        assert len(merge_cues(cues)) == 2


class TestCaptionDensity:
    """``group_cues`` - the knob behind the karaoke and trailing styles."""

    words = ["שלום", "עולם", "זהו", "מבחן", "קצר", "מאוד", "של", "כתוביות", "בעברית"]

    def test_one_word_per_cue_is_one_cue_per_word(self):
        grouped = group_cues(word_cues(self.words), words_per_cue=1)
        assert [c.text for c in grouped] == self.words

    def test_three_words_per_cue(self):
        grouped = group_cues(word_cues(self.words), words_per_cue=3)
        assert len(grouped) == 3
        assert all(len(c.text.split()) == 3 for c in grouped)

    def test_the_char_cap_does_not_quietly_override_the_word_count(self):
        # merge_cues applies max_words and max_chars independently, so a char
        # cap tuned for readable lines would cap this at about six words.
        long_words = ["התייעלות"] * 12  # 8 characters each, 107 with spaces
        grouped = group_cues(word_cues(long_words), words_per_cue=12)
        assert len(grouped) == 1
        assert len(grouped[0].text.split()) == 12

    def test_the_default_still_produces_readable_lines(self):
        # Unchanged from before the control existed: 7 words, 42 characters.
        assert group_cues(word_cues(self.words)) == merge_cues(word_cues(self.words))

    def test_timings_are_untouched_by_grouping(self):
        cues = word_cues(self.words)
        grouped = group_cues(cues, words_per_cue=1)
        assert [(c.start, c.end) for c in grouped] == [(c.start, c.end) for c in cues]

    def test_min_duration_stretches_a_short_cue(self):
        cues = [Cue(0.0, 0.15, "שלום"), Cue(1.0, 1.5, "עולם")]
        grouped = group_cues(cues, words_per_cue=1, min_duration=0.5)
        assert grouped[0].end == pytest.approx(0.5)

    def test_min_duration_never_runs_into_the_next_cue(self):
        cues = [Cue(0.0, 0.15, "שלום"), Cue(0.2, 0.7, "עולם")]
        grouped = group_cues(cues, words_per_cue=1, min_duration=0.5)
        assert grouped[0].end == pytest.approx(0.2)
        assert grouped[0].end <= grouped[1].start

    def test_min_duration_never_shortens_a_cue(self):
        cues = [Cue(0.0, 2.0, "שלום")]
        assert group_cues(cues, words_per_cue=1, min_duration=0.5)[0].end == 2.0

    def test_punctuation_stripping_leaves_the_timings_alone(self):
        cues = [Cue(0.0, 0.4, "שלום,"), Cue(0.4, 0.8, "עולם.")]
        grouped = group_cues(cues, words_per_cue=1, strip_punctuation=True)
        assert [c.text for c in grouped] == ["שלום", "עולם"]
        assert [(c.start, c.end) for c in grouped] == [(0.0, 0.4), (0.4, 0.8)]

    def test_punctuation_stripping_happens_after_the_sentence_break(self):
        # Stripping first would erase the very marks merge_cues breaks on.
        cues = [Cue(0.0, 0.4, "שלום."), Cue(0.4, 0.8, "עולם")]
        grouped = group_cues(cues, words_per_cue=7, strip_punctuation=True)
        assert [c.text for c in grouped] == ["שלום", "עולם"]

    def test_a_cue_that_is_only_punctuation_is_not_emptied(self):
        grouped = group_cues([Cue(0.0, 0.4, "...")], words_per_cue=1, strip_punctuation=True)
        assert grouped[0].text == "..."

    @pytest.mark.parametrize("bad", [0, -1, 21])
    def test_rejects_a_density_outside_the_range(self, bad):
        with pytest.raises(ValueError):
            group_cues(word_cues(self.words), words_per_cue=bad)

    def test_cues_survive_a_round_trip_through_storage(self):
        cues = word_cues(self.words)
        loaded = load_cues(dump_cues(cues))
        assert [c.text for c in loaded] == [c.text for c in cues]
        # Stored to the millisecond, which is all SRT and VTT can express.
        for stored, original in zip(loaded, cues):
            assert stored.start == pytest.approx(original.start, abs=5e-4)
            assert stored.end == pytest.approx(original.end, abs=5e-4)

    def test_dumped_cues_keep_hebrew_readable(self):
        assert "שלום" in dump_cues([Cue(0.0, 0.4, "שלום")])

    @pytest.mark.parametrize("junk", ["", "{}", '{"version": 99, "cues": []}', "[1,2]"])
    def test_unreadable_cue_files_raise_valueerror(self, junk):
        with pytest.raises(ValueError):
            load_cues(junk)


class TestSynthesize:
    @pytest.mark.anyio
    async def test_returns_audio_without_writing_a_file(self, monkeypatch):
        fakes.install(monkeypatch, synth)
        result = await synthesize("שלום עולם")
        assert result.path is None
        assert result.audio  # this is the regression that motivated the change
        assert result.audio_bytes == len(result.audio)
        assert result.cues and result.duration > 0

    @pytest.mark.anyio
    async def test_writes_audio_and_subtitles(self, monkeypatch, tmp_path):
        fakes.install(monkeypatch, synth)
        out = tmp_path / "nested" / "out.mp3"
        result = await synthesize(
            "שלום עולם", out, srt=tmp_path / "out.srt", vtt=tmp_path / "out.vtt"
        )
        assert out.read_bytes() == result.audio
        assert (tmp_path / "out.srt").read_text(encoding="utf-8").startswith("1\n")
        assert (tmp_path / "out.vtt").read_text(encoding="utf-8").startswith("WEBVTT")

    @pytest.mark.anyio
    async def test_settings_reach_the_engine(self, monkeypatch):
        fakes.install(monkeypatch, synth)
        opts = SynthesisOptions.from_numbers("avri", rate=10, pitch=-5, volume=3)
        await synthesize("שלום", None, opts)
        call = fakes.FakeCommunicate.instances[-1]
        assert call.voice == "he-IL-AvriNeural"
        assert call.kwargs["rate"] == "+10%"
        assert call.kwargs["pitch"] == "-5Hz"
        assert call.kwargs["volume"] == "+3%"

    @pytest.mark.anyio
    async def test_text_is_prepared_before_being_sent(self, monkeypatch):
        fakes.install(monkeypatch, synth)
        await synthesize("שָׁלוֹם ע\"י דני")
        assert fakes.FakeCommunicate.instances[-1].text == "שלום על ידי דני"

    @pytest.mark.anyio
    async def test_already_prepared_text_is_passed_through(self, monkeypatch):
        fakes.install(monkeypatch, synth)
        await synthesize("שָׁלוֹם", None, already_prepared=True)
        assert fakes.FakeCommunicate.instances[-1].text == "שָׁלוֹם"

    @pytest.mark.anyio
    async def test_empty_text_raises(self, monkeypatch):
        fakes.install(monkeypatch, synth)
        with pytest.raises(ValueError):
            await synthesize("   ")

    @pytest.mark.anyio
    async def test_retries_a_transient_failure(self, monkeypatch):
        factory = fakes.failing_factory(1)
        fakes.install(monkeypatch, synth, factory)
        slept = []

        async def fake_sleep(delay):
            slept.append(delay)

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        result = await synthesize("שלום", None, SynthesisOptions(retries=3))
        assert result.audio
        assert factory.state["calls"] == 2
        assert slept == [1.0]

    @pytest.mark.anyio
    async def test_gives_up_after_the_retry_budget(self, monkeypatch):
        fakes.install(monkeypatch, synth, fakes.failing_factory(99))

        async def fake_sleep(delay):
            return None

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        with pytest.raises(ConnectionError):
            await synthesize("שלום", None, SynthesisOptions(retries=2))

    @pytest.mark.anyio
    async def test_does_not_retry_a_programming_error(self, monkeypatch):
        fakes.install(monkeypatch, synth, fakes.failing_factory(99, TypeError("bad call")))
        with pytest.raises(TypeError):
            await synthesize("שלום", None, SynthesisOptions(retries=3))

    @pytest.mark.anyio
    async def test_handles_a_response_with_no_audio(self, monkeypatch):
        fakes.install(monkeypatch, synth, fakes.silent_factory)
        result = await synthesize("שלום")
        assert result.audio == b""
        assert result.cues

    @pytest.mark.anyio
    async def test_split_writes_one_file_per_chunk(self, monkeypatch, tmp_path):
        fakes.install(monkeypatch, synth)
        results = await synth.synthesize_split(
            "פסקה אחת\n\nפסקה שתיים\n\nפסקה שלוש", tmp_path, by="paragraph"
        )
        assert len(results) == 3
        assert sorted(p.name for p in tmp_path.glob("*.mp3")) == [
            "part-001.mp3",
            "part-002.mp3",
            "part-003.mp3",
        ]
        # Batch results don't hold the audio in memory.
        assert all(r.audio == b"" for r in results)
