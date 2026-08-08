"""Command line interface.

``hebrew-voice say "שלום עולם" -o out.mp3`` is the scripting equivalent of the
web app, and the quickest way to prove the TTS service is reachable from a new
machine.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import sys
from pathlib import Path
from typing import List, Optional, Sequence

from . import __version__
from .synth import SynthesisOptions, synthesize, synthesize_batch, synthesize_split, to_srt, to_vtt
from .text import prepare
from .voices import HEBREW_VOICES, list_hebrew_voices, resolve_voice

__all__ = ["main", "build_parser"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hebrew-voice",
        description="Hebrew text-to-speech, powered by edge-tts.",
    )
    parser.add_argument("--version", action="version", version=f"hebrew-voice {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    # -- say ---------------------------------------------------------------
    say = sub.add_parser("say", help="synthesize text to an audio file")
    source = say.add_mutually_exclusive_group()
    source.add_argument("text", nargs="?", help="the text to speak; omit to read stdin")
    source.add_argument("-f", "--file", type=Path, help="read the text from a file")
    say.add_argument("-o", "--output", type=Path, default=Path("out.mp3"), help="output .mp3")
    say.add_argument("-v", "--voice", default=None, help="voice name or alias (hila/avri)")
    say.add_argument("-r", "--rate", default="+0%", help="speaking rate, e.g. +10%%")
    say.add_argument("-p", "--pitch", default="+0Hz", help="pitch, e.g. -5Hz")
    say.add_argument("--volume", default="+0%", help="volume, e.g. +10%%")
    say.add_argument("--srt", type=Path, nargs="?", const=True, help="also write subtitles")
    say.add_argument("--vtt", type=Path, nargs="?", const=True, help="also write WebVTT")
    say.add_argument("--keep-niqqud", action="store_true", help="don't strip vowel points")
    say.add_argument("--no-symbols", action="store_true", help="don't read symbols aloud")
    say.add_argument("--no-abbreviations", action="store_true", help="don't expand abbreviations")
    say.add_argument("--split", choices=("paragraph", "sentence", "chars"),
                     help="write one file per chunk into the output directory")
    say.add_argument("--max-chars", type=int, default=1200, help="chunk size when splitting")
    say.add_argument("--proxy", default=None, help="HTTP proxy for the TTS service")
    say.add_argument("-q", "--quiet", action="store_true")

    # -- voices ------------------------------------------------------------
    voices = sub.add_parser("voices", help="list the available Hebrew voices")
    voices.add_argument("--live", action="store_true", help="query the service instead of the catalog")
    voices.add_argument("--json", action="store_true", help="machine-readable output")

    # -- prepare -----------------------------------------------------------
    prep = sub.add_parser("prepare", help="show how text will be cleaned before speaking")
    prep.add_argument("text", nargs="?")
    prep.add_argument("-f", "--file", type=Path)
    prep.add_argument("--keep-niqqud", action="store_true")

    # -- batch -------------------------------------------------------------
    batch = sub.add_parser("batch", help="synthesize many lines, one file each")
    batch.add_argument("manifest", type=Path, help="a .txt (one line each) or .json list")
    batch.add_argument("-d", "--out-dir", type=Path, default=Path("out"))
    batch.add_argument("-v", "--voice", default=None)
    batch.add_argument("-r", "--rate", default="+0%")
    batch.add_argument("-c", "--concurrency", type=int, default=3)

    # -- serve -------------------------------------------------------------
    serve = sub.add_parser("serve", help="run the web application")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--reload", action="store_true")

    # -- admin -------------------------------------------------------------
    sub.add_parser("initdb", help="create or migrate the database")

    user = sub.add_parser("user", help="manage accounts")
    user_sub = user.add_subparsers(dest="user_command", required=True)
    add = user_sub.add_parser("add", help="create an account")
    add.add_argument("email")
    add.add_argument("--password", help="prompted for if omitted")
    add.add_argument("--admin", action="store_true")
    user_sub.add_parser("list", help="list accounts")
    verify = user_sub.add_parser("verify", help="mark an address confirmed by hand")
    verify.add_argument("email")
    passwd = user_sub.add_parser("passwd", help="change a password")
    passwd.add_argument("email")
    passwd.add_argument("--password")
    disable = user_sub.add_parser("disable", help="disable an account and drop its sessions")
    disable.add_argument("email")
    enable = user_sub.add_parser("enable", help="re-enable an account")
    enable.add_argument("email")

    clean = sub.add_parser("cleanup", help="run the retention sweep now")
    clean.add_argument("--dry-run", action="store_true")

    return parser


def _read_text(inline: Optional[str], file: Optional[Path]) -> str:
    if file:
        return file.read_text(encoding="utf-8")
    if inline:
        return inline
    if not sys.stdin.isatty():
        return sys.stdin.read()
    raise SystemExit("no text given: pass a string, use --file, or pipe stdin")


def _subtitle_path(flag, output: Path, extension: str) -> Optional[Path]:
    if flag is None:
        return None
    return output.with_suffix(extension) if flag is True else Path(flag)


def _cmd_say(args: argparse.Namespace) -> int:
    raw = _read_text(args.text, args.file)
    opts = SynthesisOptions(
        voice=resolve_voice(args.voice),
        rate=args.rate,
        pitch=args.pitch,
        volume=args.volume,
        keep_niqqud=args.keep_niqqud,
        expand_symbols=not args.no_symbols,
        expand_abbreviations=not args.no_abbreviations,
        proxy=args.proxy,
    )
    report = (lambda message: None) if args.quiet else (lambda message: print(message))

    if args.split:
        results = asyncio.run(
            synthesize_split(
                raw, args.output, opts, by=args.split, max_chars=args.max_chars, progress=report
            )
        )
        report(f"wrote {len(results)} files to {args.output}")
        return 0

    result = asyncio.run(
        synthesize(
            raw,
            args.output,
            opts,
            srt=_subtitle_path(args.srt, args.output, ".srt"),
            vtt=_subtitle_path(args.vtt, args.output, ".vtt"),
            progress=report,
        )
    )
    report(f"{result.duration:.1f}s of audio, {len(result.cues)} cues")
    return 0


def _cmd_voices(args: argparse.Namespace) -> int:
    if args.live:
        found = asyncio.run(list_hebrew_voices())
        if args.json:
            print(json.dumps(found, ensure_ascii=False, indent=2))
        else:
            for voice in found:
                print(f"{voice['ShortName']:<24} {voice['Gender']:<8} {voice['FriendlyName']}")
        return 0

    if args.json:
        from .voices import catalog

        print(json.dumps(catalog(), ensure_ascii=False, indent=2))
    else:
        for voice in HEBREW_VOICES:
            aliases = ", ".join(voice.aliases[:3])
            print(f"{voice.short_name:<24} {voice.gender:<8} {voice.label:<8} ({aliases})")
    return 0


def _cmd_prepare(args: argparse.Namespace) -> int:
    raw = _read_text(args.text, args.file)
    print(prepare(raw, keep_niqqud=args.keep_niqqud))
    return 0


def _cmd_batch(args: argparse.Namespace) -> int:
    manifest = args.manifest
    if manifest.suffix == ".json":
        entries = json.loads(manifest.read_text(encoding="utf-8"))
        lines = [e["text"] if isinstance(e, dict) else str(e) for e in entries]
        names = [
            e.get("name") if isinstance(e, dict) and e.get("name") else f"line-{i:03d}"
            for i, e in enumerate(entries, start=1)
        ]
    else:
        lines = [ln.strip() for ln in manifest.read_text(encoding="utf-8").splitlines() if ln.strip()]
        names = [f"line-{i:03d}" for i in range(1, len(lines) + 1)]

    args.out_dir.mkdir(parents=True, exist_ok=True)
    items = [(line, args.out_dir / f"{name}.mp3") for line, name in zip(lines, names)]
    opts = SynthesisOptions(voice=resolve_voice(args.voice), rate=args.rate)
    results = asyncio.run(
        synthesize_batch(items, opts, concurrency=args.concurrency, progress=print)
    )
    print(f"wrote {len(results)} files to {args.out_dir}")
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    from .config import Settings, load_dotenv

    load_dotenv()
    settings = Settings.from_env()
    try:
        import uvicorn
    except ImportError:
        raise SystemExit("the web app needs the extra dependencies: pip install 'hebrew-voice[web]'")

    uvicorn.run(
        "hebrew_voice.web.app:app",
        host=args.host or settings.host,
        port=args.port or settings.port,
        reload=args.reload,
        log_level=settings.log_level,
        workers=1,  # the rate limiter and concurrency caps are per-process
    )
    return 0


def _settings():
    from .config import Settings, load_dotenv

    load_dotenv()
    return Settings.from_env()


def _cmd_initdb(_args: argparse.Namespace) -> int:
    from . import db, storage

    settings = _settings()
    version = db.migrate(settings.db_path)
    storage.ensure_data_dir(settings.data_dir)
    print(f"database ready at {settings.db_path} (schema v{version})")
    return 0


def _cmd_user(args: argparse.Namespace) -> int:
    from . import repo, security
    from .db import migrate

    settings = _settings()
    migrate(settings.db_path)

    def hash_for(supplied: Optional[str]) -> str:
        password = supplied or getpass.getpass("Password: ")
        if len(password) < 10:
            raise SystemExit("password must be at least 10 characters")
        return security.hash_password(
            password, n=settings.scrypt_n, r=settings.scrypt_r, p=settings.scrypt_p
        )

    if args.user_command == "add":
        try:
            user = repo.create_user(
                settings.db_path,
                email=args.email,
                password_hash=hash_for(args.password),
                is_admin=args.admin,
                # An admin at a shell has already vouched for the address, and
                # bootstrapping a new box shouldn't depend on working SMTP.
                verified=True,
            )
        except repo.EmailTaken:
            raise SystemExit(f"{args.email} is already registered")
        print(f"created {user.email} (id={user.id}, admin={user.is_admin})")
        return 0

    if args.user_command == "list":
        for user in repo.list_users(settings.db_path):
            flags = ",".join(
                part
                for part in (
                    "admin" if user.is_admin else "",
                    "" if user.is_active else "disabled",
                    "" if user.is_verified else "unverified",
                )
                if part
            )
            print(f"{user.id:<4} {user.email:<32} {flags}")
        return 0

    user = repo.get_user_by_email(settings.db_path, args.email)
    if user is None:
        raise SystemExit(f"no such user: {args.email}")

    if args.user_command == "passwd":
        repo.set_password(settings.db_path, user.id, hash_for(args.password))
        repo.delete_user_sessions(settings.db_path, user.id)
        print(f"password updated for {user.email}; existing sessions revoked")
    elif args.user_command == "verify":
        repo.mark_email_verified(settings.db_path, user.id)
        print(f"{user.email} is now verified")
    elif args.user_command == "disable":
        repo.set_active(settings.db_path, user.id, False)
        print(f"disabled {user.email}")
    elif args.user_command == "enable":
        repo.set_active(settings.db_path, user.id, True)
        print(f"enabled {user.email}")
    return 0


def _cmd_cleanup(args: argparse.Namespace) -> int:
    from . import cleanup
    from .db import migrate

    settings = _settings()
    migrate(settings.db_path)
    result = cleanup.sweep(settings, dry_run=args.dry_run)
    prefix = "would remove" if args.dry_run else "removed"
    print(f"{prefix} {result}")
    return 0


_COMMANDS = {
    "say": _cmd_say,
    "voices": _cmd_voices,
    "prepare": _cmd_prepare,
    "batch": _cmd_batch,
    "serve": _cmd_serve,
    "initdb": _cmd_initdb,
    "user": _cmd_user,
    "cleanup": _cmd_cleanup,
}


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return _COMMANDS[args.command](args)
    except KeyboardInterrupt:
        return 130
    except (ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
