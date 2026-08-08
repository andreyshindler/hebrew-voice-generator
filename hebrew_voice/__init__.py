"""Hebrew text-to-speech built on edge-tts.

The core (:mod:`hebrew_voice.text`, :mod:`hebrew_voice.voices`,
:mod:`hebrew_voice.synth`) has no web dependencies, so the CLI works in an
environment where FastAPI isn't installed. The web app lives in
:mod:`hebrew_voice.web` and is imported lazily.
"""

__version__ = "0.1.0"

from .synth import Cue, Result, SynthesisOptions, synthesize, synthesize_sync, to_srt, to_vtt
from .text import prepare
from .voices import DEFAULT_VOICE, HEBREW_VOICES, resolve_voice

__all__ = [
    "__version__",
    "Cue",
    "Result",
    "SynthesisOptions",
    "synthesize",
    "synthesize_sync",
    "to_srt",
    "to_vtt",
    "prepare",
    "DEFAULT_VOICE",
    "HEBREW_VOICES",
    "resolve_voice",
]
