"""Signup, login, sessions, and CSRF."""

import time

import pytest

from hebrew_voice import repo, security
from hebrew_voice.web.app import create_app

from .conftest import EMAIL, INVITE, PASSWORD, csrf


def signup(client, **overrides):
    body = {"email": EMAIL, "password": PASSWORD, "invite_code": INVITE}
    body.update(overrides)
    return client.post("/api/auth/signup", json=body)


class TestSignup:
    def test_creates_an_account_and_signs_in(self, client):
        response = signup(client)
        assert response.status_code == 201
        assert response.json()["user"]["email"] == EMAIL
        assert client.cookies.get("hv_session")
        assert client.cookies.get("hv_csrf")

    def test_first_account_is_an_admin(self, client):
        assert signup(client).json()["user"]["is_admin"] is True

    def test_rejects_a_wrong_invite_code(self, client):
        response = signup(client, invite_code="nope")
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "invalid_invite"

    def test_rejects_a_duplicate_email(self, client):
        signup(client)
        client.cookies.clear()
        response = signup(client)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "email_taken"

    def test_rejects_a_short_password(self, client):
        assert signup(client, password="short").status_code == 422

    def test_rejects_a_malformed_email(self, client):
        assert signup(client, email="not-an-email").status_code == 422

    def test_is_closed_when_disabled(self, settings):
        from dataclasses import replace

        from fastapi.testclient import TestClient

        with TestClient(create_app(replace(settings, signup_enabled=False))) as client:
            assert signup(client).status_code == 404
            assert client.get("/signup").status_code == 404


class TestLogin:
    def test_round_trip(self, client):
        signup(client)
        client.cookies.clear()
        response = client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD})
        assert response.status_code == 200
        assert client.get("/api/auth/me").json()["user"]["email"] == EMAIL

    def test_email_is_case_insensitive(self, client):
        signup(client)
        client.cookies.clear()
        response = client.post(
            "/api/auth/login", json={"email": EMAIL.upper(), "password": PASSWORD}
        )
        assert response.status_code == 200

    def test_wrong_password_is_rejected(self, client):
        signup(client)
        client.cookies.clear()
        response = client.post("/api/auth/login", json={"email": EMAIL, "password": "wrong-one!!"})
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "invalid_credentials"

    def test_unknown_email_looks_the_same_as_a_wrong_password(self, client):
        response = client.post(
            "/api/auth/login", json={"email": "nobody@example.com", "password": PASSWORD}
        )
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "invalid_credentials"

    def test_locks_out_after_repeated_failures(self, client, settings):
        signup(client)
        client.cookies.clear()
        for _ in range(settings.max_failed_logins - 1):
            client.post("/api/auth/login", json={"email": EMAIL, "password": "wrong-one!!"})
        final = client.post("/api/auth/login", json={"email": EMAIL, "password": "wrong-one!!"})
        assert final.status_code == 423
        assert repo.get_user_by_email(settings.db_path, EMAIL).locked_until > time.time()
        # The correct password is refused too - by the lock, or by the per-IP
        # rate limit that has also tripped by now. Either way, no way in.
        assert client.post(
            "/api/auth/login", json={"email": EMAIL, "password": PASSWORD}
        ).status_code in (423, 429)

    def test_a_disabled_account_cannot_sign_in(self, client, settings):
        signup(client)
        user = repo.get_user_by_email(settings.db_path, EMAIL)
        repo.set_active(settings.db_path, user.id, False)
        client.cookies.clear()
        response = client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD})
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "account_disabled"


class TestSessions:
    def test_logout_revokes_the_session_server_side(self, auth_client, settings):
        token = auth_client.cookies.get("hv_session")
        assert auth_client.post("/api/auth/logout", headers=csrf(auth_client)).status_code == 204
        assert (
            repo.get_session_with_user(settings.db_path, security.hash_session_token(token))
            is None
        )
        auth_client.cookies.set("hv_session", token)
        assert auth_client.get("/api/auth/me").status_code == 401

    def test_a_forged_cookie_is_rejected(self, client):
        client.cookies.set("hv_session", "not-a-real-token")
        assert client.get("/api/auth/me").status_code == 401

    def test_an_expired_session_is_rejected(self, auth_client, settings):
        token = auth_client.cookies.get("hv_session")
        from hebrew_voice.db import connect

        with connect(settings.db_path) as conn:
            conn.execute(
                "UPDATE sessions SET expires_at = ? WHERE id = ?",
                (int(time.time()) - 5, security.hash_session_token(token)),
            )
        assert auth_client.get("/api/auth/me").status_code == 401


class TestProtection:
    #: Everything under /api that must refuse an anonymous caller.
    PROTECTED = [
        ("GET", "/api/auth/me"),
        ("GET", "/api/voices"),
        ("POST", "/api/preview"),
        ("POST", "/api/synthesize"),
        ("GET", "/api/generations"),
        ("GET", "/api/generations/" + "a" * 32),
        ("GET", "/api/generations/" + "a" * 32 + "/audio.mp3"),
        ("DELETE", "/api/generations/" + "a" * 32),
    ]

    @pytest.mark.parametrize("method,path", PROTECTED)
    def test_anonymous_callers_are_refused(self, client, method, path):
        response = client.request(method, path, json={})
        assert response.status_code == 401, f"{method} {path} was reachable"

    def test_every_api_route_is_covered_by_this_list(self, app):
        allowlist = {"/api/auth/login", "/api/auth/signup", "/api/auth/logout"}
        documented = {path for _method, path in self.PROTECTED} | allowlist
        for route in _api_routes(app):
            template = route.replace("{gen_id}", "a" * 32)
            assert template in documented or route.startswith("/api/openapi"), (
                f"{route} is a new API route - add it to PROTECTED or the allowlist"
            )

    @pytest.mark.parametrize(
        "method,path",
        [("POST", "/api/synthesize"), ("DELETE", "/api/generations/" + "a" * 32)],
    )
    def test_csrf_is_required(self, auth_client, method, path):
        response = auth_client.request(method, path, json={"text": "שלום"})
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "csrf_failed"

    def test_a_csrf_token_from_another_session_is_rejected(self, auth_client):
        response = auth_client.post(
            "/api/synthesize", json={"text": "שלום"}, headers={"X-CSRF-Token": "someone-elses"}
        )
        assert response.status_code == 403

    def test_a_cross_origin_post_is_blocked(self, auth_client):
        response = auth_client.post(
            "/api/synthesize",
            json={"text": "שלום"},
            headers={**csrf(auth_client), "Origin": "https://evil.example"},
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "cross_origin_blocked"

    def test_sec_fetch_site_cross_site_is_blocked(self, auth_client):
        response = auth_client.post(
            "/api/synthesize",
            json={"text": "שלום"},
            headers={**csrf(auth_client), "Sec-Fetch-Site": "cross-site"},
        )
        assert response.status_code == 403


def _api_routes(app):
    """Every declared /api path, flattening this FastAPI's nested routers."""
    found = set()

    def walk(routes):
        for route in routes:
            path = getattr(route, "path", None)
            if path and getattr(route, "methods", None):
                found.add(path)
            for attr in ("routes", "router"):
                nested = getattr(route, attr, None)
                if nested is not None:
                    walk(getattr(nested, "routes", nested))

    walk(app.routes)
    return {p for p in found if p.startswith("/api") and not p.startswith("/api/docs")}


class TestPages:
    def test_anonymous_root_redirects_to_login(self, client):
        response = client.get("/", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"

    def test_logged_in_root_renders_the_app(self, auth_client):
        response = auth_client.get("/")
        assert response.status_code == 200
        assert 'dir="rtl"' in response.text
        assert "bootstrap" in response.text

    def test_login_page_redirects_when_already_signed_in(self, auth_client):
        assert auth_client.get("/login", follow_redirects=False).status_code == 303

    def test_html_carries_security_headers(self, client):
        response = client.get("/login")
        assert "default-src 'self'" in response.headers["content-security-policy"]
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"

    def test_healthz_needs_no_auth(self, client):
        assert client.get("/healthz").json()["status"] == "ok"
