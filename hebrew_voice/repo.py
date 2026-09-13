"""All SQL lives here. Functions are synchronous; callers offload to a thread.

Every function takes the database path and opens its own short-lived
connection, so there is no shared state and no thread affinity to worry about.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from .db import connect, transaction
from .models import Generation, Media, Render, Session, Transcription, User

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
    "create_email_token",
    "consume_email_token",
    "mark_email_verified",
    "purge_expired_tokens",
    "VERIFY_EMAIL",
    "insert_generation",
    "get_generation",
    "list_generations",
    "delete_generation",
    "count_generations",
    "reserve_quota",
    "refund_quota",
    "usage_today",
    "expired_generations",
    "insert_render",
    "get_render",
    "renders_for_generation",
    "render_video_paths",
    "find_reusable_render",
    "claim_next_render",
    "finish_render",
    "fail_render",
    "requeue_or_fail_running",
    "reserve_render_quota",
    "refund_render_quota",
    "renders_today",
    "insert_media",
    "get_media",
    "get_media_many",
    "list_media",
    "delete_media",
    "media_bytes_used",
    "media_paths_for_user",
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
    verified: bool = False,
) -> User:
    """Insert a user. The first account created is automatically an admin.

    ``verified`` skips email confirmation - used by the CLI, where an admin at
    a shell has already vouched for the address.
    """
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
                         invite_code, daily_char_quota, email_verified_at)
                    VALUES (?, ?, ?, 1, ?, ?, ?, ?)
                    """,
                    (
                        email.strip(), password_hash, now, admin, invite_code,
                        daily_char_quota, now if verified else 0,
                    ),
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
# Email verification
# --------------------------------------------------------------------------

VERIFY_EMAIL = "verify_email"


def create_email_token(
    db: Path, *, user_id: int, purpose: str, ttl_seconds: int
) -> str:
    """Mint a token for an emailed link and return the *raw* value.

    Only the SHA-256 is stored, so a database leak yields no usable links.
    Outstanding tokens of the same purpose are dropped first, so a resend
    retires the previous email.
    """
    from .security import hash_session_token, new_email_token

    raw = new_email_token()
    now = int(time.time())
    with connect(db) as conn:
        with transaction(conn):
            conn.execute(
                "DELETE FROM email_tokens WHERE user_id = ? AND purpose = ?",
                (user_id, purpose),
            )
            conn.execute(
                """
                INSERT INTO email_tokens
                    (id, user_id, purpose, created_at, expires_at, used_at)
                VALUES (?, ?, ?, ?, ?, 0)
                """,
                (hash_session_token(raw), user_id, purpose, now, now + ttl_seconds),
            )
    return raw


def consume_email_token(db: Path, token_hash: str, purpose: str) -> Optional[int]:
    """Spend a token, returning its user id, or ``None`` if it isn't usable.

    The lookup and the "mark used" happen in one ``BEGIN IMMEDIATE`` so two
    concurrent clicks can't both succeed.
    """
    now = int(time.time())
    with connect(db) as conn:
        with transaction(conn):
            row = conn.execute(
                """
                SELECT user_id FROM email_tokens
                 WHERE id = ? AND purpose = ? AND used_at = 0 AND expires_at > ?
                """,
                (token_hash, purpose, now),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE email_tokens SET used_at = ? WHERE id = ?", (now, token_hash)
            )
            return row["user_id"]


def mark_email_verified(db: Path, user_id: int) -> None:
    with connect(db) as conn:
        conn.execute(
            "UPDATE users SET email_verified_at = ? WHERE id = ?",
            (int(time.time()), user_id),
        )


def purge_expired_tokens(db: Path) -> int:
    """Drop spent and expired links so the table doesn't grow forever."""
    with connect(db) as conn:
        cur = conn.execute(
            "DELETE FROM email_tokens WHERE expires_at <= ? OR used_at > 0",
            (int(time.time()),),
        )
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
                 cues_rel, words_per_cue, audio_bytes, duration_ms, cue_count, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                gen.id, gen.user_id, gen.created_at, gen.title, gen.text_raw,
                gen.text_prepared, gen.char_count, gen.voice, gen.rate, gen.pitch,
                gen.volume, int(gen.keep_niqqud), int(gen.expand_symbols),
                int(gen.expand_abbreviations), int(gen.expand_acronyms),
                gen.audio_rel, gen.srt_rel, gen.vtt_rel, gen.cues_rel,
                gen.words_per_cue, gen.audio_bytes, gen.duration_ms, gen.cue_count,
                gen.source,
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


# --------------------------------------------------------------------------
# Renders
# --------------------------------------------------------------------------


def insert_render(db: Path, render: Render) -> None:
    with connect(db) as conn:
        conn.execute(
            """
            INSERT INTO renders
                (id, generation_id, user_id, created_at, started_at, finished_at,
                 status, error, format, words_per_cue, width, height, fps,
                 video_rel, video_bytes, media_ids, plan)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                render.id, render.generation_id, render.user_id, render.created_at,
                render.started_at, render.finished_at, render.status, render.error,
                render.format, render.words_per_cue, render.width, render.height,
                render.fps, render.video_rel, render.video_bytes,
                json.dumps(list(render.media_ids)),
                json.dumps(render.plan, sort_keys=True, separators=(",", ":")),
            ),
        )


def get_render(db: Path, render_id: str, user_id: Optional[int] = None) -> Optional[Render]:
    """Fetch a render, scoped to its owner when ``user_id`` is given."""
    sql = "SELECT * FROM renders WHERE id = ?"
    params: List[object] = [render_id]
    if user_id is not None:
        sql += " AND user_id = ?"
        params.append(user_id)
    with connect(db) as conn:
        row = conn.execute(sql, params).fetchone()
    return Render.from_row(row) if row else None


def renders_for_generation(db: Path, gen_id: str) -> List[Render]:
    with connect(db) as conn:
        rows = conn.execute(
            "SELECT * FROM renders WHERE generation_id = ? ORDER BY created_at DESC", (gen_id,)
        ).fetchall()
    return [Render.from_row(row) for row in rows]


def render_video_paths(db: Path, gen_ids: Sequence[str]) -> List[str]:
    """Every stored video file for these generations.

    Deleting a generation cascades its render *rows* away, but the files on
    disk are ours to unlink - so callers collect the paths with this first and
    delete them after.
    """
    if not gen_ids:
        return []
    placeholders = ",".join("?" for _ in gen_ids)
    with connect(db) as conn:
        rows = conn.execute(
            f"SELECT video_rel FROM renders "
            f"WHERE generation_id IN ({placeholders}) AND video_rel IS NOT NULL",
            list(gen_ids),
        ).fetchall()
    return [row[0] for row in rows]


def find_reusable_render(
    db: Path,
    gen_id: str,
    *,
    fmt: str,
    words_per_cue: int,
    width: int,
    height: int,
    fps: int,
    media_ids: str = "[]",
    plan: str = "{}",
) -> Optional[Render]:
    """A finished render with identical parameters, if one exists.

    Re-rendering the same thing costs minutes of CPU for a byte-identical
    file, so the request handler hands back the old one instead.
    """
    with connect(db) as conn:
        row = conn.execute(
            """
            SELECT * FROM renders
             WHERE generation_id = ? AND status = 'done' AND video_rel IS NOT NULL
               AND format = ? AND words_per_cue = ? AND width = ? AND height = ? AND fps = ?
               AND media_ids = ? AND plan = ?
             ORDER BY created_at DESC LIMIT 1
            """,
            (gen_id, fmt, words_per_cue, width, height, fps, media_ids, plan),
        ).fetchone()
    return Render.from_row(row) if row else None


def claim_next_render(db: Path) -> Optional[Render]:
    """Take the oldest queued render and mark it running, atomically.

    The UPDATE ... WHERE status = 'queued' inside the transaction is what makes
    this safe: if anything else claimed the row first the rowcount is zero and
    we look again.
    """
    now = int(time.time())
    with connect(db) as conn:
        with transaction(conn):
            row = conn.execute(
                "SELECT * FROM renders WHERE status = 'queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            cur = conn.execute(
                "UPDATE renders SET status = 'running', started_at = ? "
                " WHERE id = ? AND status = 'queued'",
                (now, row["id"]),
            )
            if not cur.rowcount:
                return None
            claimed = conn.execute("SELECT * FROM renders WHERE id = ?", (row["id"],)).fetchone()
    return Render.from_row(claimed) if claimed else None


def finish_render(db: Path, render_id: str, *, video_rel: str, video_bytes: int) -> None:
    with connect(db) as conn:
        conn.execute(
            """
            UPDATE renders
               SET status = 'done', finished_at = ?, video_rel = ?, video_bytes = ?, error = NULL
             WHERE id = ?
            """,
            (int(time.time()), video_rel, video_bytes, render_id),
        )


def fail_render(db: Path, render_id: str, error: str) -> None:
    with connect(db) as conn:
        conn.execute(
            "UPDATE renders SET status = 'failed', finished_at = ?, error = ? WHERE id = ?",
            (int(time.time()), error[:500], render_id),
        )


def requeue_or_fail_running(db: Path, error: str) -> int:
    """Clear out renders left running by a process that went away.

    The container restarts on every deploy, and a row stuck in ``running`` has
    no worker behind it - nothing would ever move it again. They are failed
    rather than requeued on purpose: a job that killed the renderer would
    otherwise come back and kill it again on every boot.
    """
    with connect(db) as conn:
        cur = conn.execute(
            "UPDATE renders SET status = 'failed', finished_at = ?, error = ? "
            " WHERE status = 'running'",
            (int(time.time()), error),
        )
        return cur.rowcount or 0


# --------------------------------------------------------------------------
# Transcriptions
# --------------------------------------------------------------------------


def insert_transcription(db: Path, job: Transcription) -> None:
    with connect(db) as conn:
        conn.execute(
            """
            INSERT INTO transcriptions
                (id, user_id, media_id, generation_id, created_at, started_at,
                 finished_at, status, error, seconds)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job.id, job.user_id, job.media_id, job.generation_id, job.created_at,
                job.started_at, job.finished_at, job.status, job.error, job.seconds,
            ),
        )


def get_transcription(db: Path, job_id: str, user_id: int) -> Optional[Transcription]:
    """Fetch a job, scoped to its owner - ownership is part of the query."""
    with connect(db) as conn:
        row = conn.execute(
            "SELECT * FROM transcriptions WHERE id = ? AND user_id = ?", (job_id, user_id)
        ).fetchone()
    return Transcription.from_row(row) if row else None


def has_active_transcription(db: Path, user_id: int) -> bool:
    """Whether this account already has one in flight.

    One at a time per account: the queue is shared and a single user should not
    be able to fill it.
    """
    with connect(db) as conn:
        row = conn.execute(
            "SELECT 1 FROM transcriptions "
            " WHERE user_id = ? AND status IN ('queued', 'running') LIMIT 1",
            (user_id,),
        ).fetchone()
    return row is not None


def claim_next_transcription(db: Path) -> Optional[Transcription]:
    """Take the oldest queued job and mark it running, atomically.

    Mirrors :func:`claim_next_render`, including why the conditional UPDATE is
    what makes it safe.
    """
    now = int(time.time())
    with connect(db) as conn:
        with transaction(conn):
            row = conn.execute(
                "SELECT * FROM transcriptions WHERE status = 'queued' "
                " ORDER BY created_at LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            cur = conn.execute(
                "UPDATE transcriptions SET status = 'running', started_at = ? "
                " WHERE id = ? AND status = 'queued'",
                (now, row["id"]),
            )
            if not cur.rowcount:
                return None
            claimed = conn.execute(
                "SELECT * FROM transcriptions WHERE id = ?", (row["id"],)
            ).fetchone()
    return Transcription.from_row(claimed) if claimed else None


def finish_transcription(db: Path, job_id: str, *, generation_id: str, seconds: float) -> None:
    """Attach the recording the job produced, and record what it really cost."""
    with connect(db) as conn:
        conn.execute(
            """
            UPDATE transcriptions
               SET status = 'done', finished_at = ?, generation_id = ?, seconds = ?,
                   error = NULL
             WHERE id = ?
            """,
            (int(time.time()), generation_id, seconds, job_id),
        )


def fail_transcription(db: Path, job_id: str, error: str) -> None:
    with connect(db) as conn:
        conn.execute(
            "UPDATE transcriptions SET status = 'failed', finished_at = ?, error = ? "
            " WHERE id = ?",
            (int(time.time()), error[:500], job_id),
        )


def fail_running_transcriptions(db: Path, error: str) -> int:
    """Clear out jobs left running by a process that went away.

    Failed rather than requeued, for the same reason as renders: a recording
    that killed the worker would otherwise come back every boot.
    """
    with connect(db) as conn:
        cur = conn.execute(
            "UPDATE transcriptions SET status = 'failed', finished_at = ?, error = ? "
            " WHERE status = 'running'",
            (int(time.time()), error),
        )
        return cur.rowcount or 0


def reserve_transcription_quota(
    db: Path, user_id: int, day: str, seconds: int, limit: int
) -> Tuple[bool, int]:
    """Atomically claim seconds of today's transcription allowance.

    The caller only knows what the browser measured, so this is a reservation
    against an estimate. :func:`settle_transcription_quota` corrects it once
    the provider says how long the recording really was.
    """
    with connect(db) as conn:
        with transaction(conn):
            row = conn.execute(
                "SELECT transcribed_seconds FROM usage_daily WHERE user_id = ? AND day = ?",
                (user_id, day),
            ).fetchone()
            used = row[0] if row else 0
            if used + seconds > limit:
                return False, used
            conn.execute(
                """
                INSERT INTO usage_daily
                    (user_id, day, chars, requests, renders, transcribed_seconds)
                VALUES (?, ?, 0, 0, 0, ?)
                ON CONFLICT(user_id, day) DO UPDATE
                    SET transcribed_seconds = transcribed_seconds + ?
                """,
                (user_id, day, seconds, seconds),
            )
            return True, used + seconds


def refund_transcription_quota(db: Path, user_id: int, day: str, seconds: int) -> None:
    """Give the allowance back when the job produced no subtitles."""
    with connect(db) as conn:
        conn.execute(
            "UPDATE usage_daily "
            "   SET transcribed_seconds = MAX(0, transcribed_seconds - ?) "
            " WHERE user_id = ? AND day = ?",
            (seconds, user_id, day),
        )


def settle_transcription_quota(db: Path, user_id: int, day: str, delta: int) -> None:
    """Correct the reservation once the real duration is known.

    ``delta`` is signed: the browser's estimate is advice, and a recording that
    turned out longer should still be charged for what it was.
    """
    if not delta:
        return
    with connect(db) as conn:
        conn.execute(
            "UPDATE usage_daily "
            "   SET transcribed_seconds = MAX(0, transcribed_seconds + ?) "
            " WHERE user_id = ? AND day = ?",
            (delta, user_id, day),
        )


# --------------------------------------------------------------------------
# Media
# --------------------------------------------------------------------------


def insert_media(db: Path, item: Media) -> None:
    with connect(db) as conn:
        conn.execute(
            """
            INSERT INTO media
                (id, user_id, created_at, kind, mime, rel, bytes, duration_ms,
                 original_name)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item.id, item.user_id, item.created_at, item.kind, item.mime,
                item.rel, item.bytes, item.duration_ms, item.original_name,
            ),
        )


def get_media(db: Path, media_id: str, user_id: int) -> Optional[Media]:
    """Fetch one upload, scoped to its owner."""
    with connect(db) as conn:
        row = conn.execute(
            "SELECT * FROM media WHERE id = ? AND user_id = ?", (media_id, user_id)
        ).fetchone()
    return Media.from_row(row) if row else None


def get_media_many(db: Path, media_ids: Sequence[str], user_id: int) -> List[Media]:
    """Fetch several uploads, **in the order asked for**.

    Order is the whole point - it is the running order of the finished video -
    and SQL will not preserve it, so the rows are reordered here. Ids that do
    not exist or belong to someone else are simply absent, which the caller
    checks by counting.
    """
    if not media_ids:
        return []
    placeholders = ",".join("?" for _ in media_ids)
    with connect(db) as conn:
        rows = conn.execute(
            f"SELECT * FROM media WHERE user_id = ? AND id IN ({placeholders})",
            [user_id, *media_ids],
        ).fetchall()
    by_id = {row["id"]: Media.from_row(row) for row in rows}
    return [by_id[mid] for mid in media_ids if mid in by_id]


def list_media(db: Path, user_id: int, limit: int = 100) -> List[Media]:
    with connect(db) as conn:
        rows = conn.execute(
            "SELECT * FROM media WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    return [Media.from_row(row) for row in rows]


def delete_media(db: Path, media_id: str, user_id: int) -> Optional[str]:
    """Delete one upload, returning its path so the file can go too."""
    with connect(db) as conn:
        with transaction(conn):
            row = conn.execute(
                "SELECT rel FROM media WHERE id = ? AND user_id = ?", (media_id, user_id)
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "DELETE FROM media WHERE id = ? AND user_id = ?", (media_id, user_id)
            )
            return row[0]


def media_bytes_used(db: Path, user_id: int) -> int:
    with connect(db) as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(bytes), 0) FROM media WHERE user_id = ?", (user_id,)
        ).fetchone()
    return row[0] or 0


def media_paths_for_user(db: Path, user_id: int) -> List[str]:
    """Every stored upload path for a user, for wholesale cleanup."""
    with connect(db) as conn:
        rows = conn.execute(
            "SELECT rel FROM media WHERE user_id = ?", (user_id,)
        ).fetchall()
    return [row[0] for row in rows]


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


def reserve_render_quota(db: Path, user_id: int, day: str, limit: int) -> Tuple[bool, int]:
    """Atomically claim one of today's renders. Mirrors :func:`reserve_quota`."""
    with connect(db) as conn:
        with transaction(conn):
            row = conn.execute(
                "SELECT renders FROM usage_daily WHERE user_id = ? AND day = ?", (user_id, day)
            ).fetchone()
            used = row[0] if row else 0
            if used + 1 > limit:
                return False, used
            conn.execute(
                """
                INSERT INTO usage_daily (user_id, day, chars, requests, renders)
                VALUES (?, ?, 0, 0, 1)
                ON CONFLICT(user_id, day) DO UPDATE
                    SET renders = renders + 1
                """,
                (user_id, day),
            )
            return True, used + 1


def refund_render_quota(db: Path, user_id: int, day: str) -> None:
    """Give a render allowance back when the render never produced a file."""
    with connect(db) as conn:
        conn.execute(
            "UPDATE usage_daily SET renders = MAX(0, renders - 1) WHERE user_id = ? AND day = ?",
            (user_id, day),
        )


def renders_today(db: Path, user_id: int, day: str) -> int:
    with connect(db) as conn:
        row = conn.execute(
            "SELECT renders FROM usage_daily WHERE user_id = ? AND day = ?", (user_id, day)
        ).fetchone()
    return row[0] if row else 0


def usage_today(db: Path, user_id: int, day: str) -> int:
    with connect(db) as conn:
        row = conn.execute(
            "SELECT chars FROM usage_daily WHERE user_id = ? AND day = ?", (user_id, day)
        ).fetchone()
    return row[0] if row else 0


def transcribed_today(db: Path, user_id: int, day: str) -> int:
    """Seconds of audio charged to this account today."""
    with connect(db) as conn:
        row = conn.execute(
            "SELECT transcribed_seconds FROM usage_daily WHERE user_id = ? AND day = ?",
            (user_id, day),
        ).fetchone()
    return row[0] if row else 0
