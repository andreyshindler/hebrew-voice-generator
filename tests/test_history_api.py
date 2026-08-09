"""History listing, per-user isolation, and deletion."""

import pytest

from .conftest import INVITE, csrf, register_verified


def generate(client, text="שלום עולם"):
    return client.post("/api/synthesize", json={"text": text}, headers=csrf(client)).json()


@pytest.fixture
def other_client(app, settings):
    """A second, unrelated account - used for the isolation tests."""
    from fastapi.testclient import TestClient

    client = TestClient(app)
    return register_verified(
        client, settings, email="other@example.com", password="another-password"
    )


class TestListing:
    def test_newest_first(self, auth_client, fake_tts):
        for index in range(3):
            generate(auth_client, f"טקסט מספר {index}")
        titles = [item["title"] for item in auth_client.get("/api/generations").json()["items"]]
        assert titles == ["טקסט מספר 2", "טקסט מספר 1", "טקסט מספר 0"]

    def test_pagination(self, auth_client, fake_tts):
        for index in range(5):
            generate(auth_client, f"פריט {index}")
        page = auth_client.get("/api/generations?limit=2").json()
        assert len(page["items"]) == 2
        assert page["has_more"] is True

    def test_list_omits_the_text_unless_asked(self, auth_client, fake_tts):
        generate(auth_client)
        item = auth_client.get("/api/generations").json()["items"][0]
        assert "text" not in item
        assert "text" in auth_client.get("/api/generations?full=true").json()["items"][0]

    def test_detail_returns_the_text_and_settings(self, auth_client, fake_tts):
        created = generate(auth_client)
        detail = auth_client.get(f"/api/generations/{created['id']}").json()
        assert detail["text"] == "שלום עולם"
        assert detail["options"]["expand_symbols"] is True

    def test_empty_for_a_new_account(self, auth_client):
        assert auth_client.get("/api/generations").json()["items"] == []


class TestIsolation:
    def test_another_users_generation_is_invisible(self, auth_client, other_client, fake_tts):
        created = generate(auth_client)
        # 404, not 403 - an id belonging to someone else shouldn't be
        # distinguishable from one that doesn't exist.
        assert other_client.get(f"/api/generations/{created['id']}").status_code == 404
        assert other_client.get(created["urls"]["audio"]).status_code == 404
        assert other_client.get(created["urls"]["srt"]).status_code == 404
        assert other_client.get("/api/generations").json()["items"] == []

    def test_another_user_cannot_delete_it(self, auth_client, other_client, fake_tts):
        created = generate(auth_client)
        response = other_client.delete(
            f"/api/generations/{created['id']}", headers=csrf(other_client)
        )
        assert response.status_code == 404
        assert auth_client.get(created["urls"]["audio"]).status_code == 200


class TestPathSafety:
    @pytest.mark.parametrize(
        "bad_id",
        [
            "../../../etc/passwd",
            "%2e%2e%2fetc%2fpasswd",
            "not-hex-at-all",
            "A" * 32,  # uppercase is outside the pattern
            "a" * 31,
        ],
    )
    def test_a_malformed_id_never_reaches_the_filesystem(self, auth_client, bad_id):
        response = auth_client.get(f"/api/generations/{bad_id}/audio.mp3")
        assert response.status_code in (404, 422)

    def test_a_corrupted_row_cannot_escape_the_data_directory(self, auth_client, settings, fake_tts):
        created = generate(auth_client)
        from hebrew_voice.db import connect

        with connect(settings.db_path) as conn:
            conn.execute(
                "UPDATE generations SET audio_rel = ? WHERE id = ?",
                ("../../../etc/passwd", created["id"]),
            )
        assert auth_client.get(created["urls"]["audio"]).status_code == 404


class TestDeletion:
    def test_removes_the_row_and_the_files(self, auth_client, settings, fake_tts):
        created = generate(auth_client)
        audio = settings.data_dir / "audio"
        assert list(audio.rglob("*.mp3"))

        assert auth_client.delete(
            f"/api/generations/{created['id']}", headers=csrf(auth_client)
        ).status_code == 204
        assert auth_client.get("/api/generations").json()["items"] == []
        # Nothing left behind: audio, both subtitle files, and the word
        # timings kept for re-rendering them.
        assert not list(audio.rglob("*"))

    def test_deleting_twice_is_a_404(self, auth_client, fake_tts):
        created = generate(auth_client)
        auth_client.delete(f"/api/generations/{created['id']}", headers=csrf(auth_client))
        assert auth_client.delete(
            f"/api/generations/{created['id']}", headers=csrf(auth_client)
        ).status_code == 404
