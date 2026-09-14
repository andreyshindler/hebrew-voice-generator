# The HyperFrames render sidecar.
#
# HeyGen publishes no image, so this is ours to build and ours to keep patched
# - it carries a Chromium, which is the one part of this deployment with a
# steady stream of CVEs.
#
# It is deliberately a separate image from the app: the app stays pure-Python
# with no compiler and no browser, and a render competing for CPU cannot stall
# the single uvicorn worker.
FROM node:22-bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive

# chromium and ffmpeg are the engine. fonts-noto-core carries Noto Sans
# Hebrew: without a Hebrew face Chromium draws empty boxes and the render
# "succeeds" with unreadable captions, which is the worst possible failure
# because nothing errors.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        chromium \
        ffmpeg \
        fonts-noto-core \
        fonts-noto-color-emoji \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Puppeteer must use the distro Chromium rather than downloading its own,
# which would both bloat the image and un-pin the version.
ENV PUPPETEER_SKIP_DOWNLOAD=1 \
    PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium \
    CHROME_PATH=/usr/bin/chromium \
    NODE_ENV=production

WORKDIR /srv

# Pinned on purpose: HyperFrames' selling point is deterministic output, which
# a floating version would quietly undo.
COPY package.json ./
RUN npm install --omit=dev && npm cache clean --force

COPY server.js ./

# The renderer is reachable only on the compose network - it is never
# published to the host, because it renders HTML in a browser.
EXPOSE 8080

# Chromium needs a real /dev/shm; compose sets shm_size for this.
#
# The uid must match the app's appuser. Both containers share the artifacts
# volume, and the app creates /data as 0700 owned by 10001 - so any other uid
# cannot even traverse it. The node image's own `node` user is 1000, and using
# it makes every render fail with "Project directory not found", because
# existsSync() cannot tell a directory it may not enter from one that is not
# there. Keep this in step with the uid in the app's Dockerfile.
RUN useradd --system --create-home --uid 10001 renderer
USER renderer

CMD ["node", "server.js"]
