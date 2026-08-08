"""All SQL lives here. Functions are synchronous; callers offload to a thread.

Every function takes the database path and opens its own short-lived
connection, so there is no shared state and no thread affinity to worry about.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from .db import connect, transaction
from .models import Generation, Session, User

__all__ = [
    "create_user",
    "get_user_by_email",
    "get_user",
    "list_users",
    "set_password",
    "set_active",
    "note_failed_login",
    "clear_failed_logins",
    "create_session",
    "get_session_with_user",
    "touch_session",
    "delete_session",
    "delete_user_sessions",
    "purge_expired_sessions",
    "insert_generation",
    "get_generation",
    "list_generations",
    "delete_generation",
    "count_generations",
    "reserve_quota",
    "refund_quota",
    "usage_today",
    "expired_generations",
]


class EmailTaken(Exception):
    """Raised when an email is already registered."""


# --------------------------------------------------------------------------
# Users
# --------------------------------------------------------------------------


def create_user(
    db: Path,
    *,
    email: str,
    password_hash: str,
    is_admin: bool = False,
    invite_code: Optional[str] = None,
    daily_char_quota: Optional[int] = None,
) -> User:
    """Insert a user. The first account created is automatically an admin."""
    now = int(time.time())
    with connect(db) as conn:
        try:
            with transaction(conn):
                existing = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
                admin = 1 if (is_admin or existing == 0) else 0
                cur = conn.execute(
                    """
                    INSERT INTO users
                        (email, password_hash, created_at, is_active, is_admin,
                         invite_code, daily_char_quota)
                    VALUES (?, ?, ?, 1, ?, ?, ?)
                    """,
                    (email.strip(), password_hash, now, admin, invite_code, daily_char_quota),
                )
                user_id = cur.lastrowid
        except sqlite3.IntegrityError as exc:
            raise EmailTaken(email) from exc
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return User.from_row(row)


def get_user_by_email(db: Path, email: str) -> Optional[User]:
    with connect(db) as conn:
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email.strip(),)).fetchone()
    return User.from_row(row) if row else None


def get_user(db: Path, user_id: int) -> Optional[User]:
    with connect(db) as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return User.from_row(row) if row else None


def list_users(db: Path) -> List[User]:
    with connect(db) as conn:
        rows = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
    return [User.from_row(r) for r in rows]


def set_password(db: Path, user_id: int, password_hash: str) -> None:
    with connect(db) as conn:
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id)
        )


def set_active(db: Path, user_id: int, active: bool) -> None:
    with connect(db) as conn:
        conn.execute("UPDATE users SET is_active = ? WHERE id = ?", (1 if active else 0, user_id))
        if not active:
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


def note_failed_login(db: Path, user_id: int, *, max_failures: int, lockout_seconds: int) -> int:
    """Count a failed attempt and lock the account once the limit is hit.

    Returns the epoch second the lock expires, or 0 when not locked.
    """
    now = int(time.time())
    with connect(db) as conn:
        with transaction(conn):
            conn.execute(
                "UPDATE users SET failed_logins = failed_logins + 1 WHERE id = ?", (user_id,)
            )
            failures = conn.execute(
                "SELECT failed_logins FROM users WHERE id = ?", (user_id,)
            ).fetchone()[0]
            locked_until = 0
            if failures >= max_failures:
                locked_until = now + lockout_seconds
                conn.execute(
                    "UPDATE users SET locked_until = ?, failed_logins = 0 WHERE id = ?",
                    (locked_until, user_id),
                )
    return locked_until


def clear_failed_logins(db: Path, user_id: int) -> None:
    with connect(db) as conn:
        conn.execute(
            "UPDATE users SET failed_logins = 0, locked_until = 0 WHERE id = ?", (user_id,)
        )


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------


def create_session(
    db: Path,
    *,
    token_hash: str,
    user_id: int,
    csrf_token: str,
    ttl_seconds: int,
    user_agent: Optional[str] = None,
    ip: Optional[str] = None,
) -> Session:
    now = int(time.time())
    with connect(db) as conn:
        conn.execute(
            """
            INSERT INTO sessions
                (id, user_id, csrf_token, created_at, last_seen_at, expires_at, user_agent, ip)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (token_hash, user_id, csrf_token, now, now, now + ttl_seconds, user_agent, ip),
        )
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (token_hash,)).fetchone()
    return Session.from_row(row)


def get_session_with_user(db: Path, token_hash: str) -> Optional[Tuple[Session, User]]:
    """Look up a live session and its user in one query."""
    now = int(time.time())
    with connect(db) as conn:
        row = conn.execute(
            """
            SELECT s.id AS s_id, s.user_id AS s_user_id, s.csrf_token AS s_csrf,
                   s.created_at AS s_created, s.last_seen_at AS s_seen,
                   s.expires_at AS s_expires, s.user_agent AS s_ua, s.ip AS s_ip,
                   u.*
            FROM sessions s JOIN users u ON u.id = s.user_id
            WHERE s.id = ? AND s.expires_at > ? AND u.is_active = 1
            """,
            (token_hash, now),
        ).fetchone()
    if not row:
        return None
    session = Session(
        id=row["s_id"],
        user_id=row["s_user_id"],
        csrf_token=row["s_csrf"],
        created_at=row["s_created"],
        last_seen_at=row["s_seen"],
        expires_at=row["s_expires"],
        user_agent=row["s_ua"],
        ip=row["s_ip"],
    )
    return session, User.from_row(row)


def touch_session(db: Path, token_hash: str, *, ttl_seconds: int) -> None:
    """Slide the expiry forward on an active session."""
    now = int(time.time())
    with connect(db) as conn:
        conn.execute(
            "UPDATE sessions SET last_seen_at = ?, expires_at = ? WHERE id = ?",
            (now, now + ttl_seconds, token_hash),
        )


def delete_session(db: Path, token_hash: str) -> None:
    with connect(db) as conn:
        conn.execute("DELETE FROM sessions WHERE id = ?", (token_hash,))


def delete_user_sessions(db: Path, user_id: int) -> None:
    with connect(db) as conn:
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


def purge_expired_sessions(db: Path) -> int:
    with connect(db) as conn:
        cur = conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (int(time.time()),))
        return cur.rowcount or 0


# --------------------------------------------------------------------------
# Generations
# --------------------------------------------------------------------------


def insert_generation(db: Path, gen: Generation) -> None:
    with connect(db) as conn:
        conn.execute(
            """
            INSERT INTO generations
                (id, user_id, created_at, title, text_raw, text_prepared, char_count,
                 voice, rate, pitch, volume, keep_niqqud, expand_symbols,
                 expand_abbreviations, expand_acronyms, audio_rel, srt_rel, vtt_rel,
                 audio_bytes, duration_ms, cue_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                gen.id, gen.user_id, gen.created_at, gen.title, gen.text_raw,
                gen.text_prepared, gen.char_count, gen.voice, gen.rate, gen.pitch,
                gen.volume, int(gen.keep_niqqud), int(gen.expand_symbols),
                int(gen.expand_abbreviations), int(gen.expand_acronyms),
                gen.audio_rel, gen.srt_rel, gen.vtt_rel, gen.audio_bytes,
                gen.duration_ms, gen.cue_count,
            ),
        )


def get_generation(db: Path, gen_id: str, user_id: int) -> Optional[Generation]:
    """Fetch a generation, scoped to its owner.

    Ownership is part of the query rather than a check afterwards, so another
    user's id simply doesn't exist as far as the caller is concerned.
    """
    with connect(db) as conn:
        row = conn.execute(
            "SELECT * FROM generations WHERE id = ? AND user_id = ?", (gen_id, user_id)
        ).fetchone()
    return Generation.from_row(row) if row else None


def list_generations(
    db: Path, user_id: int, *, limit: int = 20, before: Optional[int] = None
) -> List[Generation]:
    """Newest first, keyset-paginated on ``created_at``.

    Ties break on ``rowid`` - insertion order - because ``created_at`` only has
    one-second resolution and ``id`` is random hex, so two generations made in
    the same second would otherwise come back in arbitrary order.
    """
    sql = "SELECT * FROM generations WHERE user_id = ?"
    params: List[object] = [user_id]
    if before is not None:
        sql += " AND created_at < ?"
        params.append(before)
    sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
    params.append(limit)
    with connect(db) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [Generation.from_row(r) for r in rows]


def count_generations(db: Path, user_id: int) -> int:
    with connect(db) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM generations WHERE user_id = ?", (user_id,)
        ).fetchone()[0]


def delete_generation(db: Path, gen_id: str, user_id: Optional[int] = None) -> bool:
    sql = "DELETE FROM generations WHERE id = ?"
    params: List[object] = [gen_id]
    if user_id is not None:
        sql += " AND user_id = ?"
        params.append(user_id)
    with connect(db) as conn:
        cur = conn.execute(sql, params)
        return bool(cur.rowcount)


def expired_generations(
    db: Path, *, keep_per_user: int, max_age_days: int
) -> List[Generation]:
    """Rows the retention policy says should go: too old, or past the keep-N."""
    cutoff = int(time.time()) - max_age_days * 86400
    with connect(db) as conn:
        old = conn.execute(
            "SELECT * FROM generations WHERE created_at < ?", (cutoff,)
        ).fetchall()
        surplus = conn.execute(
            """
            SELECT * FROM (
                SELECT g.*, ROW_NUMBER() OVER (
                    PARTITION BY user_id ORDER BY created_at DESC, rowid DESC
                ) AS rn
                FROM generations g
            ) WHERE rn > ?
            """,
            (keep_per_user,),
        ).fetchall()
    seen = set()
    result: List[Generation] = []
    for row in list(old) + list(surplus):
        if row["id"] in seen:
            continue
        seen.add(row["id"])
        result.append(Generation.from_row(row))
    return result


# --------------------------------------------------------------------------
# Quota
# --------------------------------------------------------------------------


def reserve_quota(db: Path, user_id: int, day: str, chars: int, limit: int) -> Tuple[bool, int]:
    """Atomically claim ``chars`` of today's allowance.

    Returns ``(granted, used_after)``. The read and the write happen inside one
    ``BEGIN IMMEDIATE``, so two concurrent requests can't both slip past the
    limit.
    """
    with connect(db) as conn:
        with transaction(conn):
            row = conn.execute(
                "SELECT chars FROM usage_daily WHERE user_id = ? AND day = ?", (user_id, day)
            ).fetchone()
            used = row[0] if row else 0
            if used + chars > limit:
                return False, used
            conn.execute(
                """
                INSERT INTO usage_daily (user_id, day, chars, requests)
                VALUES (?, ?, ?, 1)
                ON CONFLICT(user_id, day) DO UPDATE
                    SET chars = chars + excluded.chars, requests = requests + 1
                """,
                (user_id, day, chars),
            )
            return True, used + chars


def refund_quota(db: Path, user_id: int, day: str, chars: int) -> None:
    """Give back a reservation when the synthesis failed."""
    with connect(db) as conn:
        conn.execute(
            """
            UPDATE usage_daily
               SET chars = MAX(0, chars - ?), requests = MAX(0, requests - 1)
             WHERE user_id = ? AND day = ?
            """,
            (chars, user_id, day),
        )


def usage_today(db: Path, user_id: int, day: str) -> int:
    with connect(db) as conn:
        row = conn.execute(
            "SELECT chars FROM usage_daily WHERE user_id = ? AND day = ?", (user_id, day)
        ).fetchone()
    return row[0] if row else 0
