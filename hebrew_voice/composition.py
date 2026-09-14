"""The HTML composition a render is produced from.

HyperFrames renders HTML in headless Chromium and encodes the frames with
FFmpeg, so the captions are laid out by a browser: right-to-left, niqqud and
Hebrew shaping all come out correct without us doing anything. That is the
whole reason this path exists - the editors people take the audio into get
Hebrew auto-captions wrong, and this sidesteps them.

The template is a module constant rather than a file so the wheel has no data
files to package and the Docker build cannot miss one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence

from jinja2 import Template

from .editing import DEFAULT_PLAN
from .synth import Cue

__all__ = [
    "build_composition",
    "plan_shots",
    "Shot",
    "COMPOSITION_FILENAME_SUFFIX",
]

COMPOSITION_FILENAME_SUFFIX = ".render.html"

#: Fonts are asked for by family name and must exist in the renderer image -
#: the sidecar installs Noto Sans Hebrew. A missing Hebrew font does not error,
#: it silently draws empty boxes, which is why readiness waits on
#: ``document.fonts.ready`` below.
_TEMPLATE = Template(
    """<!doctype html>
<html lang="he" dir="rtl">
<meta charset="utf-8">
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  html, body {
    width: {{ width }}px;
    height: {{ height }}px;
    overflow: hidden;
    /* Transparent for the overlay format: everything the encoder sees as
       alpha here becomes real alpha in the VP9 stream. */
    background: {{ "transparent" if transparent else background }};
  }
  #stage {
    position: relative;
    width: {{ width }}px;
    height: {{ height }}px;
    display: flex;
    align-items: {{ "center" if transparent else "flex-end" }};
    justify-content: center;
  }
  /* Uploaded photos and clips, behind the captions. `cover` is what makes a
     landscape photo usable in a 9:16 frame: it fills the frame and crops,
     rather than letterboxing. */
  .shot {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    object-fit: cover;
  }
{%- if motion.zoom %}
  /* A slow push, so a photo is not a slide in a deck. Driven by the document
     timeline, which the renderer seeks - the same mechanism the cues use. */
  img.shot { animation: shot-zoom var(--shot-hold, 3s) linear both; }
  @keyframes shot-zoom {
    from { transform: scale(1); }
    to   { transform: scale(1.09); }
  }
{%- endif %}
{%- if motion.fade %}
  /* Fade in rather than crossfade: shots are shown and hidden by the renderer,
     so only one is ever on screen to fade between. */
  .shot { animation-name: shot-in; animation-duration: .45s; animation-fill-mode: both; }
{%- if motion.zoom %}
  img.shot { animation: shot-in .45s both, shot-zoom var(--shot-hold, 3s) linear both; }
{%- endif %}
  @keyframes shot-in { from { opacity: 0; } to { opacity: 1; } }
{%- endif %}
  .cue {
    position: absolute;
{%- if caption.position == "middle" %}
    top: 50%;
    transform: translateY(-50%);
{%- elif caption.position == "top" %}
    top: {{ bottom }}px;
{%- else %}
    bottom: {{ bottom }}px;
{%- endif %}
    max-width: {{ (width * 0.82) | round | int }}px;
    padding: 0 24px;
    font-family: "Noto Sans Hebrew", "Heebo", "David CLM", sans-serif;
    font-weight: 700;
    font-size: {{ font_size }}px;
    line-height: 1.28;
    text-align: center;
    color: {{ caption.color }};
    direction: rtl;
    /* Legible over arbitrary footage without a backing box, which would
       defeat the point of a transparent overlay. */
    text-shadow:
      0 2px 6px rgba(0, 0, 0, .65),
      0 0 2px rgba(0, 0, 0, .95);
    -webkit-text-stroke: {{ stroke }}px rgba(0, 0, 0, .55);
    paint-order: stroke fill;
{%- if caption.box %}
    /* A backing box for footage the outline alone cannot survive - bright,
       busy, or the same colour as the text. */
    padding: 10px 22px;
    border-radius: 12px;
    background: rgba(0, 0, 0, .55);
    -webkit-text-stroke: 0;
{%- endif %}
  }
{%- if caption.karaoke %}
  /* The word being spoken. Each cue is emitted once per word with a different
     one lit, because the renderer shows and hides whole elements rather than
     restyling them mid-shot. */
  .cue .on { color: {{ karaoke_color }}; }
{%- endif %}
</style>

<div id="stage"
     data-composition-id="captions"
     data-start="0"
     data-duration="{{ '%.3f' | format(duration) }}"
     data-width="{{ width }}"
     data-height="{{ height }}">
{%- if audio_src %}
  <audio data-start="0" data-duration="{{ '%.3f' | format(duration) }}" src="{{ audio_src }}"></audio>
{%- endif %}
{%- if music_src %}
  {# Under the narration, not mixed with it: a fixed low level rather than
     real ducking, which would need an automation pass we do not have. #}
  <audio data-start="0" data-duration="{{ '%.3f' | format(duration) }}" \
data-volume="{{ music_volume }}" src="{{ music_src }}"></audio>
{%- endif %}
{%- for shot in shots %}
{%- if shot.kind == "video" %}
  {# muted on purpose: the clip's own sound would fight the narration, and
     mixing two tracks is a decision the user has not been asked to make. #}
  <video class="shot" muted data-start="{{ '%.3f' | format(shot.start) }}" \
data-duration="{{ '%.3f' | format(shot.duration) }}" src="{{ shot.src }}"></video>
{%- else %}
  <img class="shot" style="--shot-hold: {{ '%.3f' | format(shot.duration) }}s"
       data-start="{{ '%.3f' | format(shot.start) }}" \
data-duration="{{ '%.3f' | format(shot.duration) }}" src="{{ shot.src }}" alt="">
{%- endif %}
{%- endfor %}
{%- for cue in cues %}
{%- if cue.parts %}
  <div class="cue" data-start="{{ '%.3f' | format(cue.start) }}" \
data-duration="{{ '%.3f' | format(cue.duration) }}">{% for part in cue.parts %}\
<span{% if part.active %} class="on"{% endif %}>{{ part.text }}</span>{% endfor %}</div>
{%- else %}
  <div class="cue" data-start="{{ '%.3f' | format(cue.start) }}" \
data-duration="{{ '%.3f' | format(cue.duration) }}">{{ cue.text }}</div>
{%- endif %}
{%- endfor %}
</div>

<script>
  /* HyperFrames waits for this before it starts seeking frames. Fonts are the
     thing worth waiting for: capture beginning first would bake a fallback
     face, or blank boxes, into the opening frames. */
  window.__renderReady = false;
  document.fonts.ready.then(function () { window.__renderReady = true; });
</script>
</html>
""",
    autoescape=True,
)


def _metrics(width: int, height: int) -> dict:
    """Caption size and placement for this frame shape.

    Scaled to the *short* edge rather than the width: a 1080-wide vertical
    frame and a 1280-wide landscape one need captions of similar apparent
    size, and scaling by width alone makes the vertical one smaller, which is
    backwards - vertical video is watched on a phone and wants larger text.
    """
    short = min(width, height)
    portrait = height > width
    return {
        "font_size": max(24, round(short * 0.062)),
        # Portrait video is watched in apps that put controls, captions and
        # handles over the bottom sixth of the screen. Landscape has no such
        # furniture, so the captions can sit lower.
        "bottom": round(height * (0.18 if portrait else 0.11)),
        "stroke": max(1, round(short * 0.0022)),
    }


@dataclass(frozen=True)
class Shot:
    """One uploaded photo or clip, and the slot it occupies."""

    kind: str
    src: str
    start: float
    duration: float


@dataclass(frozen=True)
class _Part:
    """One run of caption text, lit or not."""

    text: str
    active: bool


@dataclass(frozen=True)
class _CueView:
    """A cue as the template needs it: plain text, or split for karaoke."""

    start: float
    duration: float
    text: str = ""
    parts: Sequence[_Part] = ()


def _karaoke_views(cues: Sequence[Cue], words: Sequence[Cue]) -> List[_CueView]:
    """Split each cue into one variant per word, with that word lit.

    The renderer shows and hides whole elements rather than restyling one
    mid-shot, so highlighting a moving word means emitting the cue once per
    word and letting the timeline swap between them. A cue of five words
    becomes five divs, which is nothing next to the frames being encoded.
    """
    views: List[_CueView] = []
    for cue in cues:
        inside = [
            word
            for word in words
            if word.start < cue.end and word.end > cue.start and word.text.strip()
        ]
        if len(inside) < 2:
            views.append(_CueView(start=cue.start, duration=cue.duration, text=cue.text))
            continue
        labels = [word.text.strip() for word in inside]
        for index, word in enumerate(inside):
            start = max(cue.start, word.start)
            end = min(cue.end, inside[index + 1].start if index + 1 < len(inside) else cue.end)
            if end <= start:
                continue
            parts: List[_Part] = []
            for position, label in enumerate(labels):
                if position:
                    parts.append(_Part(text=" ", active=False))
                parts.append(_Part(text=label, active=position == index))
            views.append(_CueView(start=start, duration=end - start, parts=parts))
    return views


def plan_shots(media: Sequence[tuple], duration: float) -> List[Shot]:
    """Give each upload an equal share of the voiceover, in order.

    ``media`` is (kind, src) pairs. The last slot absorbs the rounding so the
    shots always reach exactly the end of the audio - a hundredth of a second
    of black at the tail is small but visible, and free to avoid.

    A clip shorter than its slot leaves the last frame on screen rather than a
    gap: we cannot measure clip lengths in this image, so the layout cannot
    depend on knowing them.
    """
    if not media or duration <= 0:
        return []
    slot = duration / len(media)
    shots: List[Shot] = []
    for index, (kind, src) in enumerate(media):
        start = index * slot
        end = duration if index == len(media) - 1 else start + slot
        shots.append(Shot(kind=kind, src=src, start=start, duration=end - start))
    return shots


def build_composition(
    cues: Sequence[Cue],
    *,
    duration: float,
    width: int,
    height: int,
    audio_src: str = "",
    transparent: bool = False,
    shots: Sequence[Shot] = (),
    plan: Optional[dict] = None,
    word_cues: Sequence[Cue] = (),
    music_src: str = "",
) -> str:
    """Render the composition HTML for one video.

    ``audio_src`` is resolved by the browser relative to the composition file,
    so the caller writes both into the same directory and passes a bare
    filename - no absolute paths, which would differ between the app container
    and the renderer container.

    Cues are clamped to ``duration``: a word boundary landing a few
    milliseconds past the end of the audio would otherwise extend the video.
    """
    settings = {**DEFAULT_PLAN, **(plan or {})}
    caption = {**DEFAULT_PLAN["caption"], **(settings.get("caption") or {})}
    motion = {**DEFAULT_PLAN["motion"], **(settings.get("motion") or {})}

    clamped: List[Cue] = []
    for cue in cues:
        if cue.start >= duration:
            continue
        end = min(cue.end, duration)
        if end <= cue.start:
            continue
        clamped.append(Cue(cue.start, end, cue.text))

    views: Sequence = (
        _karaoke_views(clamped, word_cues)
        if caption["karaoke"] and word_cues
        else [_CueView(start=c.start, duration=c.duration, text=c.text) for c in clamped]
    )

    metrics = _metrics(width, height)
    metrics["font_size"] = max(18, round(metrics["font_size"] * float(caption["scale"])))

    music = settings.get("music") or {}
    return _TEMPLATE.render(
        cues=views,
        shots=shots,
        duration=max(duration, 0.1),
        width=width,
        height=height,
        audio_src=audio_src,
        transparent=transparent,
        background="#0b0f19",
        caption=caption,
        motion=motion,
        # Lit words keep the chosen colour's contrast rather than inventing a
        # second palette: white text lights up amber, anything else lights up
        # white.
        karaoke_color="#ffd54a" if caption["color"] == "#ffffff" else "#ffffff",
        music_src=music_src,
        music_volume=music.get("volume", 0.15),
        **metrics,
    )
