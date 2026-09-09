# Running a branch beside production

Puts a second instance on the same box, at a second URL, so a branch can be
tried against real hardware before it is merged:

| | |
| --- | --- |
| `https://srv1515969.hstgr.cloud/voice-gen/` | production, `main`, deployed by CI on merge |
| `https://srv1515969.hstgr.cloud/voice-gen-branch/` | whatever branch you put there, deployed by hand |

**The two share nothing.** Separate checkout, separate compose project,
separate database and artifacts volume, separate containers, separate port.
That is the whole point: a branch must not be able to touch production's
accounts or recordings, and a bad migration on the branch must not be a bad
migration on production.

CI cannot do this. The deploy key in `authorized_keys` is pinned to
`deploy/deploy.sh` with a forced command, so the runner can only ever deploy
production. A branch instance is deployed by hand, on the box.

---

## 1. A second checkout

```bash
cd ~/projects
git clone https://github.com/andreyshindler/hebrew-voice-generator.git hebrew-voice-branch
cd hebrew-voice-branch
git checkout claude/hyperframes-video-render
```

Keep it beside production, not inside it. `deploy.sh` works out its own
checkout from where it lives, so the copy in this directory deploys this
directory.

## 2. Its own `.env`

Start from production's so the SMTP and invite settings match, then change the
things that must differ:

```bash
cp ~/projects/hebrew-voice-generator/.env .env
```

Edit it. These are the ones that **must** change — everything else can stay:

```ini
# Isolates the compose project: volumes, networks and the DB are all separate.
# Compose reads this from .env natively.
COMPOSE_PROJECT_NAME=hebrew-voice-branch

# Container names are global to the Docker daemon, not to the project, so
# these would otherwise collide with production's and the stack refuses to start.
HV_CONTAINER_NAME=hebrew-voice-branch
HV_RENDER_CONTAINER_NAME=hebrew-voice-branch-renderer

# Same reason for image tags: sharing `hebrew-voice:latest` means each deploy
# silently overwrites the other's build.
HV_IMAGE=hebrew-voice-branch
HV_RENDER_IMAGE=hebrew-voice-branch-renderer

# A free port. Production is on 8095. On srv1515969 the loopback ports already
# taken are 3000, 3001, 4000, 5001, 8000, 8080, 8082, 8091, 8095, 8096, 8431
# and 18788, so 8090 is clear - but that box gains apps, so check first:
#     sudo nginx -T | grep -oE '127\.0\.0\.1:[0-9]+' | sort -u
HV_PUBLISH_PORT=8090

# The branch's own public URL. Getting this wrong is the most likely mistake:
# cookies are scoped to this prefix, so production's value here would put the
# branch's session cookie on /voice-gen and the two would log each other out.
HV_BASE_URL=https://srv1515969.hstgr.cloud/voice-gen-branch
```

With it set correctly the two are isolated by construction: `cookie_path`
derives from the prefix, giving `/voice-gen/` and `/voice-gen-branch/`, and a
browser sends neither cookie to the other app.

A fresh volume means **a fresh database**: no accounts. Sign up again on the
branch instance with an invite code from `HV_INVITE_CODES`. That is deliberate
— it is what keeps a branch away from real users' recordings.

## 3. nginx

Paste both blocks from
[`nginx-branch.conf.example`](nginx-branch.conf.example) into the **same**
`server { ... }` that already has the `/voice-gen/` blocks:

```bash
sudo -e /etc/nginx/sites-available/<the-existing-site>
sudo nginx -t && sudo systemctl reload nginx
```

Order does not matter. nginx takes the longest matching prefix, and
`/voice-gen-branch/…` is not prefixed by `/voice-gen/` at all — the strings
diverge at the `-`. Without the block the catch-all `location /` would send
the branch URL to whatever is on `:5001`, which is the failure to expect if
you forget this step.

The other prefix blocks on that host are safe: `/api/`, `/static/` and the
rest are matched against the *whole* request path, and
`/voice-gen-branch/api/…` does not start with `/api/`.

## 4. Deploy it

```bash
cd ~/projects/hebrew-voice-branch
HV_DEPLOY_BRANCH=claude/hyperframes-video-render ./deploy/deploy.sh
```

The same script production uses. It resets to the named branch, backs up *this
stack's* database, rebuilds, and waits for health. The lock file is derived
from the checkout path, so this never blocks a production deploy.

To move the instance to a different branch later, just change
`HV_DEPLOY_BRANCH`.

## 5. Check it

```bash
curl -sI https://srv1515969.hstgr.cloud/voice-gen-branch/ | head -1
docker ps --format '{{.Names}}\t{{.Status}}\t{{.Ports}}' | grep hebrew-voice
```

You should see production's containers and the branch's, on different ports,
and production still answering at `/voice-gen/`.

---

## What this costs on the box

The branch stack adds a second app container **and a Chromium**. The renderer
is the expensive one: it is a browser and an encoder, and it is idle memory
most of the time. On a small VPS, watch RAM before assuming this is free —
and note that after the video branch merges, production will run a renderer
too, so the steady state is four containers rather than two.

If you want the branch instance without video, set `HV_RENDER_URL=` (empty) in
its `.env` and the app hides the feature entirely. The `hyperframes` service
will still be built and started by `compose up`, so also
`docker compose -p hebrew-voice-branch stop hyperframes` if you want the
memory back.

## Tearing it down

```bash
cd ~/projects/hebrew-voice-branch
docker compose down                 # keeps the volume
docker compose down -v              # DESTROYS the branch's database and files
```

`down -v` here only touches the branch's volume, because the project name
scopes it — but read the command twice anyway, since the same words in
production's directory would destroy every account and recording.
