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

# Google Sheets refuses more than 50,000 characters in a single cell. 45,000
# leaves room for the continuation marker appended to each split part.
MAX_CELL_CHARS = 45_000

# How many adjacent columns a single page's transcription may spill across.
#
# Overflow goes SIDEWAYS, never into extra rows. Every page is written to the
# row matching its page number (row = page + 1), and that is the whole reason
# republishing is safe: write the same page twice and it lands in the same
# cells. Continuation rows would break that mapping and reintroduce the
# duplicate-row problem the batched publisher exists to solve.
#
# Four columns holds 180,000 characters. For scale, the densest page in this
# corpus is about 27,000, and the model's own output ceiling puts a hard limit
# well inside this. If a page somehow exceeds even that, the last cell is
# truncated with a marker rather than failing the upload.
TRANSCRIPTION_COLUMNS = 4

# Total characters allowed in one request to Apps Script.
#
# Chunks are bounded by characters as well as row count now: 150 ordinary pages
# is a modest payload, but 150 pages that each need four cells would not be, and
# a request too large to process would fail the whole chunk.
MAX_CHUNK_CHARS = 2_000_000


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


def build_tab_names(books) -> dict:
    """
    Work out one tab name per book, keyed by book id.

    The tab is simply the PDF's name: `010.pdf` becomes a tab called `010`.

    The only complication is that two different PDFs in different folders can
    share a filename, and two tabs cannot share a name. Where that happens the
    id is appended to the duplicates *only* — so in the normal case every tab is
    the clean name, and a collision is disambiguated rather than silently
    overwriting another book's transcriptions.
    """
    counts: dict[str, int] = {}
    for book in books:
        counts[book["title"]] = counts.get(book["title"], 0) + 1

    names = {}
    for book in books:
        title = book["title"]
        if counts[title] == 1:
            names[int(book["id"])] = title[:95]
        else:
            # Ambiguous name — keep it readable but make it unique.
            names[int(book["id"])] = f"{title} ({int(book['id'])})"[:95]
    return names


def split_for_cells(
    text: str,
    columns: int = TRANSCRIPTION_COLUMNS,
    limit: int = MAX_CELL_CHARS,
) -> tuple[list[str], bool]:
    """
    Split one page's text across up to `columns` cells.

    Returns (parts, was_truncated). `parts` is always exactly `columns` long,
    padded with empty strings, so every row has the same width and the range
    write stays rectangular.

    Splits are made at the last line break before the limit, falling back to
    the last space, so a cell never ends mid-word and each part starts on a
    clean line. Reassembling the page is then a plain concatenation of the
    cells left to right — in the sheet itself, `=C2&D2&E2&F2`.
    """
    if len(text) <= limit:
        return [text] + [""] * (columns - 1), False

    parts: list[str] = []
    remaining = text

    while remaining and len(parts) < columns:
        if len(remaining) <= limit:
            parts.append(remaining)
            remaining = ""
            break

        window = remaining[:limit]

        # Prefer a paragraph or line boundary, then a word boundary. Only fall
        # back to a hard cut if the text contains neither, which in practice
        # means it is not prose at all.
        cut = window.rfind("\n")
        if cut < limit // 2:
            cut = window.rfind(" ")
        if cut < limit // 2:
            cut = limit

        parts.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")

    truncated = bool(remaining)
    if truncated:
        # Every column is full and there is still text left. Mark the last cell
        # so the sheet never silently misrepresents the page.
        #
        # The slice reserves exactly the marker's own length, measured rather
        # than guessed — a hardcoded margin smaller than the marker would push
        # the cell back over the limit, which is the very thing being avoided.
        marker = (
            "\n\n[TRUNCATED — this page exceeds what a sheet row can hold. "
            "Full text is in the JSON and .txt exports.]"
        )
        parts[-1] = parts[-1][: limit - len(marker)] + marker

    parts += [""] * (columns - len(parts))
    return parts, truncated


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

    parts, truncated = split_for_cells(text)

    # Footnotes get the same protection; a single cell's worth is ample for
    # them, so they are capped rather than spread.
    footnotes = row["footnotes"] or ""
    if len(footnotes) > MAX_CELL_CHARS:
        footnotes = footnotes[: MAX_CELL_CHARS - 40] + "\n[TRUNCATED — see exports]"

    return (
        [int(row["page_no"]), page_type]
        + parts
        + [footnotes, row["note"] or row["flag_reason"] or "", row["status"]]
    ), truncated


def _build_chunks(rows: list[list]) -> list[list[list]]:
    """
    Group rows into requests, bounded by BOTH row count and total characters.

    Row count alone is not enough once a page can occupy four cells: 150
    ordinary pages is a small payload, but 150 very long ones would be large
    enough to fail, and a failed chunk fails the whole book.
    """
    chunks: list[list[list]] = []
    current: list[list] = []
    current_chars = 0

    for row in rows:
        row_chars = sum(len(cell) for cell in row if isinstance(cell, str))

        too_many_rows = len(current) >= CHUNK_SIZE
        too_many_chars = current and current_chars + row_chars > MAX_CHUNK_CHARS

        if too_many_rows or too_many_chars:
            chunks.append(current)
            current = []
            current_chars = 0

        current.append(row)
        current_chars += row_chars

    if current:
        chunks.append(current)

    return chunks


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

        self._tab_names: dict = {}

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
            all_books = self.db.list_books()

            # Names are worked out across the WHOLE library, not just the books
            # being published, so a duplicate filename is spotted even when only
            # one of the pair is in this run.
            self._tab_names = build_tab_names(all_books)

            books = all_books
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

        rows = []
        split_pages = 0
        truncated_pages = []

        for page in self.db.pages_for_book(book_id):
            if page["status"] not in ("done", "flagged"):
                continue

            row, truncated = _page_to_row(page)
            rows.append(row)

            # Columns 2..2+TRANSCRIPTION_COLUMNS hold the transcription.
            if any(row[3 : 2 + TRANSCRIPTION_COLUMNS]):
                split_pages += 1
            if truncated:
                truncated_pages.append(int(page["page_no"]))

        if not rows:
            return 0

        if split_pages:
            self.db.log(
                f"“{book['title']}”: {split_pages} page(s) exceeded the 50,000 "
                "character cell limit and were spread across the Transcription "
                "columns. Rejoin them in the sheet with =C2&D2&E2&F2.",
                "info",
                book_id=book_id,
            )

        if truncated_pages:
            self.db.log(
                f"“{book['title']}”: page(s) {truncated_pages} are too long "
                "even for four cells and were truncated in the sheet. The full "
                "text is in the JSON and .txt exports. A page this long is "
                "worth checking — it is usually a dense index or a repetition "
                "loop.",
                "warn",
                book_id=book_id,
            )

        tab = self._tab_names.get(book_id, book["title"][:95])

        chunks = _build_chunks(rows)

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
