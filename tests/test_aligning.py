"""Re-timing a transcript when its words are corrected."""

from __future__ import annotations

import pytest

from hebrew_voice.aligning import realign, split_words
from hebrew_voice.synth import Cue


def words(cues):
    return [c.text for c in cues]


def spans(cues):
    return [(round(c.start, 3), round(c.end, 3)) for c in cues]


@pytest.fixture
def heard():
    return [Cue(0.0, 0.4, "כך"), Cue(0.5, 1.1, "הגינה"), Cue(1.2, 1.8, "נראית")]


class TestCorrections:
    def test_a_swapped_word_keeps_its_own_slot(self, heard):
        """The common edit, and the one that has to be exact.

        Fixing a misheard name must not nudge anything else - every other word
        was recognised correctly and is already in the right place.
        """
        out = realign(heard, ["כך", "החצר", "נראית"])
        assert words(out) == ["כך", "החצר", "נראית"]
        assert spans(out) == [(0.0, 0.4), (0.5, 1.1), (1.2, 1.8)]

    def test_a_deleted_word_leaves_its_neighbours_alone(self, heard):
        """The freed time becomes silence rather than padding a neighbour.

        Stretching the word before it over the gap would claim the speaker
        took longer over it than they did.
        """
        out = realign(heard, ["כך", "נראית"])
        assert words(out) == ["כך", "נראית"]
        assert spans(out) == [(0.0, 0.4), (1.2, 1.8)]

    def test_an_inserted_word_takes_the_pause_it_was_written_into(self, heard):
        out = realign(heard, ["כך", "שלי", "הגינה", "נראית"])
        assert words(out) == ["כך", "שלי", "הגינה", "נראית"]
        # It fits inside the 0.4-0.5 gap, so the recognised words do not move.
        assert spans(out)[0] == (0.0, 0.4)
        assert spans(out)[2] == (0.5, 1.1)
        assert 0.4 <= out[1].start and out[1].end <= 0.5

    def test_splitting_a_word_divides_its_window(self, heard):
        out = realign(heard, ["כך", "ה", "גינה", "נראית"])
        assert words(out) == ["כך", "ה", "גינה", "נראית"]
        # The two halves stay inside what the one word occupied.
        assert out[1].start == 0.5
        assert out[2].end <= 1.1
        # Divided by length, so the longer half gets the longer slice.
        assert out[2].duration > out[1].duration

    def test_joining_two_words_spans_both(self, heard):
        out = realign(heard, ["כך", "הגינהנראית"])
        assert words(out) == ["כך", "הגינהנראית"]
        assert spans(out)[1] == (0.5, 1.8)

    def test_nothing_moves_when_nothing_changed(self, heard):
        out = realign(heard, [c.text for c in heard])
        assert spans(out) == spans(heard)


class TestEdges:
    def test_appending_past_the_end_uses_the_recording(self, heard):
        out = realign(heard, [*[c.text for c in heard], "באמת"], duration=3.0)
        assert words(out)[-1] == "באמת"
        assert out[-1].start >= 1.8
        assert out[-1].end <= 3.0

    def test_an_insert_with_nowhere_to_go_still_gets_a_slot(self):
        """Two words with no pause between them, and a third written in.

        It has to land somewhere: collapsing it onto a single instant would
        make a cue the subtitle writer cannot show.
        """
        heard = [Cue(0.0, 0.5, "אחת"), Cue(0.5, 1.0, "שתיים")]
        out = realign(heard, ["אחת", "חדשה", "שתיים"])
        assert words(out) == ["אחת", "חדשה", "שתיים"]
        assert out[1].duration > 0

    def test_an_empty_transcript_is_empty(self, heard):
        assert realign(heard, []) == []
        assert realign(heard, ["", "  "]) == []

    def test_words_without_timings_spread_over_the_recording(self):
        out = realign([], ["אחת", "שתיים", "שלוש"], duration=3.0)
        assert words(out) == ["אחת", "שתיים", "שלוש"]
        assert out[0].start == 0.0
        assert out[-1].end <= 3.0

    def test_a_full_rewrite_stays_inside_the_window(self, heard):
        """The honest limit: it does not drift outside what was said.

        Nothing survives to anchor to, so the timings are a guess - but a
        guess that still starts and ends where the speech did.
        """
        out = realign(heard, ["משהו", "אחר", "לגמרי", "נאמר", "כאן"])
        assert out[0].start == 0.0
        assert out[-1].end <= 1.8

    def test_cues_never_run_backwards(self, heard):
        out = realign(heard, ["כך", "ה", "גינה", "הזאת", "נראית", "טוב"])
        for cue in out:
            assert cue.end >= cue.start
        for first, second in zip(out, out[1:]):
            assert second.start >= first.start


class TestSplitWords:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("אחת שתיים", ["אחת", "שתיים"]),
            ("  אחת   שתיים  ", ["אחת", "שתיים"]),
            ("אחת\nשתיים", ["אחת", "שתיים"]),
            ("", []),
        ],
    )
    def test_whitespace_is_what_separates_words(self, text, expected):
        assert split_words(text) == expected
