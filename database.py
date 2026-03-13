import sqlite3
import time
import logging
from contextlib import contextmanager
from json_helper import get_json
from database_helper import _extract_job_record

DB_PATH = "jobs.db"

logger = logging.getLogger("database")

DB_MAX_RETRIES   = int(get_json()["Retry"]["MAX_RETRIES"])
DB_RETRY_DELAY   = int(get_json()["Retry"]["RETRY_DELAY"])
DB_BACKOFF_DELAY = int(get_json()["Retry"]["BACKOFF_DELAY"])


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


# ─── Schema ───────────────────────────────────────────────────────────────────

def init_db():
    """Create all tables if they don't exist."""
    def _run():
        with get_conn() as conn:
            conn.executescript("""
                -- Stores posted jobs to prevent duplicates
                CREATE TABLE IF NOT EXISTS posted_jobs (
                    job_id           TEXT PRIMARY KEY,
                    title            TEXT,
                    posted_at        TEXT,
                    detected_at      TEXT DEFAULT (datetime('now')),
                    description      TEXT,
                    budget           TEXT,
                    job_type         TEXT,
                    experience_level TEXT,
                    duration         TEXT,
                    skills           TEXT,
                    location         TEXT,
                    total_spent      TEXT,
                    proposals        INTEGER
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

                -- Caches resolved canonical group for each keyword.
                -- Populated on first !add; never re-queried against the taxonomy after that.
                CREATE TABLE IF NOT EXISTS keyword_metadata (
                    keyword      TEXT PRIMARY KEY,
                    canonical    TEXT NOT NULL,
                    family       TEXT NOT NULL,
                    resolved_at  TEXT DEFAULT (datetime('now'))
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


# ─── Logging ──────────────────────────────────────────────────────────────────

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


# ─── Jobs ─────────────────────────────────────────────────────────────────────

def is_job_posted(job_id: str) -> bool:
    def _run():
        with get_conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM posted_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            return row is not None
    return retry_sync("is_job_posted", _run)


def mark_job_posted(job_id: str, details: dict):
    """
    Persist a job as posted. Extracts and stores rich fields from the details payload.
    Uses INSERT OR IGNORE so a duplicate call never overwrites an existing record.
    """
    record = _extract_job_record(details)

    def _run():
        with get_conn() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO posted_jobs (
                    job_id, title, posted_at, description, budget, job_type,
                    experience_level, duration, skills, location, total_spent, proposals
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    record["title"],
                    record["posted_at"],
                    record["description"],
                    record["budget"],
                    record["job_type"],
                    record["experience_level"],
                    record["duration"],
                    record["skills"],
                    record["location"],
                    record["total_spent"],
                    record["proposals"],
                ),
            )
    retry_sync("mark_job_posted", _run)


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


# ─── Search channels ──────────────────────────────────────────────────────────

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


def is_keyword_tracked(keyword: str) -> bool:
    """True if the keyword already has an active search_channels entry."""
    def _run():
        with get_conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM search_channels WHERE keyword = ? AND active = 1",
                (keyword,),
            ).fetchone()
            return row is not None
    return retry_sync("is_keyword_tracked", _run)


def get_keywords_for_channel(channel_id: str) -> list[str]:
    """Return all active keywords pointing to a given channel."""
    def _run():
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT keyword FROM search_channels WHERE channel_id = ? AND active = 1",
                (channel_id,),
            ).fetchall()
            return [r["keyword"] for r in rows]
    return retry_sync("get_keywords_for_channel", _run)


def deactivate_channel_keywords(channel_id: str) -> int:
    """
    Deactivate ALL keywords for a channel (used when the Discord channel is deleted).
    Returns count of deactivated rows.
    """
    def _run():
        with get_conn() as conn:
            cur = conn.execute(
                "UPDATE search_channels SET active = 0 WHERE channel_id = ?",
                (channel_id,),
            )
            return cur.rowcount
    return retry_sync("deactivate_channel_keywords", _run)


# ─── Keyword metadata (skill taxonomy cache) ──────────────────────────────────

def get_keyword_canonical(keyword: str) -> dict | None:
    """
    Return cached {canonical, family} for a keyword, or None if unseen.
    This is checked first on every !add so the taxonomy file is only
    consulted on the very first occurrence of a keyword.
    """
    def _run():
        with get_conn() as conn:
            row = conn.execute(
                "SELECT canonical, family FROM keyword_metadata WHERE keyword = ?",
                (keyword,),
            ).fetchone()
            return dict(row) if row else None
    return retry_sync("get_keyword_canonical", _run)


def set_keyword_metadata(keyword: str, canonical: str, family: str) -> None:
    """Cache the resolved canonical/family for a keyword (upsert)."""
    def _run():
        with get_conn() as conn:
            conn.execute(
                """
                INSERT INTO keyword_metadata (keyword, canonical, family)
                VALUES (?, ?, ?)
                ON CONFLICT(keyword) DO UPDATE SET
                    canonical   = excluded.canonical,
                    family      = excluded.family,
                    resolved_at = datetime('now')
                """,
                (keyword, canonical, family),
            )
    retry_sync("set_keyword_metadata", _run)


def get_channel_for_canonical(canonical: str) -> str | None:
    """
    Return the channel_id already tracking any keyword whose canonical matches.
    Returns None if no active channel exists for this canonical group yet.
    """
    def _run():
        with get_conn() as conn:
            row = conn.execute(
                """
                SELECT sc.channel_id
                FROM search_channels sc
                JOIN keyword_metadata km ON km.keyword = sc.keyword
                WHERE km.canonical = ? AND sc.active = 1
                LIMIT 1
                """,
                (canonical,),
            ).fetchone()
            return row["channel_id"] if row else None
    return retry_sync("get_channel_for_canonical", _run)


def get_all_active_canonicals() -> list[str]:
    """Return all distinct canonical names currently being tracked."""
    def _run():
        with get_conn() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT km.canonical
                FROM keyword_metadata km
                JOIN search_channels sc ON sc.keyword = km.keyword
                WHERE sc.active = 1
                """,
            ).fetchall()
            return [r["canonical"] for r in rows]
    return retry_sync("get_all_active_canonicals", _run)


# ─── Logs ─────────────────────────────────────────────────────────────────────

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

