import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import psycopg2
from psycopg2.extras import Json, RealDictCursor

DATABASE_URL = os.environ.get("DATABASE_URL", "")


@contextmanager
def _db():
    conn = psycopg2.connect(DATABASE_URL)
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with _db() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id            SERIAL PRIMARY KEY,
                email         TEXT UNIQUE NOT NULL,
                paid          BOOLEAN NOT NULL DEFAULT FALSE,
                created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                magic_token   TEXT,
                token_expires TIMESTAMPTZ,
                password_hash TEXT
            )
        """)
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash TEXT")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS trial_expires TIMESTAMPTZ")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS compose_sessions (
                session_id            TEXT PRIMARY KEY,
                hook_clips            JSONB NOT NULL,
                middle_clips          JSONB NOT NULL,
                final_clips           JSONB NOT NULL,
                audio                 TEXT,
                duration_range        TEXT NOT NULL,
                music_start           DOUBLE PRECISION NOT NULL DEFAULT 0,
                music_end             DOUBLE PRECISION,
                clip_audios           JSONB NOT NULL DEFAULT '{}',
                use_original_duration BOOLEAN NOT NULL DEFAULT FALSE,
                output_format         TEXT NOT NULL DEFAULT '9:16',
                fit_mode              TEXT NOT NULL DEFAULT 'crop',
                clip_trims            JSONB NOT NULL DEFAULT '{}',
                variation_count       INTEGER NOT NULL DEFAULT 0,
                created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS render_jobs (
                job_id     TEXT PRIMARY KEY,
                status     TEXT NOT NULL DEFAULT 'processing',
                result     JSONB,
                error      TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)


def get_user(email: str):
    with _db() as cur:
        cur.execute("SELECT * FROM users WHERE email = %s", (email,))
        return cur.fetchone()


def upsert_user_paid(email: str):
    with _db() as cur:
        cur.execute(
            """
            INSERT INTO users (email, paid) VALUES (%s, TRUE)
            ON CONFLICT (email) DO UPDATE SET paid = TRUE
            """,
            (email,),
        )


def set_magic_token(email: str, token: str, expires: datetime):
    with _db() as cur:
        cur.execute(
            """
            INSERT INTO users (email, magic_token, token_expires)
            VALUES (%s, %s, %s)
            ON CONFLICT (email) DO UPDATE SET magic_token = %s, token_expires = %s
            """,
            (email, token, expires, token, expires),
        )


def get_user_by_token(token: str):
    with _db() as cur:
        cur.execute("SELECT * FROM users WHERE magic_token = %s", (token,))
        return cur.fetchone()


def clear_magic_token(email: str):
    with _db() as cur:
        cur.execute(
            "UPDATE users SET magic_token = NULL, token_expires = NULL WHERE email = %s",
            (email,),
        )


def grant_trial(email: str, days: int):
    expires = datetime.now(timezone.utc) + timedelta(days=days)
    with _db() as cur:
        cur.execute(
            """
            INSERT INTO users (email, trial_expires)
            VALUES (%s, %s)
            ON CONFLICT (email) DO UPDATE SET trial_expires = %s
            """,
            (email, expires, expires),
        )


def set_password(email: str, password_hash: str):
    with _db() as cur:
        cur.execute(
            "UPDATE users SET password_hash = %s WHERE email = %s",
            (password_hash, email),
        )


# ── Compose sessions ──────────────────────────────────────────────────────────

def create_compose_session(
    session_id: str,
    hook_clips: list,
    middle_clips: list,
    final_clips: list,
    audio,
    duration_range: str,
    music_start: float,
    music_end,
    clip_audios: dict,
    use_original_duration: bool,
    output_format: str,
    fit_mode: str,
    clip_trims: dict,
):
    with _db() as cur:
        cur.execute(
            """
            INSERT INTO compose_sessions (
                session_id, hook_clips, middle_clips, final_clips,
                audio, duration_range, music_start, music_end,
                clip_audios, use_original_duration, output_format,
                fit_mode, clip_trims
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                session_id,
                Json(hook_clips), Json(middle_clips), Json(final_clips),
                audio, duration_range, music_start, music_end,
                Json(clip_audios), use_original_duration, output_format,
                fit_mode, Json(clip_trims),
            ),
        )


def get_compose_session(session_id: str):
    with _db() as cur:
        cur.execute("SELECT * FROM compose_sessions WHERE session_id = %s", (session_id,))
        row = cur.fetchone()
        # psycopg2 2.5+ auto-deserializes JSONB columns to Python dict/list;
        # no json.loads() needed here.
        return dict(row) if row else None


def update_compose_session_options(
    session_id: str,
    use_original_duration=None,
    output_format=None,
    fit_mode=None,
):
    updates, params = [], []
    if use_original_duration is not None:
        updates.append("use_original_duration = %s")
        params.append(use_original_duration)
    if output_format is not None:
        updates.append("output_format = %s")
        params.append(output_format)
    if fit_mode is not None:
        updates.append("fit_mode = %s")
        params.append(fit_mode)
    if not updates:
        return
    params.append(session_id)
    with _db() as cur:
        cur.execute(
            f"UPDATE compose_sessions SET {', '.join(updates)} WHERE session_id = %s",
            params,
        )


def increment_variation_count(session_id: str) -> int:
    with _db() as cur:
        cur.execute(
            "UPDATE compose_sessions SET variation_count = variation_count + 1 "
            "WHERE session_id = %s RETURNING variation_count",
            (session_id,),
        )
        row = cur.fetchone()
        return row["variation_count"]


def list_all_session_ids_with_clip_paths() -> list:
    with _db() as cur:
        cur.execute(
            "SELECT session_id, hook_clips, middle_clips, final_clips, audio "
            "FROM compose_sessions"
        )
        return [dict(row) for row in cur.fetchall()]


def delete_compose_session(session_id: str):
    with _db() as cur:
        cur.execute("DELETE FROM compose_sessions WHERE session_id = %s", (session_id,))


# ── Render jobs ───────────────────────────────────────────────────────────────

def create_render_job(job_id: str):
    with _db() as cur:
        cur.execute(
            "INSERT INTO render_jobs (job_id) VALUES (%s)",
            (job_id,),
        )


def get_render_job(job_id: str):
    with _db() as cur:
        cur.execute("SELECT * FROM render_jobs WHERE job_id = %s", (job_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def set_render_job_done(job_id: str, result_dict: dict):
    with _db() as cur:
        cur.execute(
            "UPDATE render_jobs SET status = 'done', result = %s WHERE job_id = %s",
            (Json(result_dict), job_id),
        )


def set_render_job_error(job_id: str, error_str: str):
    with _db() as cur:
        cur.execute(
            "UPDATE render_jobs SET status = 'error', error = %s WHERE job_id = %s",
            (error_str, job_id),
        )
