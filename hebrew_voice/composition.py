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

from typing import List, Sequence

from jinja2 import Template

from .synth import Cue

__all__ = ["build_composition", "COMPOSITION_FILENAME_SUFFIX"]

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
  .cue {
    position: absolute;
    bottom: {{ bottom }}px;
    max-width: {{ (width * 0.82) | round | int }}px;
    padding: 0 24px;
    font-family: "Noto Sans Hebrew", "Heebo", "David CLM", sans-serif;
    font-weight: 700;
    font-size: {{ font_size }}px;
    line-height: 1.28;
    text-align: center;
    color: #ffffff;
    direction: rtl;
    /* Legible over arbitrary footage without a backing box, which would
       defeat the point of a transparent overlay. */
    text-shadow:
      0 2px 6px rgba(0, 0, 0, .65),
      0 0 2px rgba(0, 0, 0, .95);
    -webkit-text-stroke: {{ stroke }}px rgba(0, 0, 0, .55);
    paint-order: stroke fill;
  }
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
{%- for cue in cues %}
  <div class="cue" data-start="{{ '%.3f' | format(cue.start) }}" \
data-duration="{{ '%.3f' | format(cue.duration) }}">{{ cue.text }}</div>
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


def build_composition(
    cues: Sequence[Cue],
    *,
    duration: float,
    width: int,
    height: int,
    audio_src: str = "",
    transparent: bool = False,
) -> str:
    """Render the composition HTML for one video.

    ``audio_src`` is resolved by the browser relative to the composition file,
    so the caller writes both into the same directory and passes a bare
    filename - no absolute paths, which would differ between the app container
    and the renderer container.

    Cues are clamped to ``duration``: a word boundary landing a few
    milliseconds past the end of the audio would otherwise extend the video.
    """
    clamped: List[Cue] = []
    for cue in cues:
        if cue.start >= duration:
            continue
        end = min(cue.end, duration)
        if end <= cue.start:
            continue
        clamped.append(Cue(cue.start, end, cue.text))

    return _TEMPLATE.render(
        cues=clamped,
        duration=max(duration, 0.1),
        width=width,
        height=height,
        audio_src=audio_src,
        transparent=transparent,
        background="#0b0f19",
        **_metrics(width, height),
    )
