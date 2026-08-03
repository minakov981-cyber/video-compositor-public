import os
from contextlib import contextmanager
from datetime import datetime

import psycopg2
from psycopg2.extras import RealDictCursor

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


def set_password(email: str, password_hash: str):
    with _db() as cur:
        cur.execute(
            "UPDATE users SET password_hash = %s WHERE email = %s",
            (password_hash, email),
        )
