# מחולל קול עברי · Hebrew Voice Generator

A self-hosted Hebrew text-to-speech service. Log in, paste Hebrew text, pick a voice,
tune the speed, and get an MP3 you can play and download — plus synchronised subtitles
and a history of everything you've made.

It's the equivalent of

```bash
python -m edge_tts --voice he-IL-HilaNeural --file script.txt --write-media vo.mp3
```

wrapped in a proper RTL web app with accounts, quotas, and guard rails, so you can put
it on a VPS and hand the URL to other people.

---

## What it does

- **Two Hebrew voices** — הילה (female) and אברי (male), from Microsoft's Edge TTS.
- **Real controls** — speed, pitch, and volume sliders; every value is validated
  server-side and formatted into the engine's wire format for you.
- **Hebrew text preparation that actually matters.** Neural Hebrew voices stumble on
  pointed text, abbreviations, and symbols. Before anything is spoken the app strips
  niqqud and cantillation, normalises geresh/gershayim and maqaf, expands abbreviations
  (`ע"י` → `על ידי`, `עמ׳` → `עמוד`), reads symbols aloud (`₪` → `שקלים`, `50%` →
  `50 אחוז`), moves currency signs to where Hebrew reads them (`$50` → `50 דולר`), and
  turns acronyms into words so `צה"ל` is pronounced rather than spelled out.
  Every step is a toggle, with a live preview of the text that will really be spoken.
- **Subtitles** — word-timed SRT and WebVTT, generated in the same pass as the audio.
- **Script upload** — drop a `.txt` file onto the text box; it's read in the browser and
  loaded into the editor, still editable before you generate.
- **History** — replay, re-download, or reload the settings of anything you made before.
- **Accounts** — email and password in SQLite, signup gated behind an invite code.
- **Guard rails** — per-request character cap, daily per-user quota, rate limiting, a
  server-wide concurrency cap, and automatic retention cleanup.
- **A CLI** for scripting and for smoke-testing a fresh install.

Four runtime dependencies: `edge-tts`, `fastapi`, `uvicorn`, `jinja2`. Passwords use
stdlib `hashlib.scrypt` and storage uses stdlib `sqlite3`, so there's no ORM, no
password library, and nothing that needs a compiler.

---

## Quick start

```bash
git clone https://github.com/andreyshindler/hebrew-voice-generator
cd hebrew-voice-generator
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

export HV_ENV=development
export HV_SECRET_KEY=dev
export HV_INVITE_CODES=LET-ME-IN
export HV_SECURE_COOKIES=false     # so cookies work over plain http://localhost
export HV_DATA_DIR=./data

hebrew-voice initdb
hebrew-voice serve                 # http://127.0.0.1:8080
```

Open the URL, go to **הרשמה**, and register with the invite code. The first account
created becomes the admin.

Prefer to check the engine works before touching the browser:

```bash
hebrew-voice say "שלום עולם" -o test.mp3
```

---

## The CLI

```bash
hebrew-voice say "שלום עולם" -o vo.mp3          # basic
hebrew-voice say -f script.txt -o vo.mp3 --srt  # from a file, with subtitles
hebrew-voice say -f book.txt -o parts/ --split paragraph   # one file per paragraph
hebrew-voice say "שלום" -o vo.mp3 -v avri -r +15% -p -5Hz  # male voice, faster, lower

hebrew-voice voices                    # the Hebrew catalog
hebrew-voice voices --live             # ask the service directly
hebrew-voice prepare 'ד"ר כהן שילם 50 ₪'   # see exactly what will be spoken
hebrew-voice batch lines.txt -d out/   # one MP3 per line

hebrew-voice initdb                    # create/migrate the database
hebrew-voice user add you@example.com  # create an account (prompts for a password)
hebrew-voice user list | disable | enable | passwd
hebrew-voice cleanup --dry-run         # preview the retention sweep
hebrew-voice serve                     # run the web app
```

---

## Configuration

Everything is an `HV_*` environment variable; see [`.env.example`](.env.example) for the
annotated list. A `.env` file in the working directory is read at startup, but **real
environment variables always win**, so a systemd `EnvironmentFile` beats a stale `.env`.

The settings you'll actually think about:

| Variable | Default | What it does |
| --- | --- | --- |
| `HV_SECRET_KEY` | — | Required in production. |
| `HV_BASE_URL` | — | Full public URL **including any subpath**. Required in production. Sets the allowed origin, and its path becomes the app's URL prefix. |
| `HV_ROOT_PATH` | derived | The prefix the app is served under. Leave unset — it comes from `HV_BASE_URL`. |
| `HV_INVITE_CODES` | empty | Comma-separated codes accepted at signup. |
| `HV_SIGNUP_ENABLED` | `true` | Set false to close registration entirely. |
| `HV_MAX_CHARS` | `10000` | Longest single request. |
| `HV_DAILY_CHAR_QUOTA` | `50000` | Characters per user per day. |
| `HV_QUOTA_TZ` | `Asia/Jerusalem` | When "today" rolls over. |
| `HV_MAX_CONCURRENT_SYNTH` | `3` | Simultaneous jobs server-wide. |
| `HV_SYNTH_TIMEOUT` | `180` | Hard ceiling on one job. Keep nginx's `proxy_read_timeout` above it. |
| `HV_HISTORY_KEEP` / `HV_HISTORY_MAX_AGE_DAYS` | `50` / `30` | Retention policy. |

Production boots refuse to start with a missing secret key, insecure cookies, no
`HV_BASE_URL`, or open signup with no invite codes.

> **Run exactly one worker.** The concurrency cap, the per-user gate, and the rate
> limiter are in-process. `--workers 4` would silently multiply every limit by four. The
> workload is network-bound, not CPU-bound, so one worker is the right shape anyway.

---

## Deploying to a VPS

See [`deploy/DEPLOY.md`](deploy/DEPLOY.md) for the full runbook.

**Sharing a hostname with another app** — the common case, where something else already
owns `/`. Set one variable:

```ini
HV_BASE_URL=https://your-host.example.com/voice-gen
```

and every link, redirect, cookie path, and audio URL the app emits picks up the prefix;
`HV_ROOT_PATH` is derived from it. Then paste the two blocks from
[`deploy/nginx-subpath.conf.example`](deploy/nginx-subpath.conf.example) into the
hostname's existing `server {}` and run `docker compose up -d --build`. The app accepts
the prefix whether or not your proxy strips it, so `proxy_pass` works with or without a
trailing slash.

**Owning a whole hostname** — install into `/opt/hebrew-voice`, put the configuration in
`/etc/hebrew-voice/env`, and use [`deploy/hebrew-voice.service`](deploy/hebrew-voice.service)
with [`deploy/nginx.conf.example`](deploy/nginx.conf.example), with certbot for TLS.

---

## How it's put together

```
hebrew_voice/
  text.py      Hebrew preparation - pure functions, no I/O
  voices.py    voice catalog and name resolution
  synth.py     edge-tts streaming, cues, SRT/VTT, retries
  config.py    HV_* settings
  db.py        sqlite3 connections, schema migrations
  repo.py      every SQL statement
  security.py  scrypt hashing, session tokens
  storage.py   the only module that builds filesystem paths
  quota.py     daily quota helpers and the token bucket
  cleanup.py   retention sweep
  cli.py       command line entry point
  web/         FastAPI app, routes, templates, static assets
```

Two decisions worth knowing about:

**`POST /api/synthesize` returns JSON with an id, not the MP3.** One run produces audio,
subtitles, *and* a history row. Persisting first and handing back URLs means the player,
the three download buttons, and the history list all point at the same stored artifacts;
it keeps HTTP range requests (and therefore seeking) working; and re-downloading later
costs nothing.

**Filenames never come from user input.** Artifacts are named after a server-generated
32-hex id and sharded as `audio/<user>/<year>/<month>/<id>.mp3`. Routes validate the id
against `^[0-9a-f]{32}$` before a handler runs, ownership is part of the SQL query, and
`storage.resolve_under()` refuses any path that resolves outside the data directory.

Other things the design leans on: opaque session tokens stored as SHA-256 (a database
leak yields no usable cookies), double-submit CSRF plus an `Origin`/`Sec-Fetch-Site`
check, a strict CSP with no inline script, and someone else's generation id returning
**404 rather than 403** so ids can't be probed.

---

## Development

```bash
pip install -e ".[dev]"
python -m pytest          # 187 tests, no network needed
```

The suite never touches the real service: an autouse fixture replaces the edge-tts
transport with one that raises if called, and tests install a fake in its place. Tests
marked `network` are excluded by default and are the ones to run against a live
connection after deploying.

---

## Backups

Two things matter: `hebrew-voice.db` and the `audio/` directory under `HV_DATA_DIR`.

```bash
sqlite3 /var/lib/hebrew-voice/hebrew-voice.db ".backup /backup/hv-$(date +%F).db"
rsync -a /var/lib/hebrew-voice/audio/ /backup/audio/
```

Use `.backup` rather than copying the file — the database runs in WAL mode.

---

## A note on the upstream service

`edge-tts` speaks to Microsoft's Edge read-aloud endpoint, which is not a documented
public API. It can change or start refusing datacenter IPs without warning; the version
here is pinned to `>=7.2,<8` and upstream failures surface as a distinct `502
tts_upstream_failed` rather than a generic error. Running this as a login-gated service
for other people is a different proposition from personal CLI use — check that it fits
Microsoft's terms before you open it up.

## Licence

MIT.
