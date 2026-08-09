"""Email confirmation on signup.

No SMTP anywhere: the app under test uses a RecordingMailer, and an autouse
fixture makes a real connection raise.
"""

import time
from dataclasses import replace
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from hebrew_voice import repo, security
from hebrew_voice.config import Settings
from hebrew_voice.db import connect
from hebrew_voice.mailer import (
    ConsoleMailer,
    SmtpMailer,
    build_verification_email,
    mailer_from_settings,
)
from hebrew_voice.web.app import create_app

from . import fakes
from .conftest import EMAIL, INVITE, PASSWORD, csrf


def signup(client, **overrides):
    body = {"email": EMAIL, "password": PASSWORD, "invite_code": INVITE}
    body.update(overrides)
    return client.post("/api/auth/signup", json=body)


def follow(client, link: str):
    """Open a link from an email the way a browser would."""
    parts = urlsplit(link)
    return client.get(f"{parts.path}?{parts.query}", follow_redirects=False)


class TestSignupSendsMail:
    def test_one_message_goes_to_the_registered_address(self, client, mailer):
        assert signup(client).status_code == 202
        assert len(mailer.sent) == 1
        assert mailer.last["To"] == EMAIL
        assert "אימות" in mailer.last["Subject"]

    def test_no_session_is_created(self, client, mailer):
        signup(client)
        # The account exists but is inert - that's the whole policy.
        assert not client.cookies.get("hv_session")
        assert client.get("/api/auth/me").status_code == 401

    def test_the_message_carries_a_usable_link(self, client, mailer, settings):
        signup(client)
        link = mailer.link()
        assert link.startswith(settings.base_url)
        assert "/verify?token=" in link

    def test_both_a_text_and_an_html_part_are_sent(self, client, mailer):
        signup(client)
        types = {part.get_content_type() for part in mailer.last.walk()}
        # A message with no plain-text alternative scores badly with filters.
        assert "text/plain" in types and "text/html" in types

    def test_the_account_starts_unverified(self, client, settings):
        signup(client)
        assert repo.get_user_by_email(settings.db_path, EMAIL).is_verified is False


class TestVerifying:
    def test_the_link_activates_the_account(self, client, mailer, settings):
        signup(client)
        response = follow(client, mailer.link())
        assert response.status_code == 200
        assert "אומתה" in response.text
        assert repo.get_user_by_email(settings.db_path, EMAIL).is_verified is True

    def test_login_is_refused_until_then(self, client, mailer):
        signup(client)
        response = client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD})
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "email_unverified"

    def test_login_works_afterwards(self, client, mailer):
        signup(client)
        follow(client, mailer.link())
        response = client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD})
        assert response.status_code == 200
        assert response.json()["user"]["email_verified"] is True

    def test_a_token_only_works_once(self, client, mailer):
        signup(client)
        link = mailer.link()
        assert follow(client, link).status_code == 200
        assert follow(client, link).status_code == 400

    def test_an_expired_token_is_refused(self, client, mailer, settings):
        signup(client)
        with connect(settings.db_path) as conn:
            conn.execute("UPDATE email_tokens SET expires_at = ?", (int(time.time()) - 1,))
        assert follow(client, mailer.link()).status_code == 400
        assert repo.get_user_by_email(settings.db_path, EMAIL).is_verified is False

    @pytest.mark.parametrize("token", ["", "garbage", "a" * 43])
    def test_a_bad_token_is_refused(self, client, token):
        response = client.get(f"/verify?token={token}")
        assert response.status_code == 400
        assert "הקישור אינו תקף" in response.text

    def test_the_page_is_reachable_while_signed_out(self, client, mailer):
        """The link is often opened in a different browser from the signup."""
        signup(client)
        link = mailer.link()
        with TestClient(client.app) as fresh:
            assert follow(fresh, link).status_code == 200


class TestResend:
    def test_it_sends_a_new_link_that_works(self, client, mailer):
        signup(client)
        first_token = mailer.token()
        response = client.post("/api/auth/resend-verification", json={"email": EMAIL})
        assert response.status_code == 204
        assert len(mailer.sent) == 2

        # The new link verifies...
        assert follow(client, mailer.link()).status_code == 200
        # ...and the superseded one is dead.
        assert follow(client, f"http://x/verify?token={first_token}").status_code == 400

    def test_an_unknown_address_looks_identical(self, client, mailer):
        response = client.post(
            "/api/auth/resend-verification", json={"email": "nobody@example.com"}
        )
        # 204 either way, so this can't enumerate registered addresses.
        assert response.status_code == 204
        assert mailer.sent == []

    def test_an_already_verified_account_gets_nothing(self, client, mailer):
        signup(client)
        follow(client, mailer.link())
        sent_before = len(mailer.sent)
        assert client.post(
            "/api/auth/resend-verification", json={"email": EMAIL}
        ).status_code == 204
        assert len(mailer.sent) == sent_before

    def test_it_is_rate_limited(self, client, mailer):
        signup(client)
        codes = [
            client.post("/api/auth/resend-verification", json={"email": EMAIL}).status_code
            for _ in range(6)
        ]
        assert 429 in codes, codes


class TestMailFailure:
    @pytest.fixture
    def broken(self, app):
        app.state.mailer = fakes.FailingMailer()
        with TestClient(app) as client:
            yield client

    def test_signup_reports_the_failure(self, broken):
        response = signup(broken)
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "email_send_failed"

    def test_the_account_survives_so_a_resend_can_recover_it(self, broken, settings, app):
        signup(broken)
        assert repo.get_user_by_email(settings.db_path, EMAIL) is not None

        # Mail comes back; the resend path completes the signup.
        app.state.mailer = fakes.RecordingMailer()
        assert broken.post(
            "/api/auth/resend-verification", json={"email": EMAIL}
        ).status_code == 204
        assert follow(broken, app.state.mailer.link()).status_code == 200

    def test_resend_stays_quiet_about_it(self, broken, settings):
        """A broken relay must not turn resend into an address oracle."""
        signup(broken)
        assert broken.post(
            "/api/auth/resend-verification", json={"email": EMAIL}
        ).status_code == 204


class TestEnforcement:
    @pytest.fixture
    def unverified_client(self, client, settings):
        """A signed-in session belonging to an unconfirmed account.

        The strict policy means the web flow can't produce this, but the CLI
        and future flows can - which is why require_verified exists.
        """
        signup(client)
        user = repo.get_user_by_email(settings.db_path, EMAIL)
        token = security.new_session_token()
        repo.create_session(
            settings.db_path,
            token_hash=security.hash_session_token(token),
            user_id=user.id,
            csrf_token="test-csrf",
            ttl_seconds=3600,
        )
        client.cookies.set("hv_session", token)
        client.cookies.set("hv_csrf", "test-csrf")
        return client

    @pytest.mark.parametrize(
        "method,path",
        [
            ("GET", "/api/voices"),
            ("POST", "/api/preview"),
            ("POST", "/api/synthesize"),
            ("GET", "/api/generations"),
        ],
    )
    def test_protected_routes_refuse_an_unverified_session(
        self, unverified_client, method, path
    ):
        response = unverified_client.request(
            method, path, json={"text": "שלום"}, headers=csrf(unverified_client)
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "email_unverified"

    def test_they_open_up_once_verified(self, unverified_client, settings, fake_tts):
        user = repo.get_user_by_email(settings.db_path, EMAIL)
        repo.mark_email_verified(settings.db_path, user.id)
        assert unverified_client.get("/api/voices").status_code == 200


class TestVerificationDisabled:
    """With HV_REQUIRE_EMAIL_VERIFICATION=false the old flow is intact."""

    @pytest.fixture
    def open_client(self, settings):
        relaxed = replace(settings, require_email_verification=False)
        with TestClient(create_app(relaxed)) as client:
            yield client

    def test_signup_logs_you_straight_in(self, open_client):
        response = signup(open_client)
        assert response.status_code == 201
        assert open_client.cookies.get("hv_session")
        assert open_client.get("/api/auth/me").status_code == 200

    def test_no_mail_is_sent(self, open_client):
        signup(open_client)
        # ConsoleMailer, and nothing asked it to send anything.
        assert isinstance(open_client.app.state.mailer, ConsoleMailer)


class TestGrandfathering:
    def test_accounts_that_predate_the_migration_are_verified(self, tmp_path):
        """Applying migration 2 must not lock out the existing admin."""
        from hebrew_voice import db

        path = tmp_path / "old.db"
        # Build the schema as it stood at version 1, then insert a user.
        version_one = db.MIGRATIONS[0][1]
        with connect(path) as conn:
            for statement in db._split_statements(version_one):
                conn.execute(statement)
            conn.execute(
                """
                INSERT INTO users (email, password_hash, created_at, is_active, is_admin)
                VALUES ('old@example.com', 'x', 1, 1, 1)
                """
            )
            conn.execute(
                "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at INTEGER)"
            )
            conn.execute("INSERT INTO schema_migrations VALUES (1, 1)")

        # Migrating brings the file all the way up, whatever the latest is.
        assert db.migrate(path) == db.MIGRATIONS[-1][0]
        assert repo.get_user_by_email(path, "old@example.com").is_verified is True


class TestMailerSelection:
    def test_no_host_means_the_console_mailer(self, settings):
        assert isinstance(mailer_from_settings(settings), ConsoleMailer)

    def test_a_host_means_smtp(self, settings):
        configured = replace(
            settings, smtp_host="smtp.gmail.com", smtp_from="me@gmail.com", smtp_user="me@gmail.com"
        )
        mailer = mailer_from_settings(configured)
        assert isinstance(mailer, SmtpMailer)
        assert (mailer.host, mailer.port, mailer.use_starttls) == ("smtp.gmail.com", 587, True)

    def test_the_message_names_the_sender(self):
        message = build_verification_email(
            to="a@b.co",
            link="https://host/voice-gen/verify?token=abc",
            expires_hours=24,
            from_address="me@gmail.com",
            from_name="מחולל קול עברי",
        )
        assert "me@gmail.com" in message["From"]
        assert message["To"] == "a@b.co"
        assert "https://host/voice-gen/verify?token=abc" in message.get_body(
            preferencelist=("plain",)
        ).get_content()


class TestProductionGuards:
    BASE = {
        "HV_ENV": "production",
        "HV_SECRET_KEY": "s3cret",
        "HV_INVITE_CODES": "abc",
        "HV_BASE_URL": "https://voice.example.com",
    }

    def test_refuses_to_boot_without_a_relay(self):
        with pytest.raises(ValueError, match="HV_SMTP_HOST"):
            Settings.from_env(dict(self.BASE))

    def test_boots_with_one(self):
        settings = Settings.from_env(
            {**self.BASE, "HV_SMTP_HOST": "smtp.gmail.com", "HV_SMTP_USER": "me@gmail.com"}
        )
        # HV_SMTP_FROM defaults to the login, which is what Gmail sends as.
        assert settings.smtp_from == "me@gmail.com"
        assert settings.email_enabled is True

    def test_boots_without_one_when_verification_is_off(self):
        settings = Settings.from_env(
            {**self.BASE, "HV_REQUIRE_EMAIL_VERIFICATION": "false"}
        )
        assert settings.require_email_verification is False

    def test_a_relay_is_not_required_outside_production(self):
        settings = Settings.from_env({"HV_BASE_URL": "http://localhost:8080"})
        assert settings.require_email_verification is True
        assert settings.email_enabled is False


class TestSubpathLinks:
    def test_the_link_carries_the_prefix(self, settings):
        under = replace(
            settings, base_url="https://host/voice-gen", root_path="/voice-gen"
        )
        app = create_app(under)
        app.state.mailer = fakes.RecordingMailer()
        with TestClient(app, base_url="https://host") as client:
            response = client.post(
                "/voice-gen/api/auth/signup",
                json={"email": EMAIL, "password": PASSWORD, "invite_code": INVITE},
            )
            assert response.status_code == 202
            link = app.state.mailer.link()
            assert link.startswith("https://host/voice-gen/verify?token=")
            # And it resolves.
            assert follow(client, link).status_code == 200
