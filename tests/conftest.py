"""Shared fixtures. Nothing here touches the network."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from hebrew_voice import repo, synth
from hebrew_voice.mailer import SmtpMailer
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

    def explode_smtp(*args, **kwargs):
        raise AssertionError("a test tried to open a real SMTP connection")

    monkeypatch.setattr(SmtpMailer, "send", explode_smtp)


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
    application = create_app(settings)
    # Every app in the suite records mail instead of sending it.
    application.state.mailer = fakes.RecordingMailer()
    return application


@pytest.fixture
def mailer(app):
    """The RecordingMailer the app under test is using."""
    return app.state.mailer


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def auth_client(client, settings):
    """A client with a registered, verified, logged-in account.

    Signup alone no longer logs you in - the account has to confirm its
    address first - so this walks the real flow: register, verify, log in.
    """
    response = client.post(
        "/api/auth/signup",
        json={"email": EMAIL, "password": PASSWORD, "invite_code": INVITE},
    )
    assert response.status_code in (201, 202), response.text
    user = repo.get_user_by_email(settings.db_path, EMAIL)
    repo.mark_email_verified(settings.db_path, user.id)
    login = client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert login.status_code == 200, login.text
    return client


def register_verified(client, settings, email: str = EMAIL, password: str = PASSWORD):
    """Walk the whole signup flow: register, confirm the address, log in.

    Signup on its own no longer creates a session, so tests that just need "a
    logged-in account" use this rather than asserting on the intermediate
    states, which belong to test_email_verification.py.
    """
    response = client.post(
        "/api/auth/signup",
        json={"email": email, "password": password, "invite_code": INVITE},
    )
    assert response.status_code in (201, 202), response.text
    user = repo.get_user_by_email(settings.db_path, email)
    repo.mark_email_verified(settings.db_path, user.id)
    login = client.post("/api/auth/login", json={"email": email, "password": password})
    assert login.status_code == 200, login.text
    return client


def csrf(client: TestClient) -> dict:
    """The header every unsafe API call needs."""
    return {"X-CSRF-Token": client.cookies.get("hv_csrf", "")}
