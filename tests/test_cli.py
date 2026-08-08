"""The command line interface, driven in-process."""

import json

import pytest

from hebrew_voice import cli, synth

from . import fakes


class TestSay:
    def test_writes_audio_and_subtitles(self, monkeypatch, tmp_path, capsys):
        fakes.install(monkeypatch, synth)
        out = tmp_path / "vo.mp3"
        assert cli.main(["say", "שלום עולם", "-o", str(out), "--srt"]) == 0
        assert out.exists()
        assert out.with_suffix(".srt").read_text(encoding="utf-8").startswith("1\n")

    def test_reads_a_script_file(self, monkeypatch, tmp_path):
        fakes.install(monkeypatch, synth)
        script = tmp_path / "script.txt"
        script.write_text("שלום מהקובץ", encoding="utf-8")
        out = tmp_path / "vo.mp3"
        assert cli.main(["say", "-f", str(script), "-o", str(out)]) == 0
        assert fakes.FakeCommunicate.instances[-1].text == "שלום מהקובץ"

    def test_voice_and_rate_reach_the_engine(self, monkeypatch, tmp_path):
        fakes.install(monkeypatch, synth)
        cli.main(["say", "שלום", "-o", str(tmp_path / "a.mp3"), "-v", "avri", "-r", "+20%"])
        call = fakes.FakeCommunicate.instances[-1]
        assert call.voice == "he-IL-AvriNeural"
        assert call.kwargs["rate"] == "+20%"

    def test_split_writes_one_file_per_paragraph(self, monkeypatch, tmp_path):
        fakes.install(monkeypatch, synth)
        script = tmp_path / "s.txt"
        script.write_text("פסקה אחת\n\nפסקה שתיים", encoding="utf-8")
        out = tmp_path / "parts"
        assert cli.main(["say", "-f", str(script), "-o", str(out), "--split", "paragraph"]) == 0
        assert len(list(out.glob("*.mp3"))) == 2

    def test_an_unknown_voice_fails_cleanly(self, monkeypatch, tmp_path, capsys):
        fakes.install(monkeypatch, synth)
        assert cli.main(["say", "שלום", "-o", str(tmp_path / "a.mp3"), "-v", "bogus"]) == 2
        assert "unknown voice" in capsys.readouterr().err


class TestVoices:
    def test_lists_the_catalog_without_the_network(self, capsys):
        assert cli.main(["voices"]) == 0
        out = capsys.readouterr().out
        assert "he-IL-HilaNeural" in out and "he-IL-AvriNeural" in out

    def test_json_output_is_parseable(self, capsys):
        cli.main(["voices", "--json"])
        assert len(json.loads(capsys.readouterr().out)) == 2


class TestPrepare:
    def test_shows_the_cleaned_text(self, capsys):
        cli.main(["prepare", 'שָׁלוֹם ע"י דני'])
        assert capsys.readouterr().out.strip() == "שלום על ידי דני"

    def test_can_keep_niqqud(self, capsys):
        cli.main(["prepare", "שָׁלוֹם", "--keep-niqqud"])
        assert capsys.readouterr().out.strip() == "שָׁלוֹם"


class TestBatch:
    def test_one_file_per_line(self, monkeypatch, tmp_path):
        fakes.install(monkeypatch, synth)
        manifest = tmp_path / "lines.txt"
        manifest.write_text("שורה ראשונה\nשורה שנייה\n", encoding="utf-8")
        out = tmp_path / "out"
        assert cli.main(["batch", str(manifest), "-d", str(out)]) == 0
        assert sorted(p.name for p in out.glob("*.mp3")) == ["line-001.mp3", "line-002.mp3"]

    def test_json_manifest_names_the_files(self, monkeypatch, tmp_path):
        fakes.install(monkeypatch, synth)
        manifest = tmp_path / "m.json"
        manifest.write_text(
            json.dumps([{"name": "intro", "text": "שלום"}]), encoding="utf-8"
        )
        out = tmp_path / "out"
        cli.main(["batch", str(manifest), "-d", str(out)])
        assert (out / "intro.mp3").exists()


class TestAdmin:
    @pytest.fixture(autouse=True)
    def isolated_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HV_DATA_DIR", str(tmp_path / "data"))
        monkeypatch.setenv("HV_ENV", "development")
        monkeypatch.setenv("HV_SCRYPT_N", "1024")
        monkeypatch.delenv("HV_DB_PATH", raising=False)

    def test_initdb_then_add_and_list_a_user(self, capsys):
        assert cli.main(["initdb"]) == 0
        assert "schema v" in capsys.readouterr().out

        assert cli.main(["user", "add", "a@b.co", "--password", "0123456789"]) == 0
        assert "created a@b.co" in capsys.readouterr().out

        cli.main(["user", "list"])
        assert "a@b.co" in capsys.readouterr().out

    def test_a_short_password_is_refused(self):
        cli.main(["initdb"])
        with pytest.raises(SystemExit):
            cli.main(["user", "add", "a@b.co", "--password", "short"])

    def test_a_duplicate_user_is_refused(self):
        cli.main(["initdb"])
        cli.main(["user", "add", "a@b.co", "--password", "0123456789"])
        with pytest.raises(SystemExit):
            cli.main(["user", "add", "a@b.co", "--password", "0123456789"])

    def test_disable_revokes_sessions(self, capsys):
        from hebrew_voice import repo
        from hebrew_voice.config import Settings

        cli.main(["initdb"])
        cli.main(["user", "add", "a@b.co", "--password", "0123456789"])
        settings = Settings.from_env()
        user = repo.get_user_by_email(settings.db_path, "a@b.co")
        repo.create_session(
            settings.db_path, token_hash="t", user_id=user.id, csrf_token="c", ttl_seconds=60
        )
        cli.main(["user", "disable", "a@b.co"])
        assert repo.get_session_with_user(settings.db_path, "t") is None

    def test_cleanup_dry_run(self, capsys):
        cli.main(["initdb"])
        assert cli.main(["cleanup", "--dry-run"]) == 0
        assert "would remove" in capsys.readouterr().out
