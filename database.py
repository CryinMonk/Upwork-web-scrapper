import sqlite3
import time
import logging
from contextlib import contextmanager

DB_PATH = "jobs.db"

# Retry settings — mirror the async settings in discordbot.py
DB_MAX_RETRIES   = 3    # Attempts before giving up
DB_RETRY_DELAY   = 10   # Seconds between each retry
DB_BACKOFF_DELAY = 300  # 5 minutes after all retries exhausted

logger = logging.getLogger("database")


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def retry_sync(label: str, fn, *args, **kwargs):
    """
    Call a plain (sync) function up to DB_MAX_RETRIES times.
    Waits DB_RETRY_DELAY seconds between each attempt.
    After all retries are exhausted, waits DB_BACKOFF_DELAY (5 min) then re-raises.
    Single responsibility: retry orchestration only.
    """
    last_exc = None
    for attempt in range(1, DB_MAX_RETRIES + 1):
        try:
            return fn(*args, **kwargs)
        except sqlite3.OperationalError as e:
            last_exc = e
            print(f"[DB:{label}] SQLite operational error (attempt {attempt}/{DB_MAX_RETRIES}): {e}")
        except sqlite3.DatabaseError as e:
            last_exc = e
            print(f"[DB:{label}] SQLite database error (attempt {attempt}/{DB_MAX_RETRIES}): {e}")
        except OSError as e:
            last_exc = e
            print(f"[DB:{label}] OS/disk error (attempt {attempt}/{DB_MAX_RETRIES}): {e}")
        except Exception as e:
            last_exc = e
            print(f"[DB:{label}] Unexpected error (attempt {attempt}/{DB_MAX_RETRIES}): {type(e).__name__}: {e}")

        if attempt < DB_MAX_RETRIES:
            print(f"[DB:{label}] Retrying in {DB_RETRY_DELAY}s...")
            time.sleep(DB_RETRY_DELAY)

    print(f"[DB:{label}] All {DB_MAX_RETRIES} retries exhausted. Backing off for {DB_BACKOFF_DELAY}s (5 min).")
    time.sleep(DB_BACKOFF_DELAY)
    raise last_exc


def init_db():
    """Create all tables if they don't exist."""
    def _run():
        with get_conn() as conn:
            conn.executescript("""
                -- Stores posted jobs to prevent duplicates
                CREATE TABLE IF NOT EXISTS posted_jobs (
                    job_id      TEXT PRIMARY KEY,
                    title       TEXT,
                    posted_at   TEXT,
                    detected_at TEXT DEFAULT (datetime('now'))
                );

                -- Maps search keywords to Discord channel IDs
                CREATE TABLE IF NOT EXISTS search_channels (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    keyword    TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    active     INTEGER DEFAULT 1,
                    created_at TEXT DEFAULT (datetime('now')),
                    UNIQUE(keyword, channel_id)
                );

                -- Application logs with timestamps
                CREATE TABLE IF NOT EXISTS logs (
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    logged_at TEXT DEFAULT (datetime('now')),
                    level     TEXT NOT NULL,
                    logger    TEXT NOT NULL,
                    message   TEXT NOT NULL
                );
            """)
        print("[DB] Tables initialized.")
    retry_sync("init_db", _run)


def log(level: str, logger_name: str, message: str):
    """Persist a single log record. Never raises — a broken log sink must not crash the app."""
    try:
        def _run():
            with get_conn() as conn:
                conn.execute(
                    "INSERT INTO logs (level, logger, message) VALUES (?, ?, ?)",
                    (level, logger_name, message),
                )
        retry_sync("log", _run)
    except Exception:
        pass  # Log sink failure must never propagate


def is_job_posted(job_id: str) -> bool:
    def _run():
        with get_conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM posted_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            return row is not None
    return retry_sync("is_job_posted", _run)


def mark_job_posted(job_id: str, title: str, posted_at: str):
    def _run():
        with get_conn() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO posted_jobs (job_id, title, posted_at)
                VALUES (?, ?, ?)
                """,
                (job_id, title, posted_at),
            )
    retry_sync("mark_job_posted", _run)


def get_active_search_channels() -> list[dict]:
    """Return all active keyword→channel mappings."""
    def _run():
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT keyword, channel_id FROM search_channels WHERE active = 1"
            ).fetchall()
            return [dict(row) for row in rows]
    return retry_sync("get_active_search_channels", _run)


def add_search_channel(keyword: str, channel_id: str):
    def _run():
        with get_conn() as conn:
            conn.execute(
                """
                INSERT INTO search_channels (keyword, channel_id)
                VALUES (?, ?)
                ON CONFLICT(keyword, channel_id) DO UPDATE SET active = 1
                """,
                (keyword, channel_id),
            )
    retry_sync("add_search_channel", _run)


def remove_search_channel(keyword: str, channel_id: str):
    def _run():
        with get_conn() as conn:
            conn.execute(
                """
                UPDATE search_channels SET active = 0
                WHERE keyword = ? AND channel_id = ?
                """,
                (keyword, channel_id),
            )
    retry_sync("remove_search_channel", _run)


def cleanup_old_jobs():
    """Delete posted jobs older than 30 days. Returns number of rows deleted."""
    def _run():
        with get_conn() as conn:
            cursor = conn.execute(
                "DELETE FROM posted_jobs WHERE detected_at < datetime('now', '-30 days')"
            )
            deleted = cursor.rowcount
            if deleted:
                print(f"[DB] Cleaned up {deleted} old job(s).")
            return deleted
    return retry_sync("cleanup_old_jobs", _run)


def cleanup_old_logs():
    """Delete log entries older than 30 days."""
    def _run():
        with get_conn() as conn:
            cursor = conn.execute(
                "DELETE FROM logs WHERE logged_at < datetime('now', '-30 days')"
            )
            deleted = cursor.rowcount
            if deleted:
                print(f"[DB] Cleaned up {deleted} old log(s).")
            return deleted
    return retry_sync("cleanup_old_logs", _run)


def close_db():
    """Shutdown hook. get_conn() commits on every operation so nothing extra needed."""
    print("[DB] Shutdown signal received — database closed cleanly.")


def count_recent_jobs(since_minutes: int = 60) -> int:
    """Count jobs posted within the last `since_minutes` minutes."""
    def _run():
        with get_conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM posted_jobs WHERE detected_at >= datetime('now', ?)",
                (f"-{since_minutes} minutes",),
            ).fetchone()
            return row[0] if row else 0
    return retry_sync("count_recent_jobs", _run)


def count_recent_errors(since_minutes: int = 60) -> int:
    """Count ERROR-level log entries within the last `since_minutes` minutes."""
    def _run():
        with get_conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM logs WHERE level = 'ERROR' AND logged_at >= datetime('now', ?)",
                (f"-{since_minutes} minutes",),
            ).fetchone()
            return row[0] if row else 0
    return retry_sync("count_recent_errors", _run)