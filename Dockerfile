# Two stages: build a virtualenv, then copy it into a clean runtime image.
# No compiler is needed at any point - stdlib scrypt and sqlite3 mean every
# dependency is a pure-Python wheel.
FROM python:3.12-slim AS build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY pyproject.toml README.md ./
COPY hebrew_voice ./hebrew_voice
RUN pip install --no-deps .


FROM python:3.12-slim AS runtime

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HV_DATA_DIR=/data \
    HV_HOST=0.0.0.0 \
    HV_PORT=8080

RUN useradd --system --create-home --uid 10001 appuser \
    && mkdir -p /data && chown appuser:appuser /data

COPY --from=build /opt/venv /opt/venv

USER appuser
VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz')"

# One worker: the concurrency caps and rate limiter are per-process.
CMD ["uvicorn", "hebrew_voice.web.app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
