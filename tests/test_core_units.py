"""Security, storage, config, voices, and the database layer."""

import pytest

from hebrew_voice import db, repo, security, storage, voices
from hebrew_voice.config import Settings, load_dotenv
from hebrew_voice.errors import NotFound

CHEAP = {"n": 1024, "r": 8, "p": 1}


class TestPasswords:
    def test_round_trip(self):
        encoded = security.hash_password("a-good-password", **CHEAP)
        assert security.verify_password("a-good-password", encoded)
        assert not security.verify_password("a-bad-password", encoded)

    def test_the_salt_makes_every_hash_unique(self):
        first = security.hash_password("same", **CHEAP)
        second = security.hash_password("same", **CHEAP)
        assert first != second
        assert security.verify_password("same", first)

    def test_the_encoding_records_its_parameters(self):
        encoded = security.hash_password("password", **CHEAP)
        assert encoded.startswith("scrypt$1024$8$1$")

    @pytest.mark.parametrize(
        "bad", ["", "not-a-hash", "scrypt$x$8$1$aa$bb", "bcrypt$1$2$3$4$5", "scrypt$1024$8$1$aa"]
    )
    def test_a_malformed_hash_never_verifies(self, bad):
        assert not security.verify_password("password", bad)

    def test_needs_rehash_tracks_the_parameters(self):
        encoded = security.hash_password("password", **CHEAP)
        assert not security.needs_rehash(encoded, **CHEAP)
        assert security.needs_rehash(encoded, n=1 << 15, r=8, p=1)

    def test_an_empty_password_is_refused(self):
        with pytest.raises(ValueError):
            security.hash_password("")

    def test_session_tokens_are_hashed_before_storage(self):
        token = security.new_session_token()
        assert security.hash_session_token(token) != token
        assert security.hash_session_token(token) == security.hash_session_token(token)


class TestStorage:
    def test_ids_look_right(self):
        gen_id = storage.new_generation_id()
        assert len(gen_id) == 32 and gen_id == gen_id.lower()

    def test_paths_are_sharded_by_user_and_month(self):
        gen_id = storage.new_generation_id()
        paths = storage.relative_paths(7, gen_id, when=1_700_000_000)
        assert paths.audio_rel == f"audio/7/2023/11/{gen_id}.mp3"

    def test_a_bad_id_is_refused_before_it_becomes_a_path(self):
        with pytest.raises(ValueError):
            storage.relative_paths(1, "../../etc/passwd")

    def test_writes_are_atomic(self, tmp_path):
        paths = storage.relative_paths(1, storage.new_generation_id())
        storage.write_artifacts(tmp_path, paths, audio=b"\xff\xfb", srt="1\n")
        assert (tmp_path / paths.audio_rel).read_bytes() == b"\xff\xfb"
        assert not list(tmp_path.rglob("*.tmp"))

    def test_only_requested_subtitles_are_written(self, tmp_path):
        paths = storage.relative_paths(1, storage.new_generation_id())
        written = storage.write_artifacts(tmp_path, paths, audio=b"x")
        assert written.srt_rel is None and written.vtt_rel is None

    @pytest.mark.parametrize("escape", ["../outside.mp3", "../../etc/passwd", "/etc/passwd"])
    def test_resolve_refuses_to_escape_the_data_directory(self, tmp_path, escape):
        (tmp_path.parent / "outside.mp3").write_bytes(b"secret")
        with pytest.raises(NotFound):
            storage.resolve_under(tmp_path, escape)

    def test_resolve_refuses_a_symlink_out(self, tmp_path):
        target = tmp_path.parent / "secret.mp3"
        target.write_bytes(b"secret")
        (tmp_path / "link.mp3").symlink_to(target)
        with pytest.raises(NotFound):
            storage.resolve_under(tmp_path, "link.mp3")

    def test_delete_tolerates_missing_files(self, tmp_path):
        assert storage.delete_files(tmp_path, ["audio/nope.mp3", None]) == 0


class TestDatabase:
    def test_migrate_is_idempotent(self, tmp_path):
        path = tmp_path / "db.sqlite"
        assert db.migrate(path) == db.migrate(path)

    def test_wal_is_enabled(self, tmp_path):
        path = tmp_path / "db.sqlite"
        db.migrate(path)
        with db.connect(path) as conn:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"

    def test_deleting_a_user_cascades(self, tmp_path):
        path = tmp_path / "db.sqlite"
        db.migrate(path)
        user = repo.create_user(path, email="a@b.co", password_hash="x")
        repo.create_session(
            path, token_hash="t", user_id=user.id, csrf_token="c", ttl_seconds=60
        )
        with db.connect(path) as conn:
            conn.execute("DELETE FROM users WHERE id = ?", (user.id,))
            assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0

    def test_duplicate_emails_are_rejected(self, tmp_path):
        path = tmp_path / "db.sqlite"
        db.migrate(path)
        repo.create_user(path, email="a@b.co", password_hash="x")
        with pytest.raises(repo.EmailTaken):
            repo.create_user(path, email="A@B.CO", password_hash="x")


class TestVoices:
    @pytest.mark.parametrize(
        "alias,expected",
        [
            ("hila", "he-IL-HilaNeural"),
            ("female", "he-IL-HilaNeural"),
            ("הילה", "he-IL-HilaNeural"),
            ("avri", "he-IL-AvriNeural"),
            ("זכר", "he-IL-AvriNeural"),
            (None, "he-IL-HilaNeural"),
        ],
    )
    def test_aliases_resolve(self, alias, expected):
        assert voices.resolve_voice(alias) == expected

    def test_the_cli_may_use_a_foreign_voice(self):
        assert voices.resolve_voice("en-US-AriaNeural") == "en-US-AriaNeural"

    def test_the_web_layer_may_not(self):
        with pytest.raises(ValueError):
            voices.resolve_voice("en-US-AriaNeural", allow_unknown=False)

    def test_catalog_is_json_safe(self):
        entry = voices.catalog()[0]
        assert set(entry) == {"id", "gender", "label", "description", "default"}


class TestSettings:
    def test_production_demands_a_secret(self, monkeypatch):
        monkeypatch.setenv("HV_ENV", "production")
        monkeypatch.setenv("HV_BASE_URL", "https://voice.example.com")
        monkeypatch.setenv("HV_INVITE_CODES", "abc")
        monkeypatch.delenv("HV_SECRET_KEY", raising=False)
        with pytest.raises(ValueError, match="HV_SECRET_KEY"):
            Settings.from_env()

    def test_production_refuses_open_signup_without_codes(self, monkeypatch):
        for key, value in {
            "HV_ENV": "production",
            "HV_SECRET_KEY": "s3cret",
            "HV_BASE_URL": "https://voice.example.com",
            "HV_INVITE_CODES": "",
        }.items():
            monkeypatch.setenv(key, value)
        with pytest.raises(ValueError, match="HV_INVITE_CODES"):
            Settings.from_env()

    def test_development_defaults_are_usable(self, monkeypatch):
        for key in list(os_environ_keys()):
            monkeypatch.delenv(key, raising=False)
        settings = Settings.from_env()
        assert settings.max_chars == 10_000
        assert not settings.is_production

    def test_dotenv_never_overrides_the_real_environment(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text("HV_PORT=9999\nHV_HOST=0.0.0.0\n", encoding="utf-8")
        monkeypatch.setenv("HV_PORT", "1234")
        load_dotenv(env_file)
        assert Settings.from_env().port == 1234  # systemd wins over a stale .env

    def test_invite_codes_are_split_and_trimmed(self, monkeypatch):
        monkeypatch.setenv("HV_INVITE_CODES", " one , two ,")
        assert Settings.from_env().invite_codes == ["one", "two"]


def os_environ_keys():
    import os

    return [key for key in os.environ if key.startswith("HV_")]
