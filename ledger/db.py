"""
Ledger — database layer.

SQLite is the single source of truth for a run. Every page result is committed
the moment it arrives, which is what makes the whole thing restartable: kill the
process at page 40,000 and the next start resumes at 40,001 with nothing lost.

Why SQLite rather than the JSON files the original script wrote:

  - The old script only wrote its JSON after an entire book finished, so a crash
    at page 400 of 450 lost everything and a restart began again at page 1.
  - JSON cannot be safely written by twenty concurrent workers.
  - "Show me every page marked unreadable" is a one-line query here and a
    full-file reparse there.

JSON has not gone away — it is the EXPORT format (see export.py), generated on
demand from this database. You still get your JSON files; they are simply no
longer the thing a fourteen-hour run depends on.

WAL mode is enabled so readers (the web UI) never block the writers (workers).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from .quota import ApiKey, pacific_date


# ---------------------------------------------------------------------
# Page lifecycle
# ---------------------------------------------------------------------
#
#   pending      -- not yet attempted
#   in_progress  -- claimed by a worker right now
#   done         -- transcribed and accepted
#   flagged      -- transcribed but failed a quality heuristic; needs review
#   failed       -- attempted max_page_attempts times without success
#
# Note that `flagged` pages hold real content. They are not errors, just
# results we are not confident about — see worker.py for the heuristics.

PAGE_PENDING = "pending"
PAGE_IN_PROGRESS = "in_progress"
PAGE_DONE = "done"
PAGE_FLAGGED = "flagged"
PAGE_FAILED = "failed"


SCHEMA = """
-- One row per PDF found under the configured root.
CREATE TABLE IF NOT EXISTS books (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,

    -- Path relative to pdf_root. This, not the absolute path, is the stable
    -- identity of a book: it survives the root folder being moved or the
    -- drive being remounted somewhere else.
    rel_path        TEXT    NOT NULL UNIQUE,

    file_name       TEXT    NOT NULL,
    title           TEXT    NOT NULL,

    -- Cheap fingerprint (or full SHA-256 if configured). Detects a PDF being
    -- replaced with a different file under the same name.
    fingerprint     TEXT    NOT NULL DEFAULT '',

    file_size       INTEGER NOT NULL DEFAULT 0,
    total_pages     INTEGER NOT NULL DEFAULT 0,

    -- Language/script/era profile detected once from the book's first pages,
    -- stored as JSON and injected into every page prompt for this book.
    -- NULL means "not profiled yet".
    profile_json    TEXT,

    created_at      REAL    NOT NULL,
    updated_at      REAL    NOT NULL
);

-- One row per page of every book. This table is the work queue.
CREATE TABLE IF NOT EXISTS pages (
    book_id             INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,

    -- 1-based page number within the PDF.
    page_no             INTEGER NOT NULL,

    status              TEXT    NOT NULL DEFAULT 'pending',

    -- --- Model output -------------------------------------------------
    page_type           TEXT,
    transcription       TEXT    NOT NULL DEFAULT '',
    footnotes           TEXT    NOT NULL DEFAULT '',

    -- The page number as PRINTED on the page, which is often different from
    -- the PDF page index (front matter, plates, mis-numbering). This is what
    -- makes scholarly citation possible later, and it cannot be recovered
    -- after the fact without re-reading every page.
    printed_page_number TEXT    NOT NULL DEFAULT '',

    languages           TEXT    NOT NULL DEFAULT '[]',
    has_uncertain_text  INTEGER NOT NULL DEFAULT 0,
    note                TEXT    NOT NULL DEFAULT '',

    -- --- Provenance ---------------------------------------------------
    -- In three years someone will ask which model produced a given passage
    -- and at what settings. Recording it costs nothing now and is impossible
    -- to reconstruct later.
    model               TEXT    NOT NULL DEFAULT '',
    prompt_version      TEXT    NOT NULL DEFAULT '',
    app_version         TEXT    NOT NULL DEFAULT '',
    media_resolution    TEXT    NOT NULL DEFAULT '',
    render_dpi          INTEGER NOT NULL DEFAULT 0,
    image_format        TEXT    NOT NULL DEFAULT '',
    machine_id          TEXT    NOT NULL DEFAULT '',
    key_label           TEXT    NOT NULL DEFAULT '',

    -- --- Bookkeeping --------------------------------------------------
    input_tokens        INTEGER NOT NULL DEFAULT 0,
    output_tokens       INTEGER NOT NULL DEFAULT 0,
    latency_ms          INTEGER NOT NULL DEFAULT 0,

    -- Only genuine errors increment this. Quota exhaustion and rate limiting
    -- do not, because they say nothing about the page itself.
    attempts            INTEGER NOT NULL DEFAULT 0,

    last_error          TEXT    NOT NULL DEFAULT '',
    flag_reason         TEXT    NOT NULL DEFAULT '',

    -- Set when a worker claims the page, cleared when it finishes. Stale
    -- values (from a killed process) are reset at startup.
    claimed_at          REAL,

    updated_at          REAL    NOT NULL DEFAULT 0,

    PRIMARY KEY (book_id, page_no)
);

-- The work queue is scanned by status constantly, so it needs an index.
CREATE INDEX IF NOT EXISTS idx_pages_status ON pages(status);
CREATE INDEX IF NOT EXISTS idx_pages_book_status ON pages(book_id, status);

-- API keys plus their live quota standing. NOTE: this table holds secrets,
-- which makes the whole database file sensitive.
CREATE TABLE IF NOT EXISTS api_keys (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    label                   TEXT    NOT NULL UNIQUE,
    secret                  TEXT    NOT NULL,
    rpm_limit               INTEGER NOT NULL DEFAULT 10,
    rpd_limit               INTEGER NOT NULL DEFAULT 250,
    state                   TEXT    NOT NULL DEFAULT 'active',

    -- The Pacific date that used_today refers to.
    used_on                 TEXT    NOT NULL DEFAULT '',
    used_today              INTEGER NOT NULL DEFAULT 0,

    cooldown_until          REAL    NOT NULL DEFAULT 0,
    last_used_at            REAL    NOT NULL DEFAULT 0,
    consecutive_rate_limits INTEGER NOT NULL DEFAULT 0,
    last_error              TEXT    NOT NULL DEFAULT '',
    enabled                 INTEGER NOT NULL DEFAULT 1,
    created_at              REAL    NOT NULL
);

-- Rolling activity log. Feeds the log console in the UI and gives you
-- something to read after an overnight run.
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL    NOT NULL,
    level       TEXT    NOT NULL DEFAULT 'info',
    message     TEXT    NOT NULL,
    book_id     INTEGER,
    page_no     INTEGER
);

CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts DESC);

-- Runtime settings editable from the web UI.
--
-- Deliberately narrow: engine tuning (DPI, resolution, worker counts) stays in
-- .env, because changing it mid-corpus would silently produce pages with
-- different provenance. Only operational settings that can change without
-- affecting output live here -- currently the Google Sheets connection.
CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  REAL NOT NULL
);
"""


class Database:
    """
    Thin wrapper around a SQLite connection pool.

    One connection per thread (SQLite connections are not thread-safe), created
    lazily and kept in thread-local storage. Callers just use the methods.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()

        # Serialises the multi-statement claim operation. SQLite handles
        # concurrency itself, but a Python-side lock keeps the claim logic
        # simple to read and reason about.
        self._claim_lock = threading.Lock()

        self._init_schema()

    # -----------------------------------------------------------------
    # Connection handling
    # -----------------------------------------------------------------

    @property
    def conn(self) -> sqlite3.Connection:
        """The calling thread's connection, created on first use."""
        existing = getattr(self._local, "conn", None)
        if existing is not None:
            return existing

        conn = sqlite3.connect(
            self.path,
            timeout=30.0,          # wait rather than fail if another thread is writing
            isolation_level=None,  # explicit transactions; no implicit BEGIN
        )
        conn.row_factory = sqlite3.Row

        # WAL lets the web UI read while workers write, instead of blocking.
        conn.execute("PRAGMA journal_mode=WAL")

        # NORMAL trades a tiny crash-durability window for a large speed gain.
        # Losing the last page or two on a power cut is acceptable here: the
        # page simply returns to `pending` and gets redone.
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")

        self._local.conn = conn
        return conn

    def _init_schema(self) -> None:
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """
        Add columns that older databases predate.

        `CREATE TABLE IF NOT EXISTS` does nothing to a table that already
        exists, so a database created by an earlier version keeps its old
        column set. Each migration here is additive and safe to run every time
        — no data is moved or rewritten, so an interrupted upgrade leaves
        nothing half-done.
        """
        wanted = [
            # (table, column, definition)
            ("api_keys", "enabled", "INTEGER NOT NULL DEFAULT 1"),
        ]

        for table, column, definition in wanted:
            existing = {
                row["name"]
                for row in self.conn.execute(f"PRAGMA table_info({table})")
            }
            if column not in existing:
                self.conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
                )

    def close(self) -> None:
        existing = getattr(self._local, "conn", None)
        if existing is not None:
            existing.close()
            self._local.conn = None

    # -----------------------------------------------------------------
    # Books
    # -----------------------------------------------------------------

    def upsert_book(
        self,
        rel_path: str,
        file_name: str,
        title: str,
        fingerprint: str,
        file_size: int,
        total_pages: int,
    ) -> tuple[int, bool]:
        """
        Register a book, or return the existing one.

        Returns (book_id, was_created).

        Re-scanning is safe and preserves progress: an already-known rel_path
        is left alone apart from refreshing its metadata. This is what makes
        "drive got remounted, rescan the folder" a non-event rather than a
        reason to redo thousands of pages.
        """
        now = time.time()

        row = self.conn.execute(
            "SELECT id FROM books WHERE rel_path = ?", (rel_path,)
        ).fetchone()

        if row is not None:
            self.conn.execute(
                """
                UPDATE books
                   SET file_name = ?, fingerprint = ?, file_size = ?,
                       total_pages = ?, updated_at = ?
                 WHERE id = ?
                """,
                (file_name, fingerprint, file_size, total_pages, now, row["id"]),
            )
            return int(row["id"]), False

        cursor = self.conn.execute(
            """
            INSERT INTO books
                (rel_path, file_name, title, fingerprint, file_size,
                 total_pages, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (rel_path, file_name, title, fingerprint, file_size,
             total_pages, now, now),
        )
        return int(cursor.lastrowid), True

    def create_pages_for_book(self, book_id: int, total_pages: int) -> int:
        """
        Create the page rows for a book, skipping any that already exist.

        Returns how many were newly created. `INSERT OR IGNORE` against the
        (book_id, page_no) primary key makes this idempotent, so a book whose
        PDF gained pages picks up only the new ones.
        """
        now = time.time()
        rows = [(book_id, n, PAGE_PENDING, now) for n in range(1, total_pages + 1)]

        before = self.count_pages_for_book(book_id)
        self.conn.executemany(
            """
            INSERT OR IGNORE INTO pages (book_id, page_no, status, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            rows,
        )
        return self.count_pages_for_book(book_id) - before

    def count_pages_for_book(self, book_id: int) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM pages WHERE book_id = ?", (book_id,)
        ).fetchone()
        return int(row["n"])

    def get_book(self, book_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM books WHERE id = ?", (book_id,)
        ).fetchone()

    def list_books(self) -> list[sqlite3.Row]:
        """
        Every book with its page counts rolled up.

        One query with conditional aggregation rather than a query per book,
        because the UI refreshes this constantly and there may be a thousand
        rows.
        """
        return self.conn.execute(
            """
            SELECT b.*,
                   COUNT(p.page_no)                                        AS pages_total,
                   SUM(CASE WHEN p.status = 'done'    THEN 1 ELSE 0 END)   AS pages_done,
                   SUM(CASE WHEN p.status = 'flagged' THEN 1 ELSE 0 END)   AS pages_flagged,
                   SUM(CASE WHEN p.status = 'failed'  THEN 1 ELSE 0 END)   AS pages_failed,
                   SUM(CASE WHEN p.status = 'pending' THEN 1 ELSE 0 END)   AS pages_pending
              FROM books b
              LEFT JOIN pages p ON p.book_id = b.id
             GROUP BY b.id
             ORDER BY b.rel_path
            """
        ).fetchall()

    def delete_book(self, book_id: int) -> int:
        """
        Remove a book and all its pages.

        The PDF on disk is untouched, so a later `scan` will register it again
        as new -- which is exactly what you want if you removed it to start it
        over.
        """
        return self.conn.execute(
            "DELETE FROM books WHERE id = ?", (book_id,)
        ).rowcount

    def set_book_profile(self, book_id: int, profile: dict) -> None:
        """Store the detected language/script/era profile for a book."""
        self.conn.execute(
            "UPDATE books SET profile_json = ?, updated_at = ? WHERE id = ?",
            (json.dumps(profile, ensure_ascii=False), time.time(), book_id),
        )

    def get_book_profile(self, book_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT profile_json FROM books WHERE id = ?", (book_id,)
        ).fetchone()

        if row is None or not row["profile_json"]:
            return None
        return json.loads(row["profile_json"])

    # -----------------------------------------------------------------
    # The work queue
    # -----------------------------------------------------------------

    def reset_stale_claims(self) -> int:
        """
        Return any `in_progress` pages to `pending`.

        Called once at startup. A claim only means something inside a live
        process; if we are starting up, every existing claim belongs to a
        process that is gone, and leaving them would strand those pages
        forever. This is the same reasoning as the original HTML app clearing
        book claims on reload.
        """
        cursor = self.conn.execute(
            """
            UPDATE pages
               SET status = 'pending', claimed_at = NULL
             WHERE status = 'in_progress'
            """
        )
        return cursor.rowcount

    def claim_next_page(self) -> sqlite3.Row | None:
        """
        Atomically take the next pending page and mark it in_progress.

        BEGIN IMMEDIATE takes the write lock up front, so the SELECT and the
        UPDATE cannot interleave with another worker's claim. Without this two
        threads would happily transcribe the same page and burn two requests
        of a scarce daily allowance for one result.

        Pages are handed out in (book_id, page_no) order, so books complete
        roughly in sequence and a part-finished corpus has whole books in it
        rather than every book being 40% done.
        """
        with self._claim_lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                row = self.conn.execute(
                    """
                    SELECT p.*, b.rel_path, b.title
                      FROM pages p
                      JOIN books b ON b.id = p.book_id
                     WHERE p.status = 'pending'
                     ORDER BY p.book_id, p.page_no
                     LIMIT 1
                    """
                ).fetchone()

                if row is None:
                    self.conn.execute("COMMIT")
                    return None

                self.conn.execute(
                    """
                    UPDATE pages
                       SET status = 'in_progress', claimed_at = ?
                     WHERE book_id = ? AND page_no = ?
                    """,
                    (time.time(), row["book_id"], row["page_no"]),
                )
                self.conn.execute("COMMIT")
                return row

            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    def release_page(self, book_id: int, page_no: int) -> None:
        """
        Put a claimed page back on the queue without recording an attempt.

        Used when the worker could not even try — no key was available, or the
        run was stopped. The page is untouched, so this must not count against
        max_page_attempts.
        """
        self.conn.execute(
            """
            UPDATE pages
               SET status = 'pending', claimed_at = NULL, updated_at = ?
             WHERE book_id = ? AND page_no = ?
            """,
            (time.time(), book_id, page_no),
        )

    def save_page_result(
        self,
        book_id: int,
        page_no: int,
        status: str,
        result: dict,
        provenance: dict,
        flag_reason: str = "",
    ) -> None:
        """
        Commit a successful transcription.

        Called immediately after each API response — this single commit is what
        makes the run crash-safe.
        """
        self.conn.execute(
            """
            UPDATE pages
               SET status              = ?,
                   page_type           = ?,
                   transcription       = ?,
                   footnotes           = ?,
                   printed_page_number = ?,
                   languages           = ?,
                   has_uncertain_text  = ?,
                   note                = ?,
                   model               = ?,
                   prompt_version      = ?,
                   app_version         = ?,
                   media_resolution    = ?,
                   render_dpi          = ?,
                   image_format        = ?,
                   machine_id          = ?,
                   key_label           = ?,
                   input_tokens        = ?,
                   output_tokens       = ?,
                   latency_ms          = ?,
                   flag_reason         = ?,
                   last_error          = '',
                   claimed_at          = NULL,
                   updated_at          = ?
             WHERE book_id = ? AND page_no = ?
            """,
            (
                status,
                result.get("page_type", ""),
                result.get("transcription", ""),
                result.get("footnotes", ""),
                str(result.get("printed_page_number", "") or ""),
                json.dumps(result.get("languages", []), ensure_ascii=False),
                1 if result.get("has_uncertain_text") else 0,
                result.get("note", ""),
                provenance.get("model", ""),
                provenance.get("prompt_version", ""),
                provenance.get("app_version", ""),
                provenance.get("media_resolution", ""),
                provenance.get("render_dpi", 0),
                provenance.get("image_format", ""),
                provenance.get("machine_id", ""),
                provenance.get("key_label", ""),
                provenance.get("input_tokens", 0),
                provenance.get("output_tokens", 0),
                provenance.get("latency_ms", 0),
                flag_reason,
                time.time(),
                book_id,
                page_no,
            ),
        )

    def record_page_failure(
        self,
        book_id: int,
        page_no: int,
        error: str,
        max_attempts: int,
    ) -> str:
        """
        Record a genuine failure against a page and decide its next state.

        Returns the resulting status: 'pending' if attempts remain, 'failed' if
        they are used up.

        Only call this for errors that tell us something about the page — a
        malformed response, an unreadable render, a rejected request. Never for
        quota or throttling.
        """
        row = self.conn.execute(
            "SELECT attempts FROM pages WHERE book_id = ? AND page_no = ?",
            (book_id, page_no),
        ).fetchone()

        attempts = (int(row["attempts"]) if row else 0) + 1
        status = PAGE_FAILED if attempts >= max_attempts else PAGE_PENDING

        self.conn.execute(
            """
            UPDATE pages
               SET status = ?, attempts = ?, last_error = ?,
                   claimed_at = NULL, updated_at = ?
             WHERE book_id = ? AND page_no = ?
            """,
            (status, attempts, error[:1000], time.time(), book_id, page_no),
        )
        return status

    def requeue_pages(self, statuses: Iterable[str], book_id: int | None = None) -> int:
        """
        Put pages of the given statuses back on the queue, clearing their
        attempt counters.

        This is what the "retry failed" and "review flagged" buttons call.
        Deliberately manual: on the free tier a retry costs a fresh request,
        so an automatic retry loop could quietly eat a whole day's allowance.
        """
        statuses = list(statuses)
        if not statuses:
            return 0

        placeholders = ",".join("?" for _ in statuses)
        params: list[Any] = list(statuses)

        sql = f"""
            UPDATE pages
               SET status = 'pending', attempts = 0, last_error = '',
                   flag_reason = '', claimed_at = NULL, updated_at = ?
             WHERE status IN ({placeholders})
        """
        params = [time.time()] + params

        if book_id is not None:
            sql += " AND book_id = ?"
            params.append(book_id)

        return self.conn.execute(sql, params).rowcount

    def pages_for_book(self, book_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM pages WHERE book_id = ? ORDER BY page_no", (book_id,)
        ).fetchall()

    def queue_summary(self) -> dict:
        """Page counts by status across the whole corpus."""
        rows = self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM pages GROUP BY status"
        ).fetchall()

        summary = {
            PAGE_PENDING: 0,
            PAGE_IN_PROGRESS: 0,
            PAGE_DONE: 0,
            PAGE_FLAGGED: 0,
            PAGE_FAILED: 0,
        }
        for row in rows:
            summary[row["status"]] = int(row["n"])

        summary["total"] = sum(
            v for k, v in summary.items() if k != "total"
        )
        return summary

    # -----------------------------------------------------------------
    # API keys
    # -----------------------------------------------------------------

    def add_key(
        self,
        label: str,
        secret: str,
        rpm_limit: int = 10,
        rpd_limit: int = 250,
        enabled: bool = True,
    ) -> int:
        """
        Add or update a key by label.

        Labels are the stable identity, so re-importing your seed file updates
        secrets and limits without resetting today's usage counters.
        """
        row = self.conn.execute(
            "SELECT id FROM api_keys WHERE label = ?", (label,)
        ).fetchone()

        if row is not None:
            self.conn.execute(
                """
                UPDATE api_keys
                   SET secret = ?, rpm_limit = ?, rpd_limit = ?, enabled = ?
                 WHERE id = ?
                """,
                (secret, rpm_limit, rpd_limit, 1 if enabled else 0, row["id"]),
            )
            return int(row["id"])

        cursor = self.conn.execute(
            """
            INSERT INTO api_keys
                (label, secret, rpm_limit, rpd_limit, enabled, used_on, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (label, secret, rpm_limit, rpd_limit, 1 if enabled else 0,
             pacific_date(), time.time()),
        )
        return int(cursor.lastrowid)

    def load_keys(self) -> list[ApiKey]:
        """Load all keys as ApiKey objects for the pool."""
        rows = self.conn.execute("SELECT * FROM api_keys ORDER BY id").fetchall()

        return [
            ApiKey(
                id=int(r["id"]),
                label=r["label"],
                secret=r["secret"],
                rpm_limit=int(r["rpm_limit"]),
                rpd_limit=int(r["rpd_limit"]),
                state=r["state"],
                used_on=r["used_on"],
                used_today=int(r["used_today"]),
                cooldown_until=float(r["cooldown_until"]),
                last_used_at=float(r["last_used_at"]),
                consecutive_rate_limits=int(r["consecutive_rate_limits"]),
                last_error=r["last_error"],
                enabled=bool(r["enabled"]),
            )
            for r in rows
        ]

    def save_key(self, key: ApiKey) -> None:
        """
        Persist a key's quota standing.

        Wired up as the KeyPool's on_change callback, so the pool never needs
        to know SQLite exists.
        """
        self.conn.execute(
            """
            UPDATE api_keys
               SET rpm_limit = ?, rpd_limit = ?, state = ?, used_on = ?,
                   used_today = ?, cooldown_until = ?, last_used_at = ?,
                   consecutive_rate_limits = ?, last_error = ?, enabled = ?
             WHERE id = ?
            """,
            (
                key.rpm_limit, key.rpd_limit, key.state, key.used_on,
                key.used_today, key.cooldown_until, key.last_used_at,
                key.consecutive_rate_limits, key.last_error,
                1 if key.enabled else 0, key.id,
            ),
        )

    def delete_key(self, label: str) -> int:
        return self.conn.execute(
            "DELETE FROM api_keys WHERE label = ?", (label,)
        ).rowcount

    # -----------------------------------------------------------------
    # Runtime settings
    # -----------------------------------------------------------------

    def get_setting(self, key: str, default: str = "") -> str:
        row = self.conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        self.conn.execute(
            """
            INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                          updated_at = excluded.updated_at
            """,
            (key, value, time.time()),
        )

    def all_settings(self) -> dict:
        return {
            row["key"]: row["value"]
            for row in self.conn.execute("SELECT key, value FROM settings")
        }

    # -----------------------------------------------------------------
    # Event log
    # -----------------------------------------------------------------

    def log(
        self,
        message: str,
        level: str = "info",
        book_id: int | None = None,
        page_no: int | None = None,
    ) -> None:
        """Append one line to the activity log."""
        self.conn.execute(
            """
            INSERT INTO events (ts, level, message, book_id, page_no)
            VALUES (?, ?, ?, ?, ?)
            """,
            (time.time(), level, message, book_id, page_no),
        )

    def recent_events(self, limit: int = 200, after_id: int = 0) -> list[sqlite3.Row]:
        """
        Recent log lines, newest first.

        `after_id` lets the UI poll for only what it has not seen yet instead
        of refetching the whole tail every second.
        """
        if after_id:
            return self.conn.execute(
                """
                SELECT * FROM events WHERE id > ?
                 ORDER BY id DESC LIMIT ?
                """,
                (after_id, limit),
            ).fetchall()

        return self.conn.execute(
            "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()

    def trim_events(self, keep: int = 5000) -> int:
        """
        Drop the oldest log rows.

        A long run generates a lot of these and none of it is precious; the
        durable record is the pages table.
        """
        return self.conn.execute(
            """
            DELETE FROM events
             WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT ?)
            """,
            (keep,),
        ).rowcount
