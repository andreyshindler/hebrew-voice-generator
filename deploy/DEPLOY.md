# Deploying to a VPS

Two paths below. **[Docker under a subpath](#docker-under-a-subpath)** is the one to
follow when the hostname's root already belongs to another app — that's the
`srv1515969.hstgr.cloud/voice-gen` case. The [native systemd install](#native-install-at-the-root)
after it is for owning a whole hostname.

---

# Docker under a subpath

Target: `https://srv1515969.hstgr.cloud/voice-gen/`, with occy still served at `/`,
behind the nginx that is already on the box.

## 1. Get the code and configure

```bash
sudo mkdir -p /opt/hebrew-voice && cd /opt/hebrew-voice
sudo git clone https://github.com/andreyshindler/hebrew-voice-generator .
sudo cp .env.example .env
sudo chmod 600 .env          # it holds the secret key and the invite codes
sudo -e .env
```

The only lines that matter:

```ini
HV_ENV=production
HV_SECRET_KEY=<python3 -c "import secrets; print(secrets.token_urlsafe(48))">
# Full public URL including the subpath. HV_ROOT_PATH is derived from it, so
# this one setting makes every link, redirect, and cookie carry /voice-gen.
HV_BASE_URL=https://srv1515969.hstgr.cloud/voice-gen
HV_INVITE_CODES=<a code you share with whoever should get in>
```

Leave `HV_DATA_DIR` alone — compose overrides it to `/data` inside the container.

## 2. Start it

```bash
sudo docker compose up -d --build
sudo docker compose ps                       # healthy?
curl -s localhost:8080/healthz               # {"status":"ok",...}
```

The container listens on `127.0.0.1:8080` only; nothing is exposed publicly yet.

## 3. Wire up nginx

Open the existing server block for the hostname — the one that already proxies occy —
and paste in the two blocks from [`nginx-subpath.conf.example`](nginx-subpath.conf.example):

```bash
sudo -e /etc/nginx/sites-available/<the-existing-site>
sudo nginx -t && sudo systemctl reload nginx
```

Order matters only in that `location /voice-gen/` is more specific than `location /`, so
nginx picks it first regardless of where you paste it. occy keeps serving everything else.

## 4. Create your account

```bash
sudo docker compose exec hebrew-voice hebrew-voice user add you@example.com
```

Or just register at `https://srv1515969.hstgr.cloud/voice-gen/signup` with the invite
code. Either way the first account created becomes the admin.

## 5. Verify

```bash
curl -sI https://srv1515969.hstgr.cloud/voice-gen/healthz    # 200
curl -sI https://srv1515969.hstgr.cloud/                     # occy, unchanged
curl -sI https://srv1515969.hstgr.cloud/voice-gen            # 301 -> /voice-gen/
```

Then in a browser: log in, generate a short line, play it, download the MP3. **This is
the first time the real Microsoft TTS endpoint is contacted** — everything before it is
faked in CI. If generation fails with `tts_upstream_failed`, check the container can
reach the internet:

```bash
sudo docker compose exec hebrew-voice hebrew-voice say "בדיקה" -o /tmp/t.mp3
```

## Day-to-day

```bash
sudo docker compose logs -f                       # logs
sudo docker compose exec hebrew-voice hebrew-voice user list
sudo docker compose exec hebrew-voice hebrew-voice cleanup --dry-run
sudo docker compose up -d --build                 # upgrade (migrations run at startup)
```

Back up the named volume — it holds the database and every generated file:

```bash
sudo docker compose exec hebrew-voice \
    sqlite3 /data/hebrew-voice.db ".backup /data/backup.db"
sudo docker cp hebrew-voice:/data/backup.db ./hv-$(date +%F).db
```

## If something looks wrong

| Symptom | Cause |
| --- | --- |
| Page loads with no styling | You hit `/voice-gen` without the trailing slash and the `location = /voice-gen` redirect isn't installed. |
| Login says "success" but you stay logged out | `HV_BASE_URL` is `http://` while `HV_SECURE_COOKIES` is on, so the browser drops the cookie. The app refuses to boot in this state — check you actually restarted it. |
| Every action returns 403 | `HV_BASE_URL`'s host doesn't match the hostname you're browsing. The origin check compares hosts. |
| 504 on long text | nginx `proxy_read_timeout` is below `HV_SYNTH_TIMEOUT`. The example sets 300s vs 180s. |

---

# Native install at the root

A runbook for Debian/Ubuntu when the app owns a whole hostname. Adjust paths and the
domain to taste.

## 1. System user and directories

```bash
sudo useradd --system --home /opt/hebrew-voice --shell /usr/sbin/nologin hebrewvoice
sudo mkdir -p /opt/hebrew-voice /etc/hebrew-voice
sudo chown hebrewvoice:hebrewvoice /opt/hebrew-voice
```

`/var/lib/hebrew-voice` is created automatically by the unit's `StateDirectory=`.

## 2. Install

```bash
sudo -u hebrewvoice git clone https://github.com/andreyshindler/hebrew-voice-generator \
    /opt/hebrew-voice
cd /opt/hebrew-voice
sudo -u hebrewvoice python3 -m venv .venv
sudo -u hebrewvoice .venv/bin/pip install --upgrade pip
sudo -u hebrewvoice .venv/bin/pip install .
```

Python 3.11 or newer is required.

## 3. Configure

```bash
sudo cp .env.example /etc/hebrew-voice/env
sudo chown root:hebrewvoice /etc/hebrew-voice/env
sudo chmod 0640 /etc/hebrew-voice/env         # it holds the secret and invite codes
sudo -e /etc/hebrew-voice/env
```

At minimum set:

```ini
HV_ENV=production
HV_SECRET_KEY=<python3 -c "import secrets; print(secrets.token_urlsafe(48))">
HV_BASE_URL=https://voice.example.com
HV_DATA_DIR=/var/lib/hebrew-voice
HV_INVITE_CODES=<a code you share with the people you want to let in>
```

The app refuses to start in production if the secret is missing, cookies aren't secure,
`HV_BASE_URL` is unset, or signup is open with no invite codes.

## 4. Initialise and smoke-test

```bash
sudo -u hebrewvoice HV_DATA_DIR=/var/lib/hebrew-voice \
    /opt/hebrew-voice/.venv/bin/hebrew-voice initdb

# The first thing that touches Microsoft's service. If this works, the network
# path is good; if it fails, fix that before blaming the web app.
sudo -u hebrewvoice /opt/hebrew-voice/.venv/bin/hebrew-voice say "בדיקה" -o /tmp/t.mp3
```

Create your account now, or register through the web UI with the invite code:

```bash
sudo -u hebrewvoice HV_DATA_DIR=/var/lib/hebrew-voice \
    /opt/hebrew-voice/.venv/bin/hebrew-voice user add you@example.com
```

## 5. Service

```bash
sudo cp deploy/hebrew-voice.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hebrew-voice
systemctl status hebrew-voice
curl -s localhost:8080/healthz
```

Logs: `journalctl -u hebrew-voice -f`.

## 6. nginx and TLS

```bash
sudo cp deploy/nginx.conf.example /etc/nginx/sites-available/hebrew-voice
sudo sed -i 's/voice.example.com/YOUR-DOMAIN/g' /etc/nginx/sites-available/hebrew-voice
sudo ln -s /etc/nginx/sites-available/hebrew-voice /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d YOUR-DOMAIN
```

**`proxy_read_timeout` must exceed `HV_SYNTH_TIMEOUT`.** The example config sets 300s
against a 180s app timeout. With nginx's 60s default, a long job returns a confusing 504
to the browser while the app quietly finishes and files the result in history.

## 7. Verify

- `https://YOUR-DOMAIN/healthz` returns `{"status":"ok"}`.
- Signing out and visiting `/` redirects to `/login`.
- Registering without the invite code is refused.
- Generating a short line produces audio you can play and download.

## Upgrades

```bash
cd /opt/hebrew-voice
sudo -u hebrewvoice git pull
sudo -u hebrewvoice .venv/bin/pip install .
sudo systemctl restart hebrew-voice     # migrations run automatically at startup
```

## Backups

```bash
sudo -u hebrewvoice sqlite3 /var/lib/hebrew-voice/hebrew-voice.db \
    ".backup /backup/hv-$(date +%F).db"
sudo rsync -a /var/lib/hebrew-voice/audio/ /backup/audio/
```

The database runs in WAL mode, so use `.backup` rather than copying the file directly.

## Operational notes

- **One worker only.** The unit hardcodes `--workers 1`; the concurrency caps and rate
  limiter are per-process and more workers would multiply every limit.
- **Disk.** Retention keeps the newest `HV_HISTORY_KEEP` items per user and deletes
  anything older than `HV_HISTORY_MAX_AGE_DAYS`, sweeping hourly. The app logs a warning
  when free space under the data directory drops below 1 GB.
- **Memory.** `MemoryMax=1G` in the unit. Password hashing is memory-hard by design and
  bounded by a semaphore; if you raise `HV_SCRYPT_N`, raise the memory ceiling too.
- **Rate limits are per process and reset on restart.** The daily quota is in SQLite and
  does survive restarts.
