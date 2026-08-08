"""SQLite access: connections, pragmas, and schema migrations.

Plain :mod:`sqlite3`. Four tables and a couple of dozen queries don't justify
an ORM, and staying dependency-free keeps the Docker image compiler-free.

Every unit of work opens its own connection. That costs microseconds on an
already-created file and sidesteps thread-affinity entirely, which matters
because all of these calls run in a worker thread.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, List, Tuple

__all__ = ["connect", "migrate", "MIGRATIONS"]


MIGRATIONS: List[Tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE users (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            email            TEXT    NOT NULL COLLATE NOCASE UNIQUE,
            password_hash    TEXT    NOT NULL,
            created_at       INTEGER NOT NULL,
            is_active        INTEGER NOT NULL DEFAULT 1,
            is_admin         INTEGER NOT NULL DEFAULT 0,
            invite_code      TEXT,
            daily_char_quota INTEGER,
            failed_logins    INTEGER NOT NULL DEFAULT 0,
            locked_until     INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE sessions (
            id           TEXT    PRIMARY KEY,
            user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            csrf_token   TEXT    NOT NULL,
            created_at   INTEGER NOT NULL,
            last_seen_at INTEGER NOT NULL,
            expires_at   INTEGER NOT NULL,
            user_agent   TEXT,
            ip           TEXT
        );
        CREATE INDEX idx_sessions_user    ON sessions(user_id);
        CREATE INDEX idx_sessions_expires ON sessions(expires_at);

        CREATE TABLE generations (
            id                   TEXT    PRIMARY KEY,
            user_id              INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at           INTEGER NOT NULL,
            title                TEXT    NOT NULL,
            text_raw             TEXT    NOT NULL,
            text_prepared        TEXT    NOT NULL,
            char_count           INTEGER NOT NULL,
            voice                TEXT    NOT NULL,
            rate                 INTEGER NOT NULL DEFAULT 0,
            pitch                INTEGER NOT NULL DEFAULT 0,
            volume               INTEGER NOT NULL DEFAULT 0,
            keep_niqqud          INTEGER NOT NULL DEFAULT 0,
            expand_symbols       INTEGER NOT NULL DEFAULT 1,
            expand_abbreviations INTEGER NOT NULL DEFAULT 1,
            expand_acronyms      INTEGER NOT NULL DEFAULT 1,
            audio_rel            TEXT,
            srt_rel              TEXT,
            vtt_rel              TEXT,
            audio_bytes          INTEGER NOT NULL DEFAULT 0,
            duration_ms          INTEGER NOT NULL DEFAULT 0,
            cue_count            INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX idx_gen_user_time ON generations(user_id, created_at DESC);

        CREATE TABLE usage_daily (
            user_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            day      TEXT    NOT NULL,
            chars    INTEGER NOT NULL DEFAULT 0,
            requests INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, day)
        );
        """,
    ),
]


def _split_statements(sql: str) -> List[str]:
    """Split a migration into individual statements.

    Adequate because migrations here are plain DDL with no semicolons inside
    string literals or trigger bodies; add a real parser if that changes.
    """
    return [stmt.strip() for stmt in sql.split(";") if stmt.strip()]


@contextmanager
def connect(path: Path | str, *, timeout: float = 5.0) -> Iterator[sqlite3.Connection]:
    """Open a connection with the pragmas this app depends on.

    Autocommit mode (``isolation_level=None``); callers that need atomicity
    issue an explicit ``BEGIN IMMEDIATE``.
    """
    conn = sqlite3.connect(str(path), timeout=timeout, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block inside ``BEGIN IMMEDIATE`` ... ``COMMIT``."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def migrate(path: Path | str) -> int:
    """Apply any pending migrations. Returns the resulting schema version."""
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    with connect(file) as conn:
        # Persisted in the database file itself, so this only has to run once.
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version    INTEGER PRIMARY KEY,
                applied_at INTEGER NOT NULL
            )
            """
        )
        applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
        current = max(applied) if applied else 0
        for version, sql in MIGRATIONS:
            if version in applied:
                continue
            with transaction(conn):
                # Statement by statement rather than executescript(), which
                # would commit the open transaction out from under us.
                for statement in _split_statements(sql):
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                    (version, int(time.time())),
                )
            current = version
        return current
