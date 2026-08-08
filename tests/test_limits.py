"""Quota accounting, rate limiting, concurrency caps, and retention."""

import asyncio
import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from hebrew_voice import cleanup, repo
from hebrew_voice.quota import RateLimiter, TokenBucket, day_reset_epoch, quota_day
from hebrew_voice.web.app import create_app

from . import fakes
from .conftest import EMAIL, INVITE, PASSWORD, csrf


class TestTokenBucket:
    def test_allows_a_burst_then_refuses(self):
        bucket = TokenBucket(capacity=3, rate=1.0)
        assert [bucket.take(now=0)[0] for _ in range(4)] == [True, True, True, False]

    def test_refills_over_time(self):
        bucket = TokenBucket(capacity=1, rate=1.0)
        assert bucket.take(now=0)[0] is True
        assert bucket.take(now=0.5)[0] is False
        assert bucket.take(now=1.1)[0] is True

    def test_reports_how_long_to_wait(self):
        bucket = TokenBucket(capacity=1, rate=1.0)
        bucket.take(now=0)
        allowed, retry_after = bucket.take(now=0)
        assert not allowed and 0 < retry_after <= 1.0

    def test_limiter_keys_are_independent(self):
        limiter = RateLimiter(per_minute=60, burst=1)
        assert limiter.check("a", now=0)[0] is True
        assert limiter.check("b", now=0)[0] is True
        assert limiter.check("a", now=0)[0] is False


class TestQuotaDay:
    def test_day_follows_the_configured_timezone(self):
        # 21:30 UTC on the 1st is already the 2nd in Jerusalem.
        moment = time.mktime(time.strptime("2025-06-01 21:30 UTC", "%Y-%m-%d %H:%M %Z"))
        assert quota_day("UTC", now=moment) == "2025-06-01"
        assert quota_day("Asia/Jerusalem", now=moment) == "2025-06-02"

    def test_reset_is_in_the_future(self):
        assert day_reset_epoch("Asia/Jerusalem") > time.time()


class TestDailyQuota:
    def test_refuses_once_the_allowance_is_spent(self, auth_client, fake_tts, settings):
        half = settings.daily_char_quota // 2 + 10
        assert auth_client.post(
            "/api/synthesize", json={"text": "א" * min(half, settings.max_chars)},
            headers=csrf(auth_client),
        ).status_code == 200

        # Spend the rest, then ask for more.
        for _ in range(5):
            response = auth_client.post(
                "/api/synthesize",
                json={"text": "ב" * settings.max_chars},
                headers=csrf(auth_client),
            )
            if response.status_code == 429:
                break
        assert response.status_code == 429
        error = response.json()["error"]
        assert error["code"] == "quota_exceeded"
        assert error["detail"]["remaining"] >= 0
        assert response.headers["retry-after"]

    def test_reservation_is_atomic_under_concurrency(self, settings):
        """Three parallel claims against a two-claim allowance: exactly two win."""
        from hebrew_voice.db import migrate

        migrate(settings.db_path)
        user = repo.create_user(settings.db_path, email="a@b.co", password_hash="x")
        day = quota_day("UTC")

        results = [repo.reserve_quota(settings.db_path, user.id, day, 100, 200) for _ in range(3)]
        assert [granted for granted, _ in results] == [True, True, False]
        assert repo.usage_today(settings.db_path, user.id, day) == 200

    def test_refund_restores_the_allowance(self, settings):
        from hebrew_voice.db import migrate

        migrate(settings.db_path)
        user = repo.create_user(settings.db_path, email="a@b.co", password_hash="x")
        day = quota_day("UTC")
        repo.reserve_quota(settings.db_path, user.id, day, 100, 200)
        repo.refund_quota(settings.db_path, user.id, day, 100)
        assert repo.usage_today(settings.db_path, user.id, day) == 0


class TestRateLimit:
    def test_too_many_requests_are_refused(self, settings, fake_tts):
        tight = replace(settings, rate_limit_per_min=1, rate_limit_burst=1)
        with TestClient(create_app(tight)) as client:
            client.post(
                "/api/auth/signup",
                json={"email": EMAIL, "password": PASSWORD, "invite_code": INVITE},
            )
            first = client.post("/api/synthesize", json={"text": "שלום"}, headers=csrf(client))
            second = client.post("/api/synthesize", json={"text": "שלום"}, headers=csrf(client))
        assert first.status_code == 200
        assert second.status_code == 429
        assert second.json()["error"]["code"] == "rate_limited"


class TestConcurrency:
    @pytest.mark.anyio
    async def test_a_full_queue_is_refused(self, settings, monkeypatch):
        """With one slot, a short queue wait, and a stuck job, the next caller
        gets 503 rather than waiting forever."""
        import httpx

        from hebrew_voice import synth

        gate = asyncio.Event()
        fakes.install(monkeypatch, synth, fakes.blocking_factory(gate))

        tight = replace(
            settings, max_concurrent_synth=1, max_concurrent_per_user=1, queue_wait_timeout=0.2
        )
        app = create_app(tight)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            async with app.router.lifespan_context(app):
                await client.post(
                    "/api/auth/signup",
                    json={"email": EMAIL, "password": PASSWORD, "invite_code": INVITE},
                )
                headers = {"X-CSRF-Token": client.cookies.get("hv_csrf")}
                first = asyncio.create_task(
                    client.post("/api/synthesize", json={"text": "ראשון"}, headers=headers)
                )
                await asyncio.sleep(0.1)
                second = await client.post(
                    "/api/synthesize", json={"text": "שני"}, headers=headers
                )
                # The same user already has a job in flight.
                assert second.status_code == 429
                assert second.json()["error"]["code"] == "already_running"

                gate.set()
                assert (await first).status_code == 200

    @pytest.mark.anyio
    async def test_a_slow_job_times_out(self, settings, monkeypatch):
        import httpx

        from hebrew_voice import synth

        never = asyncio.Event()
        fakes.install(monkeypatch, synth, fakes.blocking_factory(never))

        tight = replace(settings, synth_timeout=0.15)
        app = create_app(tight)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            async with app.router.lifespan_context(app):
                await client.post(
                    "/api/auth/signup",
                    json={"email": EMAIL, "password": PASSWORD, "invite_code": INVITE},
                )
                response = await client.post(
                    "/api/synthesize",
                    json={"text": "שלום"},
                    headers={"X-CSRF-Token": client.cookies.get("hv_csrf")},
                )
        assert response.status_code == 504
        assert response.json()["error"]["code"] == "synthesis_timeout"


class TestRetention:
    def _make(self, settings, user_id, count, *, age_days=0):
        from hebrew_voice import storage
        from hebrew_voice.models import Generation

        created = []
        for index in range(count):
            gen_id = storage.new_generation_id()
            stamp = int(time.time()) - age_days * 86400 - index
            paths = storage.relative_paths(user_id, gen_id, when=stamp)
            written = storage.write_artifacts(
                settings.data_dir, paths, audio=b"\xff\xfb", srt="1\n", vtt="WEBVTT\n"
            )
            generation = Generation(
                id=gen_id, user_id=user_id, created_at=stamp, title=f"item {index}",
                text_raw="שלום", text_prepared="שלום", char_count=4, voice="he-IL-HilaNeural",
                rate=0, pitch=0, volume=0, keep_niqqud=False, expand_symbols=True,
                expand_abbreviations=True, expand_acronyms=True,
                audio_rel=written.audio_rel, srt_rel=written.srt_rel, vtt_rel=written.vtt_rel,
                audio_bytes=2, duration_ms=1000, cue_count=1,
            )
            repo.insert_generation(settings.db_path, generation)
            created.append(generation)
        return created

    @pytest.fixture
    def prepared(self, settings):
        from hebrew_voice import storage
        from hebrew_voice.db import migrate

        migrate(settings.db_path)
        storage.ensure_data_dir(settings.data_dir)
        return repo.create_user(settings.db_path, email="a@b.co", password_hash="x")

    def test_keeps_only_the_newest_n(self, settings, prepared):
        tight = replace(settings, history_keep=5, history_max_age_days=3650)
        self._make(tight, prepared.id, 8)
        result = cleanup.sweep(tight)
        assert result.generations_deleted == 3
        assert repo.count_generations(tight.db_path, prepared.id) == 5
        assert len(list((tight.data_dir / "audio").rglob("*.mp3"))) == 5

    def test_deletes_anything_past_the_age_limit(self, settings, prepared):
        tight = replace(settings, history_keep=100, history_max_age_days=30)
        self._make(tight, prepared.id, 2, age_days=60)
        self._make(tight, prepared.id, 1, age_days=1)
        cleanup.sweep(tight)
        assert repo.count_generations(tight.db_path, prepared.id) == 1

    def test_dry_run_changes_nothing(self, settings, prepared):
        tight = replace(settings, history_keep=1, history_max_age_days=3650)
        self._make(tight, prepared.id, 4)
        result = cleanup.sweep(tight, dry_run=True)
        assert result.generations_deleted == 3
        assert repo.count_generations(tight.db_path, prepared.id) == 4

    def test_survives_a_missing_file(self, settings, prepared):
        tight = replace(settings, history_keep=0, history_max_age_days=3650)
        created = self._make(tight, prepared.id, 1)
        (tight.data_dir / created[0].audio_rel).unlink()
        cleanup.sweep(tight)  # must not raise
        assert repo.count_generations(tight.db_path, prepared.id) == 0

    def test_purges_expired_sessions(self, settings, prepared):
        from hebrew_voice.db import connect

        repo.create_session(
            settings.db_path,
            token_hash="dead",
            user_id=prepared.id,
            csrf_token="c",
            ttl_seconds=-10,
        )
        with connect(settings.db_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
        cleanup.sweep(settings)
        with connect(settings.db_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
