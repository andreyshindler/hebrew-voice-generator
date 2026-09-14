"""How a render is cut: shot timing, caption styling, motion, music.

All of it is presentation, and all of it ends up as CSS or as an attribute on
an element - which is the reason these options are cheap to offer at all. The
frame is a browser page, so styling and motion cost nothing extra to render.

Everything arrives from a request, so everything is clamped here rather than
trusted. The normalised plan is stored verbatim and is part of the dedupe key,
so two renders that differ only in caption colour are correctly two renders.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

__all__ = ["normalise_plan", "shot_durations", "CAPTION_POSITIONS", "DEFAULT_PLAN"]

CAPTION_POSITIONS = ("bottom", "middle", "top")

#: Caption size multiplier. Below this captions stop being readable on a phone;
#: above it a few words fill the frame.
_SCALE_RANGE = (0.6, 1.8)

#: A shot shorter than this is a flicker rather than a shot.
_MIN_SHOT = 0.3

#: Music sits under the narration. Loud enough to hear, quiet enough that the
#: voice stays the point.
_DEFAULT_MUSIC_VOLUME = 0.15

DEFAULT_PLAN: Dict[str, Any] = {
    "durations": [],
    "caption": {
        "scale": 1.0,
        "position": "bottom",
        "color": "#ffffff",
        "box": False,
        "karaoke": False,
    },
    "motion": {"zoom": True, "fade": True},
    "music": None,
}


def _clamp(value: Any, low: float, high: float, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    return max(low, min(high, number))


def _colour(value: Any, default: str) -> str:
    """Accept only ``#rgb``/``#rrggbb``.

    This string goes straight into a stylesheet, so anything else is a way to
    write arbitrary CSS into the page.
    """
    if not isinstance(value, str):
        return default
    text = value.strip().lower()
    if len(text) not in (4, 7) or not text.startswith("#"):
        return default
    if any(ch not in "0123456789abcdef" for ch in text[1:]):
        return default
    return text


def normalise_plan(raw: Any, *, shot_count: int, music_ok: bool = True) -> Dict[str, Any]:
    """Validate and clamp an edit plan from a request."""
    source = raw if isinstance(raw, dict) else {}

    caption_in = source.get("caption") if isinstance(source.get("caption"), dict) else {}
    caption = {
        "scale": round(_clamp(caption_in.get("scale"), *_SCALE_RANGE, 1.0), 3),
        "position": (
            caption_in.get("position")
            if caption_in.get("position") in CAPTION_POSITIONS
            else "bottom"
        ),
        "color": _colour(caption_in.get("color"), "#ffffff"),
        "box": bool(caption_in.get("box", False)),
        "karaoke": bool(caption_in.get("karaoke", False)),
    }

    motion_in = source.get("motion") if isinstance(source.get("motion"), dict) else {}
    motion = {
        "zoom": bool(motion_in.get("zoom", True)),
        "fade": bool(motion_in.get("fade", True)),
    }

    durations: List[float] = []
    raw_durations = source.get("durations")
    if isinstance(raw_durations, list) and shot_count and len(raw_durations) == shot_count:
        durations = [round(_clamp(value, _MIN_SHOT, 3600.0, _MIN_SHOT), 3) for value in raw_durations]

    music: Optional[Dict[str, Any]] = None
    music_in = source.get("music")
    if music_ok and isinstance(music_in, dict) and isinstance(music_in.get("id"), str):
        music = {
            "id": music_in["id"],
            "volume": round(_clamp(music_in.get("volume"), 0.0, 1.0, _DEFAULT_MUSIC_VOLUME), 3),
        }

    return {"durations": durations, "caption": caption, "motion": motion, "music": music}


def shot_durations(plan: Dict[str, Any], *, shot_count: int, total: float) -> List[float]:
    """Seconds each shot holds, scaled to fit the voiceover exactly.

    Hand-set durations are treated as *proportions* rather than absolutes: a
    shot list that adds up to more or less than the audio would otherwise leave
    black at the end or cut the last shot off, and neither is what someone
    dragging a slider meant. Their relative lengths are what they were
    adjusting, so those are preserved and the whole thing is scaled to fit.
    """
    if shot_count <= 0 or total <= 0:
        return []
    wanted = plan.get("durations") or []
    if len(wanted) != shot_count or not all(value > 0 for value in wanted):
        return [total / shot_count] * shot_count
    scale = total / sum(wanted)
    return [value * scale for value in wanted]
