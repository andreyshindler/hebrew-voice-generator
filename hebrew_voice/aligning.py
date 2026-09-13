"""Keeping word timings when the words change.

A transcription's timings belong to the words the recogniser *heard*. Correct
one and the timings no longer describe the text, so editing has to say what
happens to the clock.

The rule is alignment: match the corrected words against the recognised ones,
keep the timing of everything that survived, and share out the window of
everything that did not. That makes the common edit exact - swapping a
misheard name, a number, a loanword leaves every other word untouched, because
only the replaced word's own slice moves.

What it cannot do well is a rewrite. Replace a whole sentence and there is
nothing left to anchor to, so its window is divided up by word length and the
result drifts against the audio. That is a real limit, not a bug to file: the
timings for words nobody said do not exist, and no rule invents them.
"""

from __future__ import annotations

import difflib
from typing import List, Sequence

from .synth import Cue

__all__ = ["realign", "split_words"]

#: Floor for a word that has no time of its own to inherit. Short enough to fit
#: several into a pause, long enough not to round to zero.
_MIN_SLICE = 0.02


def split_words(text: str) -> List[str]:
    """The words of a transcript, as the aligner counts them."""
    return text.split()


def realign(old: Sequence[Cue], words: Sequence[str], *, duration: float = 0.0) -> List[Cue]:
    """Re-time ``words`` against the cues they were edited from.

    ``duration`` is the recording's length, used only to place words appended
    past the end of the last recognised one.
    """
    # Stripped, not just tested for truth: a whitespace-only token is not a
    # word, and one that survived would become a cue showing nothing.
    words = [stripped for stripped in (w.strip() for w in words) if stripped]
    if not words:
        return []
    if not old:
        # Nothing to align against: spread the words over the recording.
        return _share(list(words), 0.0, duration or float(len(words)) * 0.3)

    previous = [cue.text for cue in old]
    matcher = difflib.SequenceMatcher(a=previous, b=list(words), autojunk=False)
    out: List[Cue] = []

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            # Survived the edit, so it keeps the time it was actually said at.
            out.extend(Cue(old[i].start, old[i].end, words[j])
                       for i, j in zip(range(i1, i2), range(j1, j2)))
            continue
        if j1 == j2:
            # Words were deleted. Their time is simply gone - the neighbours
            # keep their own, which leaves a silent gap where the words were.
            # Stretching a neighbour over it would claim it said more than it
            # did.
            continue

        start, end = _window(old, i1, i2, duration)
        out.extend(_share(list(words[j1:j2]), start, end))

    return out


def _window(old: Sequence[Cue], i1: int, i2: int, duration: float) -> tuple:
    """The span of time the new words in this block may occupy."""
    if i1 < i2:
        # Replacing words: their own span is what is available.
        return old[i1].start, old[i2 - 1].end
    # A pure insertion has no span of its own, so it borrows the pause it was
    # written into. Between two recognised words that is the gap between them;
    # at either end it is whatever is left of the recording.
    before = old[i1 - 1].end if i1 > 0 else 0.0
    after = old[i1].start if i1 < len(old) else max(duration, old[-1].end)
    return before, max(before, after)


def _share(words: List[str], start: float, end: float) -> List[Cue]:
    """Divide ``start``..``end`` among ``words``, by how long each one is.

    Character count is a poor model of speech but a much better one than an
    equal split, which gives a one-letter word as long as a five-syllable one.
    """
    if not words:
        return []
    span = max(0.0, end - start)
    if span <= 0:
        # No room at all - the words were inserted where nobody paused. They
        # still need somewhere to go, so they get the floor and overlap what
        # follows rather than collapsing onto a single instant.
        return [
            Cue(start + i * _MIN_SLICE, start + (i + 1) * _MIN_SLICE, word)
            for i, word in enumerate(words)
        ]

    weights = [max(1, len(word)) for word in words]
    total = sum(weights)
    cues: List[Cue] = []
    at = start
    for word, weight in zip(words, weights):
        hold = max(_MIN_SLICE, span * weight / total)
        cues.append(Cue(round(at, 3), round(min(end, at + hold), 3), word))
        at += hold
    return cues
