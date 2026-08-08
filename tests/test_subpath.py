"""Serving the app under a URL prefix, e.g. https://host/voice-gen/.

The VPS runs another app at the root, so every URL the app emits - links,
redirects, JSON, cookies - has to carry the prefix.
"""

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from hebrew_voice.config import Settings, normalize_root_path
from hebrew_voice.web.app import create_app

from .conftest import EMAIL, INVITE, PASSWORD, csrf, register_verified

PREFIX = "/voice-gen"
HOST = "https://srv1515969.hstgr.cloud"


@pytest.fixture
def sub_settings(settings) -> Settings:
    """The real deployment shape: a prefix derived from a full public URL."""
    return replace(settings, base_url=f"{HOST}{PREFIX}", root_path=PREFIX)


@pytest.fixture
def sub_client(sub_settings):
    with TestClient(create_app(sub_settings), base_url="https://srv1515969.hstgr.cloud") as c:
        yield c


@pytest.fixture
def sub_auth(sub_client, sub_settings):
    """Registered, confirmed, and signed in - all under the prefix."""
    from hebrew_voice import repo

    response = sub_client.post(
        f"{PREFIX}/api/auth/signup",
        json={"email": EMAIL, "password": PASSWORD, "invite_code": INVITE},
    )
    assert response.status_code == 202, response.text
    user = repo.get_user_by_email(sub_settings.db_path, EMAIL)
    repo.mark_email_verified(sub_settings.db_path, user.id)
    login = sub_client.post(
        f"{PREFIX}/api/auth/login", json={"email": EMAIL, "password": PASSWORD}
    )
    assert login.status_code == 200, login.text
    return sub_client


class TestRootPathParsing:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("/voice-gen", "/voice-gen"),
            ("voice-gen", "/voice-gen"),
            ("/voice-gen/", "/voice-gen"),
            ("/voice-gen///", "/voice-gen"),
            ("  /voice-gen  ", "/voice-gen"),
            ("/", ""),
            ("", ""),
            (None, ""),
        ],
    )
    def test_normalizes_the_sloppy_forms(self, raw, expected):
        assert normalize_root_path(raw) == expected

    def test_prefix_is_derived_from_the_base_url(self):
        settings = Settings.from_env({"HV_BASE_URL": f"{HOST}{PREFIX}"})
        assert settings.root_path == PREFIX
        assert settings.origin == HOST

    def test_an_explicit_root_path_wins(self):
        settings = Settings.from_env(
            {"HV_BASE_URL": f"{HOST}{PREFIX}", "HV_ROOT_PATH": "/other"}
        )
        assert settings.root_path == "/other"

    def test_a_root_deployment_has_no_prefix(self):
        settings = Settings.from_env({"HV_BASE_URL": HOST})
        assert settings.root_path == ""
        assert settings.cookie_path == "/"
        assert settings.url("/login") == "/login"

    def test_origin_drops_the_path(self, sub_settings):
        assert sub_settings.origin == HOST
        assert sub_settings.url("/login") == "/voice-gen/login"
        assert sub_settings.cookie_path == "/voice-gen/"

    def test_a_malformed_base_url_is_refused(self):
        with pytest.raises(ValueError, match="HV_BASE_URL"):
            Settings.from_env({"HV_BASE_URL": "srv1515969.hstgr.cloud/voice-gen"})

    def test_https_is_required_when_cookies_are_secure(self):
        with pytest.raises(ValueError, match="https"):
            Settings.from_env(
                {
                    "HV_ENV": "production",
                    "HV_SECRET_KEY": "s3cret",
                    "HV_INVITE_CODES": "abc",
                    "HV_BASE_URL": f"http://srv1515969.hstgr.cloud{PREFIX}",
                }
            )


class TestOriginCheck:
    """The failure that would make the whole app look broken."""

    def test_the_hosts_own_origin_is_accepted(self, sub_client):
        # The browser sends scheme://host with no path. Comparing that against
        # a base URL carrying /voice-gen used to 403 every single write.
        response = sub_client.post(
            f"{PREFIX}/api/auth/signup",
            json={"email": EMAIL, "password": PASSWORD, "invite_code": INVITE},
            headers={"Origin": HOST},
        )
        assert response.status_code == 202, response.text

    def test_another_origin_is_still_blocked(self, sub_auth):
        response = sub_auth.post(
            f"{PREFIX}/api/synthesize",
            json={"text": "שלום"},
            headers={**csrf(sub_auth), "Origin": "https://evil.example"},
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "cross_origin_blocked"


class TestRoutingUnderThePrefix:
    def test_healthz_answers_under_the_prefix(self, sub_client):
        assert sub_client.get(f"{PREFIX}/healthz").json()["status"] == "ok"

    def test_static_files_are_served_under_the_prefix(self, sub_client):
        assert sub_client.get(f"{PREFIX}/static/css/app.css").status_code == 200
        assert sub_client.get(f"{PREFIX}/static/js/api.js").status_code == 200

    def test_a_stripped_path_still_routes(self, sub_client):
        """nginx `proxy_pass .../;` strips the prefix - that must work too."""
        assert sub_client.get("/healthz").json()["status"] == "ok"

    def test_the_prefix_alone_reaches_the_app(self, sub_client):
        response = sub_client.get(PREFIX, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == f"{PREFIX}/login"


class TestEmittedUrls:
    def test_anonymous_root_redirects_inside_the_prefix(self, sub_client):
        response = sub_client.get(f"{PREFIX}/", follow_redirects=False)
        assert response.headers["location"] == f"{PREFIX}/login"

    def test_the_page_links_to_prefixed_assets(self, sub_client):
        html = sub_client.get(f"{PREFIX}/login").text
        assert f'data-base="{PREFIX}"' in html
        assert f'href="{PREFIX}/static/css/app.css' in html
        assert f'src="{PREFIX}/static/js/theme.js' in html
        assert f'href="{PREFIX}/signup"' in html
        assert 'href="/static' not in html

    def test_the_app_shell_links_to_prefixed_assets(self, sub_auth):
        html = sub_auth.get(f"{PREFIX}/").text
        assert f'src="{PREFIX}/static/js/app.js' in html

    def test_generation_urls_carry_the_prefix(self, sub_auth, fake_tts):
        created = sub_auth.post(
            f"{PREFIX}/api/synthesize", json={"text": "שלום עולם"}, headers=csrf(sub_auth)
        ).json()
        for key in ("audio", "srt", "vtt"):
            assert created["urls"][key].startswith(f"{PREFIX}/api/generations/")
        # And the URLs it handed out actually resolve.
        assert sub_auth.get(created["urls"]["audio"]).status_code == 200
        assert sub_auth.get(created["urls"]["vtt"]).status_code == 200

    def test_history_urls_carry_the_prefix(self, sub_auth, fake_tts):
        sub_auth.post(
            f"{PREFIX}/api/synthesize", json={"text": "שלום"}, headers=csrf(sub_auth)
        )
        item = sub_auth.get(f"{PREFIX}/api/generations").json()["items"][0]
        assert item["urls"]["audio"].startswith(f"{PREFIX}/api/")


class TestCookies:
    def test_cookies_are_scoped_to_the_prefix(self, sub_client, sub_settings):
        from hebrew_voice import repo

        sub_client.post(
            f"{PREFIX}/api/auth/signup",
            json={"email": EMAIL, "password": PASSWORD, "invite_code": INVITE},
        )
        user = repo.get_user_by_email(sub_settings.db_path, EMAIL)
        repo.mark_email_verified(sub_settings.db_path, user.id)
        response = sub_client.post(
            f"{PREFIX}/api/auth/login", json={"email": EMAIL, "password": PASSWORD}
        )
        # Scoped so the session isn't sent to the app sharing the hostname.
        cookies = response.headers.get_list("set-cookie")
        assert cookies and all(f"Path={PREFIX}/" in c for c in cookies)

    def test_logout_clears_them_from_the_same_path(self, sub_auth):
        response = sub_auth.post(f"{PREFIX}/api/auth/logout", headers=csrf(sub_auth))
        assert response.status_code == 204
        # delete_cookie must use the set path, or the cookie survives logout.
        assert all(f"Path={PREFIX}/" in c for c in response.headers.get_list("set-cookie"))
        assert sub_auth.get(f"{PREFIX}/api/auth/me").status_code == 401


class TestUnauthenticatedHandling:
    def test_an_api_call_gets_json_not_a_redirect(self, sub_client):
        """An expired session on an XHR must 401, so the JS can react."""
        response = sub_client.get(
            f"{PREFIX}/api/generations",
            headers={"Accept": "text/html,application/xhtml+xml"},
            follow_redirects=False,
        )
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "auth_required"

    def test_a_page_view_redirects_to_the_prefixed_login(self, sub_client):
        response = sub_client.get(
            f"{PREFIX}/", headers={"Accept": "text/html"}, follow_redirects=False
        )
        assert response.status_code == 303
        assert response.headers["location"].startswith(f"{PREFIX}/login")


class TestEndToEndUnderPrefix:
    def test_full_flow(self, sub_auth, fake_tts):
        created = sub_auth.post(
            f"{PREFIX}/api/synthesize",
            json={"text": 'שלום עולם, 50 ₪ בע"מ'},
            headers=csrf(sub_auth),
        ).json()
        assert sub_auth.get(created["urls"]["audio"]).headers["content-type"] == "audio/mpeg"
        assert sub_auth.get(created["urls"]["srt"]).text.startswith("1\n")
        assert len(sub_auth.get(f"{PREFIX}/api/generations").json()["items"]) == 1
        assert sub_auth.delete(
            f"{PREFIX}/api/generations/{created['id']}", headers=csrf(sub_auth)
        ).status_code == 204
        assert sub_auth.get(f"{PREFIX}/api/generations").json()["items"] == []
