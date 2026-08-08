"""Hebrew text preparation for text-to-speech.

Azure's Hebrew neural voices (the ones edge-tts talks to) read plain, unpointed
Hebrew best. This module cleans up the things that commonly trip them up:
niqqud and cantillation marks, Hebrew-specific punctuation, currency and math
symbols, and the abbreviations/acronyms that are written with a geresh or
gershayim.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Dict, Iterable, List, Optional

__all__ = [
    "prepare",
    "strip_niqqud",
    "normalize_geresh",
    "normalize_punctuation",
    "expand_symbols",
    "expand_abbreviations",
    "has_hebrew",
    "billable_chars",
    "estimate_seconds",
    "split_sentences",
    "split_paragraphs",
    "chunk_text",
]

# U+0591..U+05BD and U+05BF..U+05C7 cover te'amim (cantillation) and niqqud.
# U+05BE (maqaf) is deliberately excluded: it is a word joiner, not a mark.
_NIQQUD_RE = re.compile("[֑-ֽֿׁׂׄ-ׇ]")

_HEBREW_RE = re.compile("[֐-׿יִ-ﭏ]")

# A Hebrew letter or a word character - used to build "whole word" lookarounds,
# since \b behaves poorly around quotes and Hebrew letters.
_WORDISH = r"\w֐-׿"

MAQAF = "־"  # ־
GERESH = "׳"  # ׳
GERSHAYIM = "״"  # ״
SOF_PASUQ = "׃"  # ׃
PASEQ = "׀"  # ׀
NUN_HAFUKHA = "׆"  # ׆

# Symbols that Hebrew voices either skip or read in English.
SYMBOLS: Dict[str, str] = {
    "%": " אחוז ",
    "&": " ו ",
    "@": " שטרודל ",
    "+": " פלוס ",
    "=": " שווה ",
    "±": " פלוס מינוס ",
    "×": " כפול ",
    "÷": " חלקי ",
    "°": " מעלות ",
    "§": " סעיף ",
    "©": " כל הזכויות שמורות ",
    "®": " ",
    "™": " ",
    "…": ". ",
    "€": " אירו ",
    "£": " ליש\"ט ",
    "$": " דולר ",
    "₪": " שקלים ",
}

# Currency signs that are written *before* the amount in the source text but
# have to be read *after* it in Hebrew ("$50" -> "50 דולר").
CURRENCY_PREFIXES: Dict[str, str] = {
    "$": "דולר",
    "€": "אירו",
    "£": 'ליש"ט',
    "₪": "שקלים",
}

_CURRENCY_RE = re.compile(
    "([" + "".join(re.escape(c) for c in CURRENCY_PREFIXES) + r"])\s*(\d[\d,.]*)"
)

# Abbreviations written with a geresh/gershayim. Keys use ASCII ' and "
# because normalize_geresh() runs first.
ABBREVIATIONS: Dict[str, str] = {
    "וכו'": "וכולי",
    "וכד'": "וכדומה",
    'וכיו"ב': "וכיוצא בזה",
    'ע"י': "על ידי",
    'עפ"י': "על פי",
    'עפי"ר': "על פי רוב",
    'בד"כ': "בדרך כלל",
    'כנ"ל': "כנזכר לעיל",
    'לפנה"ס': "לפני הספירה",
    'אחה"צ': "אחר הצהריים",
    'בעש"ק': "בערב שבת קודש",
    'בע"מ': "בערבון מוגבל",
    'ד"ר': "דוקטור",
    "פרופ'": "פרופסור",
    "עמ'": "עמוד",
    "מס'": "מספר",
    "רח'": "רחוב",
    "גב'": "גברת",
    "ת'ד": "תא דואר",
    'ת"ד': "תא דואר",
    'ת"א': "תל אביב",
    'ארה"ב': "ארצות הברית",
    'ח"כ': "חבר כנסת",
    'יו"ר': "יושב ראש",
    'ש"ח': "שקלים",
    'ק"מ': "קילומטר",
    'מ"ר': "מטר רבוע",
    'סמ"ר': "סנטימטר רבוע",
    'ק"ג': "קילוגרם",
    'שנ"ץ': "שניות",
}

# Acronyms that are pronounced as a single word. Dropping the gershayim is
# enough to make the voice say them instead of spelling them out.
ACRONYMS_AS_WORDS: Dict[str, str] = {
    'צה"ל': "צהל",
    'מד"א': "מדא",
    'שב"ס': "שבס",
    'של"ג': "שלג",
    'תנ"ך': "תנך",
    'רמטכ"ל': "רמטכל",
    'סמנכ"ל': "סמנכל",
    'מנכ"ל': "מנכל",
    'משמ"ר': "משמר",
}

_SENTENCE_END_RE = re.compile(r"(?<=[.!?׃])\s+|\n{2,}")
_WS_RE = re.compile(r"[ \t ]+")


def has_hebrew(text: str) -> bool:
    """True if the text contains at least one Hebrew letter."""
    return bool(_HEBREW_RE.search(text))


def strip_niqqud(text: str) -> str:
    """Remove niqqud (vowel points) and cantillation marks.

    Pointed text makes the neural voices hesitate or mispronounce, so this is
    on by default in :func:`prepare`.
    """
    # Decompose first so precomposed forms (e.g. U+FB2A) lose their points too.
    decomposed = unicodedata.normalize("NFD", text)
    return unicodedata.normalize("NFC", _NIQQUD_RE.sub("", decomposed))


def normalize_geresh(text: str) -> str:
    """Map the Hebrew geresh/gershayim onto ASCII ' and "."""
    return text.replace(GERESH, "'").replace(GERSHAYIM, '"')


def normalize_punctuation(text: str) -> str:
    """Normalize Hebrew and typographic punctuation to forms the voice reads."""
    replacements = {
        MAQAF: " ",
        SOF_PASUQ: ".",
        PASEQ: " ",
        NUN_HAFUKHA: " ",
        "–": " - ",  # en dash
        "—": " - ",  # em dash
        "“": '"',
        "”": '"',
        "‘": "'",
        "’": "'",
        "‎": "",  # LRM
        "‏": "",  # RLM
        "‪": "",  # LRE
        "‫": "",  # RLE
        "‬": "",  # PDF
        "⁦": "",  # LRI
        "⁧": "",  # RLI
        "⁨": "",  # FSI
        "⁩": "",  # PDI
        "­": "",  # soft hyphen
    }
    for src, dst in replacements.items():
        text = text.replace(src, dst)
    return text


def _replace_words(text: str, mapping: Dict[str, str]) -> str:
    """Replace whole-word keys, longest first so prefixes don't win."""
    for key in sorted(mapping, key=len, reverse=True):
        pattern = rf"(?<![{_WORDISH}]){re.escape(key)}(?![{_WORDISH}])"
        text = re.sub(pattern, mapping[key], text)
    return text


def expand_abbreviations(text: str, *, acronyms: bool = True) -> str:
    """Spell out common Hebrew abbreviations, e.g. ``ע"י`` -> ``על ידי``.

    Expects ASCII quotes; run :func:`normalize_geresh` first.
    """
    text = _replace_words(text, ABBREVIATIONS)
    if acronyms:
        text = _replace_words(text, ACRONYMS_AS_WORDS)
    return text


def expand_symbols(text: str) -> str:
    """Read symbols aloud in Hebrew, moving currency signs after the amount."""
    text = _CURRENCY_RE.sub(lambda m: f"{m.group(2)} {CURRENCY_PREFIXES[m.group(1)]}", text)
    for symbol, word in SYMBOLS.items():
        text = text.replace(symbol, word)
    return text


def prepare(
    text: str,
    *,
    keep_niqqud: bool = False,
    symbols: bool = True,
    abbreviations: bool = True,
    acronyms: bool = True,
) -> str:
    """Run the full cleanup pipeline over ``text``.

    Args:
        text: Raw input text.
        keep_niqqud: Keep vowel points instead of stripping them.
        symbols: Read ``%``, ``₪``, ``+`` and friends aloud in Hebrew.
        abbreviations: Spell out abbreviations such as ``עמ'`` -> ``עמוד``.
        acronyms: Drop the gershayim from acronyms pronounced as words.

    Returns:
        Text ready to hand to the synthesizer.
    """
    if not keep_niqqud:
        text = strip_niqqud(text)
    text = normalize_geresh(text)
    text = normalize_punctuation(text)
    if abbreviations:
        text = expand_abbreviations(text, acronyms=acronyms)
    if symbols:
        text = expand_symbols(text)
    text = _WS_RE.sub(" ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def billable_chars(raw: str) -> int:
    """Characters counted against a user's quota.

    Deliberately measured on the *raw* input, so the number in the UI counter
    is the same number that gets charged.
    """
    return len(raw.strip())


#: Rough speaking rate of the Hebrew neural voices, in characters per second.
CHARS_PER_SECOND = 14.5


def estimate_seconds(prepared: str, rate_percent: int = 0) -> float:
    """Estimate how long ``prepared`` will take to speak.

    Used for the progress affordance in the UI; calibrate against real output.
    """
    speed = max(0.25, 1.0 + rate_percent / 100.0)
    return round(len(prepared) / CHARS_PER_SECOND / speed, 1)


def split_paragraphs(text: str) -> List[str]:
    """Split on blank lines, dropping empties."""
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


def split_sentences(text: str) -> List[str]:
    """Split into sentences on ``. ! ? ׃`` and paragraph breaks."""
    return [s.strip() for s in _SENTENCE_END_RE.split(text) if s and s.strip()]


def _hard_split(piece: str, limit: int) -> Iterable[str]:
    """Break an over-long sentence at commas, then spaces, then anywhere."""
    while len(piece) > limit:
        window = piece[:limit]
        cut = max(window.rfind(","), window.rfind(";"), window.rfind(" "))
        if cut <= 0:
            cut = limit
        yield piece[:cut].strip()
        # Drop the delimiter so the next chunk doesn't open with ", ...".
        piece = piece[cut:].lstrip(",; \t\n")
    if piece:
        yield piece


def chunk_text(
    text: str, max_chars: int = 1200, *, by: str = "sentence", pack: Optional[bool] = None
) -> List[str]:
    """Group text into chunks of at most ``max_chars`` characters.

    Args:
        text: Prepared text.
        max_chars: Soft upper bound per chunk.
        by: ``"sentence"``, ``"paragraph"``, or ``"chars"``.
        pack: Combine consecutive units up to the limit. Defaults to true for
            sentences and characters, and false for paragraphs - a paragraph
            break is a boundary the author chose, so splitting by paragraph
            gives one chunk per paragraph rather than a repacked stream.

    Returns:
        Non-empty chunks, in order.
    """
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    if by == "paragraph":
        units = split_paragraphs(text)
    elif by == "sentence":
        units = split_sentences(text)
    elif by == "chars":
        units = list(_hard_split(text.strip(), max_chars))
    else:
        raise ValueError(f"unknown split mode: {by!r}")

    if pack is None:
        pack = by != "paragraph"
    if not pack:
        return [piece for unit in units for piece in _hard_split(unit, max_chars)]

    chunks: List[str] = []
    current = ""
    for unit in units:
        for piece in _hard_split(unit, max_chars):
            if not current:
                current = piece
            elif len(current) + 1 + len(piece) <= max_chars:
                current = f"{current} {piece}"
            else:
                chunks.append(current)
                current = piece
    if current:
        chunks.append(current)
    return chunks
