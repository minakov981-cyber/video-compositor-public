import hashlib
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
        # Free tier + subscription. Existing users get the Tools trial from the day this column appears.
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS generations_used INTEGER NOT NULL DEFAULT 0")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS tools_trial_expires TIMESTAMPTZ DEFAULT (NOW() + INTERVAL '7 days')")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS stripe_customer_id TEXT")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS stripe_subscription_id TEXT")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS subscription_status TEXT")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS subscription_period_end TIMESTAMPTZ")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS cancel_at_period_end BOOLEAN NOT NULL DEFAULT FALSE")
        # One-time welcome message: only new free signups get TRUE (see create_user), existing users stay FALSE
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS welcome_pending BOOLEAN NOT NULL DEFAULT FALSE")
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
        cur.execute("ALTER TABLE compose_sessions ADD COLUMN IF NOT EXISTS ordered_clips JSONB")
        cur.execute("ALTER TABLE compose_sessions ADD COLUMN IF NOT EXISTS music_fade_out BOOLEAN NOT NULL DEFAULT FALSE")
        cur.execute("ALTER TABLE compose_sessions ADD COLUMN IF NOT EXISTS music_fade_duration DOUBLE PRECISION NOT NULL DEFAULT 2")
        cur.execute("ALTER TABLE compose_sessions ADD COLUMN IF NOT EXISTS transition TEXT NOT NULL DEFAULT 'none'")
        cur.execute("ALTER TABLE compose_sessions ADD COLUMN IF NOT EXISTS transition_duration DOUBLE PRECISION NOT NULL DEFAULT 0.5")
        cur.execute("ALTER TABLE compose_sessions ADD COLUMN IF NOT EXISTS normalize_audio BOOLEAN NOT NULL DEFAULT FALSE")
        cur.execute("ALTER TABLE compose_sessions ADD COLUMN IF NOT EXISTS export_opts JSONB")
        cur.execute("ALTER TABLE compose_sessions ADD COLUMN IF NOT EXISTS owner_email TEXT")
        cur.execute("ALTER TABLE compose_sessions ADD COLUMN IF NOT EXISTS variations_used INTEGER NOT NULL DEFAULT 0")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS render_jobs (
                job_id     TEXT PRIMARY KEY,
                status     TEXT NOT NULL DEFAULT 'processing',
                result     JSONB,
                error      TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        cur.execute("ALTER TABLE render_jobs ADD COLUMN IF NOT EXISTS owner_email TEXT")
        # Render queue (worker.py): jobs wait as 'queued' with everything needed to run them in payload,
        # so a job interrupted by a deploy or crash can be picked up again by the next worker
        cur.execute("ALTER TABLE render_jobs ADD COLUMN IF NOT EXISTS payload JSONB")
        cur.execute("ALTER TABLE render_jobs ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0")
        cur.execute("ALTER TABLE render_jobs ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMPTZ")
        cur.execute("ALTER TABLE render_jobs ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ")
        # error is what the user sees; error_detail keeps FFmpeg's own output for the admin queue page
        cur.execute("ALTER TABLE render_jobs ADD COLUMN IF NOT EXISTS error_detail TEXT")
        cur.execute("CREATE INDEX IF NOT EXISTS render_jobs_queue ON render_jobs (status, created_at)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS rate_events (
                key        TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS rate_events_key_time ON rate_events (key, created_at)")
        # Deleted accounts leave only a hash of the email and the free-plan usage, so deleting and
        # signing up again doesn't reset the free limits
        cur.execute("""
            CREATE TABLE IF NOT EXISTS deleted_users (
                email_hash          TEXT PRIMARY KEY,
                generations_used    INTEGER NOT NULL DEFAULT 0,
                tools_trial_expires TIMESTAMPTZ,
                deleted_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)


def _email_hash(email: str) -> str:
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()


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


def create_user(email: str):
    """Register a free account; no-op if the email already exists.

    An email that belonged to a deleted account gets its old free-plan usage back (and no welcome message).
    """
    with _db() as cur:
        cur.execute(
            "INSERT INTO users (email, welcome_pending) VALUES (%s, TRUE) ON CONFLICT (email) DO NOTHING",
            (email,),
        )
        if cur.rowcount:
            cur.execute(
                "UPDATE users SET generations_used = d.generations_used, "
                "tools_trial_expires = d.tools_trial_expires, welcome_pending = FALSE "
                "FROM deleted_users d WHERE d.email_hash = %s AND users.email = %s",
                (_email_hash(email), email),
            )


def delete_user(email: str) -> list:
    """Delete an account with its sessions and jobs; returns their ids so the caller can remove files."""
    with _db() as cur:
        cur.execute("SELECT generations_used, tools_trial_expires FROM users WHERE email = %s FOR UPDATE", (email,))
        user = cur.fetchone()
        if not user:
            return []
        cur.execute(
            """
            INSERT INTO deleted_users (email_hash, generations_used, tools_trial_expires) VALUES (%s, %s, %s)
            ON CONFLICT (email_hash) DO UPDATE SET
                generations_used    = GREATEST(deleted_users.generations_used, EXCLUDED.generations_used),
                tools_trial_expires = LEAST(deleted_users.tools_trial_expires, EXCLUDED.tools_trial_expires),
                deleted_at          = NOW()
            """,
            (_email_hash(email), user["generations_used"], user["tools_trial_expires"]),
        )
        cur.execute("DELETE FROM compose_sessions WHERE owner_email = %s RETURNING session_id", (email,))
        ids = [r["session_id"] for r in cur.fetchall()]
        cur.execute("DELETE FROM render_jobs WHERE owner_email = %s RETURNING job_id", (email,))
        ids += [r["job_id"] for r in cur.fetchall()]
        cur.execute("DELETE FROM users WHERE email = %s", (email,))
        return ids


def mark_welcome_seen(email: str):
    with _db() as cur:
        cur.execute("UPDATE users SET welcome_pending = FALSE WHERE email = %s", (email,))


def use_generation(email: str, limit=None) -> bool:
    """Count one Create Video render; False if the free limit is already reached."""
    with _db() as cur:
        cur.execute(
            "UPDATE users SET generations_used = generations_used + 1 "
            "WHERE email = %s AND (%s::int IS NULL OR generations_used < %s::int) RETURNING generations_used",
            (email, limit, limit),
        )
        return cur.fetchone() is not None


def refund_generation(email: str):
    with _db() as cur:
        cur.execute(
            "UPDATE users SET generations_used = GREATEST(generations_used - 1, 0) WHERE email = %s",
            (email,),
        )


def set_stripe_customer(email: str, customer_id: str):
    with _db() as cur:
        cur.execute("UPDATE users SET stripe_customer_id = %s WHERE email = %s", (customer_id, email))


def get_user_by_customer(customer_id: str):
    with _db() as cur:
        cur.execute("SELECT * FROM users WHERE stripe_customer_id = %s", (customer_id,))
        return cur.fetchone()


def update_subscription(email: str, subscription_id: str, status: str, period_end, cancel_at_period_end: bool):
    with _db() as cur:
        cur.execute(
            "UPDATE users SET stripe_subscription_id = %s, subscription_status = %s, "
            "subscription_period_end = %s, cancel_at_period_end = %s WHERE email = %s",
            (subscription_id, status, period_end, cancel_at_period_end, email),
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
    music_fade_out: bool,
    music_fade_duration: float,
    clip_audios: dict,
    use_original_duration: bool,
    output_format: str,
    fit_mode: str,
    clip_trims: dict,
    ordered_clips: list,
    transition: str = "none",
    transition_duration: float = 0.5,
    normalize_audio: bool = False,
    export_opts: dict = None,
    owner_email: str = None,
):
    with _db() as cur:
        cur.execute(
            """
            INSERT INTO compose_sessions (
                session_id, hook_clips, middle_clips, final_clips,
                audio, duration_range, music_start, music_end,
                clip_audios, use_original_duration, output_format,
                fit_mode, clip_trims, ordered_clips,
                music_fade_out, music_fade_duration,
                transition, transition_duration, normalize_audio, export_opts,
                owner_email
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                session_id,
                Json(hook_clips), Json(middle_clips), Json(final_clips),
                audio, duration_range, music_start, music_end,
                Json(clip_audios), use_original_duration, output_format,
                fit_mode, Json(clip_trims), Json(ordered_clips),
                music_fade_out, music_fade_duration,
                transition, transition_duration, normalize_audio, Json(export_opts),
                owner_email,
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
    transition=None,
    transition_duration=None,
    export_opts=None,
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
    if transition is not None:
        updates.append("transition = %s")
        params.append(transition)
    if transition_duration is not None:
        updates.append("transition_duration = %s")
        params.append(transition_duration)
    if export_opts is not None:
        updates.append("export_opts = %s")
        params.append(Json(export_opts))
    if not updates:
        return
    params.append(session_id)
    with _db() as cur:
        cur.execute(
            f"UPDATE compose_sessions SET {', '.join(updates)} WHERE session_id = %s",
            params,
        )


def set_compose_session_plan(session_id, hook_clips, middle_clips, final_clips, ordered_clips,
                             clip_trims, clip_audios, music_start, transition, duration_range):
    """Replace a session's clips with the segments Smart Auto planned (see app._plan_smart_session)."""
    with _db() as cur:
        cur.execute(
            "UPDATE compose_sessions SET hook_clips = %s, middle_clips = %s, final_clips = %s, ordered_clips = %s, "
            "clip_trims = %s, clip_audios = %s, music_start = %s, transition = %s, duration_range = %s, "
            "use_original_duration = TRUE WHERE session_id = %s",
            (Json(hook_clips), Json(middle_clips), Json(final_clips), Json(ordered_clips), Json(clip_trims),
             Json(clip_audios), music_start, transition, duration_range, session_id),
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


def use_session_variation(session_id: str, limit=None) -> bool:
    """Count one New Variation render; False if the session's free variations are used up."""
    with _db() as cur:
        cur.execute(
            "UPDATE compose_sessions SET variations_used = variations_used + 1 "
            "WHERE session_id = %s AND (%s::int IS NULL OR variations_used < %s::int) RETURNING variations_used",
            (session_id, limit, limit),
        )
        return cur.fetchone() is not None


def refund_session_variation(session_id: str):
    with _db() as cur:
        cur.execute(
            "UPDATE compose_sessions SET variations_used = GREATEST(variations_used - 1, 0) WHERE session_id = %s",
            (session_id,),
        )


def list_all_session_ids_with_clip_paths() -> list:
    with _db() as cur:
        cur.execute(
            "SELECT session_id, hook_clips, middle_clips, final_clips, audio, ordered_clips "
            "FROM compose_sessions"
        )
        return [dict(row) for row in cur.fetchall()]


def delete_compose_session(session_id: str):
    with _db() as cur:
        cur.execute("DELETE FROM compose_sessions WHERE session_id = %s", (session_id,))


# ── Render jobs ───────────────────────────────────────────────────────────────

def enqueue_render_job(job_id: str, owner_email: str, payload: dict):
    """Add a job to the render queue; worker.py picks it up."""
    with _db() as cur:
        cur.execute(
            "INSERT INTO render_jobs (job_id, owner_email, status, payload) VALUES (%s, %s, 'queued', %s)",
            (job_id, owner_email, Json(payload)),
        )


def claim_render_job():
    """Take the oldest queued job and mark it processing; None if the queue is empty."""
    with _db() as cur:
        cur.execute(
            """
            UPDATE render_jobs SET status = 'processing', attempts = attempts + 1,
                                   heartbeat_at = NOW(), started_at = NOW()
            WHERE job_id = (
                SELECT job_id FROM render_jobs WHERE status = 'queued'
                ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED
            )
            RETURNING job_id, owner_email, payload, attempts
            """
        )
        row = cur.fetchone()
        return dict(row) if row else None


def heartbeat_render_jobs(job_ids: list):
    if not job_ids:
        return
    with _db() as cur:
        cur.execute(
            "UPDATE render_jobs SET heartbeat_at = NOW() WHERE job_id = ANY(%s) AND status = 'processing'",
            (list(job_ids),),
        )


def requeue_render_jobs(job_ids: list, count_attempt: bool = False):
    """Put processing jobs back in the queue. A deploy/shutdown doesn't count as an attempt, a crash does."""
    if not job_ids:
        return
    with _db() as cur:
        cur.execute(
            "UPDATE render_jobs SET status = 'queued', heartbeat_at = NULL, "
            "attempts = CASE WHEN %s THEN attempts ELSE GREATEST(attempts - 1, 0) END "
            "WHERE job_id = ANY(%s) AND status = 'processing'",
            (count_attempt, list(job_ids)),
        )


def fail_render_job(job_id: str, error_str: str) -> bool:
    """Mark a job that is still processing as failed; False if it already finished on its own."""
    with _db() as cur:
        cur.execute(
            "UPDATE render_jobs SET status = 'error', error = %s WHERE job_id = %s AND status = 'processing'",
            (error_str, job_id),
        )
        return cur.rowcount > 0


def requeue_stale_render_jobs(stale_seconds: int) -> int:
    """Re-queue jobs whose worker died without saying so (no heartbeat). Jobs from before the queue
    existed have no payload and can't be re-run, so they fail instead."""
    with _db() as cur:
        cur.execute(
            "UPDATE render_jobs SET status = 'error', error = %s "
            "WHERE status = 'processing' AND payload IS NULL",
            ("The server restarted while this was rendering. Please try again.",),
        )
        cur.execute(
            "UPDATE render_jobs SET status = 'queued', heartbeat_at = NULL "
            "WHERE status = 'processing' AND (heartbeat_at IS NULL OR heartbeat_at < NOW() - make_interval(secs => %s))",
            (stale_seconds,),
        )
        return cur.rowcount


def render_queue_overview(recent_errors: int = 20) -> dict:
    """What the admin queue page shows: counts, jobs waiting/running now, latest failures."""
    with _db() as cur:
        cur.execute(
            """
            SELECT
                COUNT(*) FILTER (WHERE status = 'queued')                                        AS queued,
                COUNT(*) FILTER (WHERE status = 'processing')                                    AS processing,
                COUNT(*) FILTER (WHERE status = 'done'  AND created_at > NOW() - INTERVAL '24 hours') AS done_24h,
                COUNT(*) FILTER (WHERE status = 'error' AND created_at > NOW() - INTERVAL '24 hours') AS errors_24h
            FROM render_jobs
            """
        )
        counts = dict(cur.fetchone())
        cur.execute(
            "SELECT job_id, status, owner_email, payload->>'kind' AS kind, payload->>'tool' AS tool, "
            "(payload->>'variation')::boolean AS variation, attempts, created_at, started_at, heartbeat_at "
            "FROM render_jobs WHERE status IN ('queued', 'processing') ORDER BY created_at"
        )
        active = [dict(r) for r in cur.fetchall()]
        cur.execute(
            "SELECT job_id, owner_email, payload->>'kind' AS kind, payload->>'tool' AS tool, "
            "(payload->>'variation')::boolean AS variation, attempts, created_at, error, error_detail "
            "FROM render_jobs WHERE status = 'error' ORDER BY created_at DESC LIMIT %s",
            (recent_errors,),
        )
        errors = [dict(r) for r in cur.fetchall()]
        return {"counts": counts, "active": active, "errors": errors}


def render_queue_position(job_id: str) -> int:
    """How many queued jobs are ahead of this one."""
    with _db() as cur:
        cur.execute(
            "SELECT COUNT(*) AS n FROM render_jobs WHERE status = 'queued' "
            "AND created_at < (SELECT created_at FROM render_jobs WHERE job_id = %s)",
            (job_id,),
        )
        return cur.fetchone()["n"]


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


def set_render_job_error(job_id: str, error_str: str, detail: str = None):
    with _db() as cur:
        cur.execute(
            "UPDATE render_jobs SET status = 'error', error = %s, error_detail = %s WHERE job_id = %s",
            (error_str, detail, job_id),
        )


def get_owner(object_id: str):
    """(found, owner_email) for a compose session or render job id. Legacy rows have no owner."""
    with _db() as cur:
        cur.execute(
            "SELECT owner_email FROM compose_sessions WHERE session_id = %s "
            "UNION ALL SELECT owner_email FROM render_jobs WHERE job_id = %s LIMIT 1",
            (object_id, object_id),
        )
        row = cur.fetchone()
        return (row is not None, row["owner_email"] if row else None)


# ── Rate limiting ─────────────────────────────────────────────────────────────

def hit_rate_limit(key: str, limit: int, window_seconds: int) -> bool:
    """Record one attempt for key; False if it already had `limit` attempts in the window.

    Lives in Postgres so the limit holds across all gunicorn workers.
    """
    with _db() as cur:
        cur.execute(
            "SELECT COUNT(*) AS n FROM rate_events WHERE key = %s "
            "AND created_at > NOW() - make_interval(secs => %s)",
            (key, window_seconds),
        )
        if cur.fetchone()["n"] >= limit:
            return False
        cur.execute("INSERT INTO rate_events (key) VALUES (%s)", (key,))
        return True


def purge_rate_events(max_age_hours: int = 24):
    with _db() as cur:
        cur.execute(
            "DELETE FROM rate_events WHERE created_at < NOW() - make_interval(hours => %s)",
            (max_age_hours,),
        )
