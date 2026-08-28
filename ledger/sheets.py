"""
Ledger — Google Sheets publishing.

This is the manual step: transcribe locally into SQLite, then push to the sheet
when you choose to. There is no live sync, and that is deliberate — per-page
syncing meant hundreds of thousands of round-trips to script.google.com, burned
Apps Script's own quota, and duplicated rows on every retry.

Publishing sends pages in chunks, each addressed by page number, so the whole
operation is repeatable: run it twice and the sheet is identical.

It runs on a background thread with observable progress, because a large corpus
takes minutes and the UI must not block on it.
"""

from __future__ import annotations

import json
import threading
import time

import requests

from .appsscript import SCRIPT_VERSION


# Pages per request. Small enough to stay well inside Apps Script's execution
# time and payload limits even when pages are dense, large enough that a
# thousand-page book is a couple of dozen requests rather than a thousand.
CHUNK_SIZE = 150

# Apps Script can be slow to wake a cold deployment.
REQUEST_TIMEOUT = 120


class SheetsError(Exception):
    """Anything that went wrong talking to the Apps Script deployment."""


def _call(url: str, secret: str, action: str, payload: dict | None = None) -> dict:
    """
    POST one action to the Apps Script web app.

    Content-Type is text/plain rather than application/json on purpose: that
    keeps it a CORS "simple request" so script.google.com never has to answer a
    preflight. Apps Script parses the body itself either way.
    """
    body = dict(payload or {})
    body["action"] = action
    body["secret"] = secret or ""

    try:
        response = requests.post(
            url,
            headers={"Content-Type": "text/plain;charset=utf-8"},
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise SheetsError(f"Could not reach the Apps Script URL: {exc}") from exc

    if not response.ok:
        raise SheetsError(f"Apps Script returned HTTP {response.status_code}")

    try:
        result = response.json()
    except ValueError:
        # Apps Script serves an HTML error page when the deployment is
        # misconfigured, which is the single most common setup mistake.
        raise SheetsError(
            "Apps Script did not return JSON. Check the deployment is a Web "
            "app with access set to Anyone, and that the URL ends in /exec."
        )

    if not result.get("ok"):
        raise SheetsError(result.get("error") or "Apps Script reported an error.")

    return result


def test_connection(url: str, secret: str) -> dict:
    """
    Ping the deployment.

    Also compares the deployed script version against the one this build
    expects, because a stale deployment is otherwise invisible until you notice
    the sheet looks wrong.
    """
    result = _call(url, secret, "ping")
    deployed = str(result.get("version") or "1")

    return {
        "connected": True,
        "deployed_version": deployed,
        "expected_version": SCRIPT_VERSION,
        "up_to_date": deployed == SCRIPT_VERSION,
    }


def _page_to_row(row) -> list:
    """
    One sheet row for one page. Column order must match HEADERS in the Apps
    Script.

    Blank and illustration-only pages are marked rather than left empty, so a
    gap in the sheet always means "not transcribed" rather than "nothing on the
    page" — an ambiguity that would be impossible to resolve later.
    """
    page_type = row["page_type"] or ""
    text = row["transcription"] or ""

    if not text:
        if page_type == "blank":
            text = "[BLANK]"
        elif page_type == "illustration_only":
            text = "[IMAGE]"
        elif page_type == "unreadable":
            text = "[UNREADABLE]"

    return [
        int(row["page_no"]),
        row["printed_page_number"] or "",
        page_type,
        text,
        row["footnotes"] or "",
        row["note"] or row["flag_reason"] or "",
        row["status"],
    ]


class PublishJob:
    """
    A background publish, with progress the UI can poll.

    One job at a time per process; the API refuses to start a second while one
    is running.
    """

    def __init__(self, config, database, url: str, secret: str, book_id=None):
        self.config = config
        self.db = database
        self.url = url
        self.secret = secret
        self.book_id = book_id

        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._cancel = False

        self.state = "idle"          # idle | running | done | failed | cancelled
        self.books_total = 0
        self.books_done = 0
        self.pages_written = 0
        self.current_book = ""
        self.error = ""
        self.started_at = 0.0
        self.finished_at = 0.0

    # -----------------------------------------------------------------

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "state": self.state,
                "books_total": self.books_total,
                "books_done": self.books_done,
                "pages_written": self.pages_written,
                "current_book": self.current_book,
                "error": self.error,
                "elapsed": int(
                    (self.finished_at or time.time()) - self.started_at
                ) if self.started_at else 0,
            }

    def start(self) -> None:
        with self._lock:
            if self.state == "running":
                return
            self.state = "running"
            self.started_at = time.time()
            self.finished_at = 0.0
            self.error = ""
            self.books_done = 0
            self.pages_written = 0
            self._cancel = False

        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        with self._lock:
            self._cancel = True

    # -----------------------------------------------------------------

    def _run(self) -> None:
        try:
            books = self.db.list_books()
            if self.book_id is not None:
                books = [b for b in books if int(b["id"]) == int(self.book_id)]

            with self._lock:
                self.books_total = len(books)

            for book in books:
                with self._lock:
                    if self._cancel:
                        self.state = "cancelled"
                        self.finished_at = time.time()
                        self.db.log("Publish cancelled.", "warn")
                        return
                    self.current_book = book["title"]

                written = self._publish_book(book)

                with self._lock:
                    self.books_done += 1
                    self.pages_written += written

            with self._lock:
                self.state = "done"
                self.current_book = ""
                self.finished_at = time.time()

            self.db.log(
                f"Published {self.pages_written} page(s) across "
                f"{self.books_done} book(s) to the sheet.",
                "ok",
            )

        except Exception as exc:
            with self._lock:
                self.state = "failed"
                self.error = str(exc)
                self.finished_at = time.time()
            self.db.log(f"Publish failed: {exc}", "err")

    def _publish_book(self, book) -> int:
        """
        Push one book's pages, in chunks.

        Only pages that actually have a result are sent. Pending pages are
        skipped rather than written as blanks, so a partially transcribed book
        publishes cleanly and can be republished later once it finishes.
        """
        book_id = int(book["id"])
        rows = [
            _page_to_row(page)
            for page in self.db.pages_for_book(book_id)
            if page["status"] in ("done", "flagged")
        ]

        if not rows:
            return 0

        # A stable tab name per book. The id prefix guarantees two books with
        # the same title never collide in the same tab.
        tab = f"{book_id:04d} {book['title']}"[:95]

        chunks = [rows[i : i + CHUNK_SIZE] for i in range(0, len(rows), CHUNK_SIZE)]

        for index, chunk in enumerate(chunks):
            with self._lock:
                if self._cancel:
                    return 0

            _call(
                self.url,
                self.secret,
                "syncChunk",
                {
                    "book": book["title"],
                    "tab": tab,
                    "rows": chunk,
                    "totalPages": int(book["total_pages"]),
                    "totalWritten": len(rows),
                    "machine": self.config.machine_id,
                    "isFinalChunk": index == len(chunks) - 1,
                },
            )

        self.db.log(
            f"Published “{book['title']}”: {len(rows)} page(s) in "
            f"{len(chunks)} chunk(s).",
            "ok",
            book_id=book_id,
        )
        return len(rows)
