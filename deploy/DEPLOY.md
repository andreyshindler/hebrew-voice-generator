# Deploying to a VPS

A runbook for Debian/Ubuntu. Adjust paths and the domain to taste.

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
