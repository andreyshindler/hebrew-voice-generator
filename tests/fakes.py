"""A stand-in for edge-tts, so the whole suite runs with no network."""

from __future__ import annotations

from typing import AsyncIterator, List, Optional

TICKS = 10_000_000  # 100ns units per second, same as the real service


class FakeCommunicate:
    """Yields plausible audio chunks and WordBoundary events.

    Records the keyword arguments it was constructed with, so tests can assert
    that the sliders really do reach the engine as ``+10%`` / ``-5Hz``.
    """

    #: Every instance ever built, in order. Tests inspect the last one.
    instances: List["FakeCommunicate"] = []

    def __init__(self, text: str, voice: str, **kwargs) -> None:
        self.text = text
        self.voice = voice
        self.kwargs = kwargs
        FakeCommunicate.instances.append(self)

    async def stream(self) -> AsyncIterator[dict]:
        words = self.text.split() or ["…"]
        offset = 0
        yield {"type": "audio", "data": b"\xff\xfb\x90\x00" + b"\x00" * 60}
        for word in words:
            duration = int(0.35 * TICKS)
            yield {
                "type": "WordBoundary",
                "offset": offset,
                "duration": duration,
                "text": word,
            }
            offset += duration
            yield {"type": "audio", "data": b"\x00" * 128}


def install(monkeypatch, module, factory=None) -> None:
    """Point ``synth.build_communicate`` at a fake."""
    FakeCommunicate.instances.clear()
    monkeypatch.setattr(module, "build_communicate", factory or default_factory)


def default_factory(text, opts):
    """Mirror the real call, so tests can assert what reached the engine."""
    return FakeCommunicate(
        text,
        opts.voice,
        rate=opts.rate,
        volume=opts.volume,
        pitch=opts.pitch,
        boundary=opts.boundary,
    )


def failing_factory(failures: int, exc: Optional[BaseException] = None):
    """Fails the first ``failures`` attempts, then behaves normally."""
    state = {"calls": 0}
    error = exc

    def factory(text, opts):
        state["calls"] += 1
        if state["calls"] <= failures:
            return _Failing(error or ConnectionError("boom"))
        return default_factory(text, opts)

    factory.state = state  # type: ignore[attr-defined]
    return factory


def silent_factory(text, opts):
    """Produces boundaries but no audio - the 'no audio received' edge case."""
    return _Silent(text, opts.voice)


def blocking_factory(event):
    """Waits on an asyncio.Event before producing anything, for gate tests."""

    def factory(text, opts):
        return _Blocking(text, opts.voice, event)

    return factory


class _Failing:
    def __init__(self, error: BaseException) -> None:
        self.error = error

    async def stream(self):
        raise self.error
        yield  # pragma: no cover - makes this an async generator


class _Silent(FakeCommunicate):
    async def stream(self):
        yield {"type": "WordBoundary", "offset": 0, "duration": TICKS, "text": "שלום"}


class _Blocking(FakeCommunicate):
    def __init__(self, text: str, voice: str, event) -> None:
        super().__init__(text, voice)
        self.event = event

    async def stream(self):
        await self.event.wait()
        async for chunk in FakeCommunicate.stream(self):
            yield chunk
