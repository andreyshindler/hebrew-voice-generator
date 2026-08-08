"""Settings, loaded from the environment (and optionally a .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Mapping, Optional
from urllib.parse import urlsplit

__all__ = ["Settings", "load_dotenv", "get_settings", "normalize_root_path"]

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def load_dotenv(path: os.PathLike | str = ".env") -> Dict[str, str]:
    """Read ``KEY=value`` lines into ``os.environ`` without overwriting it.

    Real environment variables always win, so systemd's ``EnvironmentFile``
    beats a stale ``.env`` left in the working directory. Returns what was
    actually applied.
    """
    file = Path(path)
    applied: Dict[str, str] = {}
    if not file.is_file():
        return applied
    for line in file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
            applied[key] = value
    return applied


def _bool(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = env.get(key)
    if raw is None or raw == "":
        return default
    lowered = raw.strip().lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise ValueError(f"{key} must be a boolean, got {raw!r}")


def _int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{key} must be an integer, got {raw!r}") from exc


def _float(env: Mapping[str, str], key: str, default: float) -> float:
    raw = env.get(key)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{key} must be a number, got {raw!r}") from exc


def normalize_root_path(value: str) -> str:
    """Normalize a URL prefix to ``""`` (root) or ``"/voice-gen"``.

    Accepts the sloppy forms people actually type - ``voice-gen``,
    ``/voice-gen/``, ``/`` - and returns a value that can be concatenated with
    a leading-slash path without producing a double slash.
    """
    cleaned = (value or "").strip()
    if not cleaned or cleaned == "/":
        return ""
    if not cleaned.startswith("/"):
        cleaned = "/" + cleaned
    return cleaned.rstrip("/")


@dataclass(frozen=True)
class Settings:
    """Everything configurable, all of it via ``HV_*`` environment variables."""

    env: str = "development"
    secret_key: str = "dev-insecure"
    data_dir: Path = Path("./data")
    db_path: Path = Path("./data/hebrew-voice.db")

    host: str = "127.0.0.1"
    port: int = 8080
    #: Full public URL, including any subpath: https://host/voice-gen
    base_url: str = ""
    #: URL prefix the app is served under. Derived from base_url when unset.
    root_path: str = ""
    trust_proxy: bool = True

    # Auth
    secure_cookies: bool = True
    session_ttl_days: int = 30
    signup_enabled: bool = True
    invite_codes: List[str] = field(default_factory=list)
    max_failed_logins: int = 10
    lockout_minutes: int = 15

    # Limits
    max_chars: int = 10_000
    daily_char_quota: int = 50_000
    quota_tz: str = "Asia/Jerusalem"
    rate_limit_per_min: int = 6
    rate_limit_burst: int = 3
    max_concurrent_synth: int = 3
    max_concurrent_per_user: int = 1
    max_queue: int = 10
    queue_wait_timeout: float = 20.0
    synth_timeout: float = 180.0
    thread_pool_size: int = 16
    hash_concurrency: int = 4

    # Retention
    history_keep: int = 50
    history_max_age_days: int = 30
    cleanup_interval_min: int = 60

    # Email. No smtp_host means messages are logged instead of sent, which is
    # refused in production when verification is required.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_from_name: str = "מחולל קול עברי"
    smtp_starttls: bool = True
    smtp_timeout: int = 15
    require_email_verification: bool = True
    verify_token_ttl_hours: int = 24

    # Engine
    edge_proxy: Optional[str] = None
    synth_retries: int = 2

    log_level: str = "info"

    # Hashing cost - lowered in tests so the suite isn't dominated by scrypt.
    scrypt_n: int = 1 << 15
    scrypt_r: int = 8
    scrypt_p: int = 1

    @property
    def is_production(self) -> bool:
        return self.env.lower().startswith("prod")

    @property
    def origin(self) -> str:
        """``scheme://host[:port]`` from :attr:`base_url`, with no path.

        A browser's ``Origin`` header never contains a path, so this - not
        ``base_url`` - is what cross-origin checks must compare against.
        """
        if not self.base_url:
            return ""
        parts = urlsplit(self.base_url)
        if not parts.scheme or not parts.netloc:
            return ""
        return f"{parts.scheme}://{parts.netloc}"

    @property
    def email_enabled(self) -> bool:
        """True when a real SMTP relay is configured."""
        return bool(self.smtp_host and self.smtp_from)

    def verification_link(self, token: str) -> str:
        """The absolute URL that goes in the email.

        ``base_url`` already carries the subpath, so the token is appended to
        it directly rather than through :meth:`url`, which would double it.
        """
        return f"{self.base_url}/verify?token={token}"

    @property
    def cookie_path(self) -> str:
        """Scope cookies to the app's prefix, not the whole host.

        Under a subpath this keeps the session cookie from being sent to every
        other app sharing the hostname.
        """
        return f"{self.root_path}/" if self.root_path else "/"

    def url(self, path: str = "/") -> str:
        """Prefix an app-absolute path, e.g. ``/login`` -> ``/voice-gen/login``."""
        if not path.startswith("/"):
            path = "/" + path
        return f"{self.root_path}{path}"

    @property
    def audio_dir(self) -> Path:
        return self.data_dir / "audio"

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "Settings":
        """Build settings from ``HV_*`` variables, applying defaults."""
        env = os.environ if env is None else env
        data_dir = Path(env.get("HV_DATA_DIR") or "./data").expanduser()
        db_path = env.get("HV_DB_PATH") or str(data_dir / "hebrew-voice.db")
        codes = [c.strip() for c in (env.get("HV_INVITE_CODES") or "").split(",") if c.strip()]

        base_url = (env.get("HV_BASE_URL") or "").rstrip("/")
        # Setting HV_BASE_URL=https://host/voice-gen is enough; the prefix is
        # taken from its path unless HV_ROOT_PATH says otherwise.
        root_path = normalize_root_path(
            env.get("HV_ROOT_PATH") if env.get("HV_ROOT_PATH") else urlsplit(base_url).path
        )

        settings = cls(
            env=env.get("HV_ENV") or "development",
            secret_key=env.get("HV_SECRET_KEY") or "dev-insecure",
            data_dir=data_dir,
            db_path=Path(db_path).expanduser(),
            host=env.get("HV_HOST") or "127.0.0.1",
            port=_int(env, "HV_PORT", 8080),
            base_url=base_url,
            root_path=root_path,
            trust_proxy=_bool(env, "HV_TRUST_PROXY", True),
            secure_cookies=_bool(env, "HV_SECURE_COOKIES", True),
            session_ttl_days=_int(env, "HV_SESSION_TTL_DAYS", 30),
            signup_enabled=_bool(env, "HV_SIGNUP_ENABLED", True),
            invite_codes=codes,
            max_failed_logins=_int(env, "HV_MAX_FAILED_LOGINS", 10),
            lockout_minutes=_int(env, "HV_LOCKOUT_MINUTES", 15),
            max_chars=_int(env, "HV_MAX_CHARS", 10_000),
            daily_char_quota=_int(env, "HV_DAILY_CHAR_QUOTA", 50_000),
            quota_tz=env.get("HV_QUOTA_TZ") or "Asia/Jerusalem",
            rate_limit_per_min=_int(env, "HV_RATE_LIMIT_PER_MIN", 6),
            rate_limit_burst=_int(env, "HV_RATE_LIMIT_BURST", 3),
            max_concurrent_synth=_int(env, "HV_MAX_CONCURRENT_SYNTH", 3),
            max_concurrent_per_user=_int(env, "HV_MAX_CONCURRENT_PER_USER", 1),
            max_queue=_int(env, "HV_MAX_QUEUE", 10),
            queue_wait_timeout=_float(env, "HV_QUEUE_WAIT_TIMEOUT", 20.0),
            synth_timeout=_float(env, "HV_SYNTH_TIMEOUT", 180.0),
            thread_pool_size=_int(env, "HV_THREAD_POOL_SIZE", 16),
            hash_concurrency=_int(env, "HV_HASH_CONCURRENCY", 4),
            history_keep=_int(env, "HV_HISTORY_KEEP", 50),
            history_max_age_days=_int(env, "HV_HISTORY_MAX_AGE_DAYS", 30),
            cleanup_interval_min=_int(env, "HV_CLEANUP_INTERVAL_MIN", 60),
            smtp_host=env.get("HV_SMTP_HOST") or "",
            smtp_port=_int(env, "HV_SMTP_PORT", 587),
            smtp_user=env.get("HV_SMTP_USER") or "",
            smtp_password=env.get("HV_SMTP_PASSWORD") or "",
            # Gmail rewrites From to the authenticated account anyway, so the
            # sensible default is the login itself.
            smtp_from=env.get("HV_SMTP_FROM") or env.get("HV_SMTP_USER") or "",
            smtp_from_name=env.get("HV_SMTP_FROM_NAME") or "מחולל קול עברי",
            smtp_starttls=_bool(env, "HV_SMTP_STARTTLS", True),
            smtp_timeout=_int(env, "HV_SMTP_TIMEOUT", 15),
            require_email_verification=_bool(env, "HV_REQUIRE_EMAIL_VERIFICATION", True),
            verify_token_ttl_hours=_int(env, "HV_VERIFY_TOKEN_TTL_HOURS", 24),
            edge_proxy=env.get("HV_EDGE_PROXY") or None,
            synth_retries=_int(env, "HV_SYNTH_RETRIES", 2),
            log_level=(env.get("HV_LOG_LEVEL") or "info").lower(),
            scrypt_n=_int(env, "HV_SCRYPT_N", 1 << 15),
            scrypt_r=_int(env, "HV_SCRYPT_R", 8),
            scrypt_p=_int(env, "HV_SCRYPT_P", 1),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        """Refuse to boot with a configuration that is unsafe in production."""
        problems: List[str] = []
        if self.root_path and not self.root_path.startswith("/"):
            problems.append("HV_ROOT_PATH must start with '/'")
        if self.base_url and not self.origin:
            problems.append(
                f"HV_BASE_URL must be a full URL like https://host/prefix, got {self.base_url!r}"
            )
        if self.is_production:
            if self.secret_key in ("", "dev-insecure"):
                problems.append("HV_SECRET_KEY must be set in production")
            if not self.secure_cookies:
                problems.append("HV_SECURE_COOKIES must stay true in production")
            if self.signup_enabled and not self.invite_codes:
                problems.append(
                    "HV_INVITE_CODES is empty while signup is enabled - "
                    "anyone could register; set codes or HV_SIGNUP_ENABLED=false"
                )
            if not self.base_url:
                problems.append("HV_BASE_URL must be set in production (origin checks)")
            if self.require_email_verification and not self.base_url:
                problems.append(
                    "HV_BASE_URL is required to build verification links "
                    "(or set HV_REQUIRE_EMAIL_VERIFICATION=false)"
                )
            if self.require_email_verification and not self.email_enabled:
                # Without a relay, signup would create accounts that can never
                # be activated - a service nobody can join.
                problems.append(
                    "HV_SMTP_HOST and HV_SMTP_FROM must be set when "
                    "HV_REQUIRE_EMAIL_VERIFICATION is on, or nobody could confirm "
                    "an account"
                )
            if self.base_url and self.secure_cookies and not self.base_url.startswith("https://"):
                # Secure cookies are never sent over http, so the user would
                # log in successfully and then appear logged out.
                problems.append(
                    "HV_BASE_URL must be https when HV_SECURE_COOKIES is on, "
                    f"got {self.base_url!r}"
                )
        if not 1 <= self.smtp_port <= 65535:
            problems.append(f"HV_SMTP_PORT must be a valid port, got {self.smtp_port}")
        if self.max_chars < 1:
            problems.append("HV_MAX_CHARS must be positive")
        if self.max_concurrent_synth < 1:
            problems.append("HV_MAX_CONCURRENT_SYNTH must be at least 1")
        if problems:
            raise ValueError("invalid configuration: " + "; ".join(problems))


_cached: Optional[Settings] = None


def get_settings(*, refresh: bool = False) -> Settings:
    """Process-wide settings, built from the environment on first use."""
    global _cached
    if _cached is None or refresh:
        _cached = Settings.from_env()
    return _cached
