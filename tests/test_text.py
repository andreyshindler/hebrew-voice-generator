"""Hebrew text preparation."""

import pytest

from hebrew_voice import text as T


def test_strips_niqqud_and_cantillation():
    assert T.strip_niqqud("שָׁלוֹם עוֹלָם") == "שלום עולם"
    # Precomposed forms decompose first, so their points go too.
    assert T.strip_niqqud("שׁ") == "ש"


def test_keeps_letters_when_niqqud_is_retained():
    assert T.prepare("שָׁלוֹם", keep_niqqud=True) == "שָׁלוֹם"


def test_normalizes_geresh_and_gershayim():
    assert T.normalize_geresh("צה״ל עמ׳") == 'צה"ל עמ\''


def test_expands_abbreviations_at_word_boundaries():
    assert "על ידי" in T.prepare("נכתב ע\"י דני")
    assert "עמוד" in T.prepare("ראו עמ׳ 12")
    # A key that appears inside a longer word must not be replaced.
    assert T.prepare("מסלול") == "מסלול"


def test_acronyms_pronounced_as_words_lose_their_gershayim():
    assert T.prepare('צה"ל') == "צהל"
    assert T.prepare("מנכ״ל החברה").startswith("מנכל")


def test_symbols_are_read_aloud():
    prepared = T.prepare("גדל ב‑50% והרוויח 100 ₪")
    assert "אחוז" in prepared
    assert "שקלים" in prepared


def test_currency_prefix_moves_after_the_amount():
    assert T.prepare("$50") == "50 דולר"
    assert T.prepare("₪1,200") == "1,200 שקלים"


def test_symbols_can_be_left_alone():
    assert "%" in T.prepare("50%", symbols=False)


def test_bidi_control_characters_are_removed():
    assert T.prepare("‏שלום‎") == "שלום"


def test_maqaf_becomes_a_space():
    assert T.prepare("בית־ספר") == "בית ספר"


def test_prepare_is_idempotent():
    raw = 'ד"ר כהן שילם 20 ₪, עמ׳ 3'
    once = T.prepare(raw)
    assert T.prepare(once) == once


def test_has_hebrew():
    assert T.has_hebrew("שלום")
    assert not T.has_hebrew("hello 123")


def test_billable_chars_counts_the_raw_input():
    assert T.billable_chars("  שלום  ") == len("שלום")


def test_estimate_seconds_scales_with_rate():
    slow = T.estimate_seconds("א" * 145, 0)
    fast = T.estimate_seconds("א" * 145, 100)
    assert slow == pytest.approx(10.0, abs=0.2)
    assert fast < slow


class TestChunking:
    def test_splits_by_sentence_within_the_limit(self):
        text = "משפט ראשון. משפט שני. משפט שלישי."
        chunks = T.chunk_text(text, max_chars=20)
        assert all(len(c) <= 20 for c in chunks)
        assert "".join(chunks).replace(" ", "") == text.replace(" ", "")

    def test_splits_by_paragraph(self):
        chunks = T.chunk_text("פסקה אחת\n\nפסקה שתיים", max_chars=100, by="paragraph")
        assert chunks == ["פסקה אחת", "פסקה שתיים"]

    def test_one_enormous_word_is_hard_split(self):
        chunks = T.chunk_text("א" * 5000, max_chars=1000)
        assert len(chunks) == 5
        assert all(len(c) <= 1000 for c in chunks)

    def test_continuation_chunks_do_not_start_with_a_delimiter(self):
        text = "א" * 30 + ", " + "ב" * 30
        for chunk in T.chunk_text(text, max_chars=40, by="chars"):
            assert not chunk.startswith(",")

    def test_rejects_a_bad_mode(self):
        with pytest.raises(ValueError):
            T.chunk_text("שלום", by="nonsense")
