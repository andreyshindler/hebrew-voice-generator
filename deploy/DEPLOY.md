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

Registration also needs a mail relay, because a new account can't do anything until
it opens the confirmation link:

```ini
HV_SMTP_HOST=smtp.gmail.com
HV_SMTP_USER=you@gmail.com
HV_SMTP_PASSWORD=<16-character app password, no spaces>
```

That password is **not** your Google password. Turn on 2-Step Verification, generate one
at <https://myaccount.google.com/apppasswords>, and paste it without the spaces Google
shows. The app refuses to start in production if verification is on and this is missing —
otherwise it would create accounts nobody could ever activate.

Leave `HV_DATA_DIR` alone — compose overrides it to `/data` inside the container.

**Check the host port is free before starting.** A busy VPS often already has something
on 8080:

```bash
sudo ss -ltnp | grep -E '127.0.0.1:(8080|8085|8090|8095)'
sudo nginx -T | grep -oE '127\.0\.0\.1:[0-9]+' | sort -u   # what the proxy already uses
```

If 8080 is taken, pick a free port and add it to `.env` — the container still listens on
8080 internally, so only the published side moves:

```ini
HV_PUBLISH_PORT=8095
```

## 2. Start it

```bash
sudo docker compose up -d --build
sudo docker compose ps                       # healthy?
curl -s localhost:8080/healthz               # or your HV_PUBLISH_PORT
```

The container is published on `127.0.0.1` only; nothing is exposed publicly yet.

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

Accounts made this way are **already confirmed** — an admin at a shell has vouched for
the address — so bootstrapping a new box never depends on working SMTP. Do this first;
it means a mail misconfiguration can't lock you out.

Registering at `https://srv1515969.hstgr.cloud/voice-gen/signup` with the invite code
goes through the email flow instead: the page says "check your inbox", and the account
stays inert until the link is opened. Either way the first account created is the admin.

To confirm an address by hand — say the mail bounced:

```bash
sudo docker compose exec hebrew-voice hebrew-voice user verify someone@example.com
sudo docker compose exec hebrew-voice hebrew-voice user list   # shows "unverified"
```

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

## 6. Automatic deploys

Once this is set up, merging to `main` rebuilds and restarts the container by itself.
GitHub Actions runs the test suite, then SSHes in and runs
[`deploy/deploy.sh`](deploy.sh).

### A dedicated user that can run Docker but not much else

Today everything under `/opt/hebrew-voice` is root-owned and driven with `sudo`. Give the
deploy its own unprivileged user instead — membership of the `docker` group means no
`sudo` rule anywhere:

```bash
sudo useradd -m -G docker deploy
sudo chown -R deploy:deploy /opt/hebrew-voice
sudo -u deploy ssh-keygen -t ed25519 -f /home/deploy/.ssh/deploy_key -N ""
```

> Being in the `docker` group is equivalent to root on the host — that's true of any
> Docker deploy, and it's why the key below can't open a shell.

### Pin the key to the script

Put the **public** key in `/home/deploy/.ssh/authorized_keys` prefixed with a forced
command, all on one line:

```
command="/opt/hebrew-voice/deploy/deploy.sh",no-port-forwarding,no-agent-forwarding,no-X11-forwarding,no-pty ssh-ed25519 AAAA... deploy@github
```

That prefix is what makes an SSH deploy key acceptable. The key cannot open a shell,
forward a port, or run any other command — only this script. If the GitHub secret ever
leaks, the worst it buys is a redeploy of your own `main`.

```bash
sudo chown -R deploy:deploy /home/deploy/.ssh
sudo chmod 700 /home/deploy/.ssh && sudo chmod 600 /home/deploy/.ssh/authorized_keys
```

### Repository secrets

Settings → Secrets and variables → Actions:

| Secret | Value |
| --- | --- |
| `SSH_HOST` | `srv1515969.hstgr.cloud` |
| `SSH_USER` | `deploy` |
| `SSH_KEY` | the **private** key: `sudo cat /home/deploy/.ssh/deploy_key` |
| `SSH_KNOWN_HOSTS` | `ssh-keyscan -t ed25519 srv1515969.hstgr.cloud` |
| `SSH_PORT` | only if sshd isn't on 22 |

`SSH_KNOWN_HOSTS` is not optional padding. Without it the workflow would need
`StrictHostKeyChecking=no`, which hands a key that can deploy to whatever answers on
port 22.

### Prove it works by hand first

In this order — each step rules out a different failure:

```bash
sudo -u deploy /opt/hebrew-voice/deploy/deploy.sh     # 1. the script itself
ssh -i /home/deploy/.ssh/deploy_key deploy@localhost  # 2. the forced command runs it
                                                      #    instead of giving a shell
```

Then merge something trivial and watch the run in the Actions tab.

### What the script does

Fetches `origin/main` and `git reset --hard`s onto it, backs up the database, rebuilds,
prunes dangling images, and polls `/healthz` for 90 seconds. If the container never
answers it prints the last 50 log lines and exits non-zero, leaving the container running
so you can inspect it — nothing is rolled back automatically.

It deliberately never runs `git clean -xfd` (that would delete `.env` and `data/`, both
gitignored) or `docker compose down -v` (that would destroy the volume — every account
and every recording). A `flock` stops two deploys overlapping.

Useful flags:

```bash
./deploy/deploy.sh --no-pull    # rebuild the current tree without fetching
HV_HEALTH_TIMEOUT=180 ./deploy/deploy.sh   # a slow box
```

### When a run goes red

| Symptom | Cause |
| --- | --- |
| `permission denied` on the docker socket | The `docker` group isn't applied until `deploy` logs in again. `sudo -u deploy -i` or reboot. |
| The SSH step hangs then times out | The VPS firewall is dropping GitHub's runners. Check `sudo ufw status`. |
| `Host key verification failed` | `SSH_KNOWN_HOSTS` is missing, or the host key changed. Re-run `ssh-keyscan`. |
| The script runs but you get a shell instead | The forced command isn't on the same line as the key in `authorized_keys`. |
| `another deploy is already running` | A previous run is still going, or died holding the lock: `rm /tmp/hebrew-voice-deploy.lock`. |
| Healthy locally, red in Actions | The health probe runs inside the container, so this is the app failing to boot. `docker compose logs --tail=100`. |

## Day-to-day

```bash
sudo docker compose logs -f                       # logs
sudo docker compose exec hebrew-voice hebrew-voice user list
sudo docker compose exec hebrew-voice hebrew-voice cleanup --dry-run
sudo docker compose up -d --build                 # upgrade (migrations run at startup)
```

Back up the named volume — it holds the database and every generated file:

```bash
# The runtime image is python:3.12-slim and has no sqlite3 CLI, so use the
# interpreter. .backup() is safe against a live database; `cp` is not.
sudo docker compose exec -T hebrew-voice python -c \
  "import sqlite3; s=sqlite3.connect('file:/data/hebrew-voice.db?mode=ro', uri=True); \
   d=sqlite3.connect('/data/backup.db'); s.backup(d)"
sudo docker cp hebrew-voice:/data/backup.db ./hv-$(date +%F).db
```

Every automatic deploy also takes one of these first, keeping the last five under
`/data/backups/` in the volume.

## If something looks wrong

| Symptom | Cause |
| --- | --- |
| Page loads with no styling | You hit `/voice-gen` without the trailing slash and the `location = /voice-gen` redirect isn't installed. |
| Login says "success" but you stay logged out | `HV_BASE_URL` is `http://` while `HV_SECURE_COOKIES` is on, so the browser drops the cookie. The app refuses to boot in this state — check you actually restarted it. |
| Every action returns 403 | `HV_BASE_URL`'s host doesn't match the hostname you're browsing. The origin check compares hosts. |
| Signup returns 502 `email_send_failed` | The relay rejected the message. `docker compose logs` has the SMTP error — usually a wrong app password, or 2-Step Verification not enabled. The account exists; fix the setting and use the resend link. |
| The mail never arrives | Check spam first. Then confirm the container can reach smtp.gmail.com:587 — some hosts firewall outbound SMTP as well as port 25. |
| Verification link 404s or points at the wrong host | `HV_BASE_URL` is what builds the link. It must be the full public URL including `/voice-gen`. |
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
