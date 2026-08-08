"""Shared fixtures. Nothing here touches the network."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from hebrew_voice import synth
from hebrew_voice.config import Settings
from hebrew_voice.web.app import create_app

from . import fakes

INVITE = "TEST-INVITE"
EMAIL = "user@example.com"
PASSWORD = "correct-horse-battery"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Make a real edge-tts call impossible.

    A test that forgets to install a fake fails loudly instead of hanging on a
    blocked connection.
    """

    def explode(*args, **kwargs):
        raise AssertionError("a test tried to make a live TTS call")

    monkeypatch.setattr(synth, "build_communicate", explode)


@pytest.fixture
def fake_tts(monkeypatch):
    """Install the default happy-path fake and hand back the module."""
    fakes.install(monkeypatch, synth)
    return fakes


@pytest.fixture
def settings(tmp_path) -> Settings:
    """Isolated data dir and database, with cheap password hashing."""
    return Settings(
        env="development",
        secret_key="test-secret",
        data_dir=tmp_path / "data",
        db_path=tmp_path / "data" / "test.db",
        base_url="http://testserver",
        secure_cookies=False,
        signup_enabled=True,
        invite_codes=[INVITE],
        max_chars=500,
        daily_char_quota=1000,
        quota_tz="UTC",
        rate_limit_per_min=600,
        rate_limit_burst=100,
        synth_timeout=10.0,
        queue_wait_timeout=2.0,
        cleanup_interval_min=10_000,  # effectively never during a test
        scrypt_n=1024,
        scrypt_r=8,
        scrypt_p=1,
    )


@pytest.fixture
def app(settings):
    return create_app(settings)


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def auth_client(client):
    """A client with a registered, logged-in account."""
    response = client.post(
        "/api/auth/signup",
        json={"email": EMAIL, "password": PASSWORD, "invite_code": INVITE},
    )
    assert response.status_code == 201, response.text
    return client


def csrf(client: TestClient) -> dict:
    """The header every unsafe API call needs."""
    return {"X-CSRF-Token": client.cookies.get("hv_csrf", "")}
