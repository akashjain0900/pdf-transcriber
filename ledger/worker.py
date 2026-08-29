"""
Ledger — the transcription engine.

One worker thread per usable API key. Each thread loops:

    claim a pending page  ->  get a key  ->  render  ->  call Gemini  ->  commit

Two design choices are worth explaining, because both differ from the original
HTML app and both matter.

**No fixed delay between requests.** The old app slept 4.5 seconds after every
page. That made sense as a guess at avoiding 429s, but on the free tier the
binding constraint is requests-per-DAY, not per-minute. Spacing requests out
does not buy you more pages -- it just spreads the same daily allowance across
more wall-clock hours. So pacing here is per-key and derived from each key's
actual RPM limit (see quota.py), and workers otherwise sprint until their key
hits the daily wall and then park until the Pacific reset. Twenty keys will
burn a day's allowance in well under an hour. That is the correct behaviour:
better finished-and-idle than slow-and-idle.

**Pages are claimed individually, not whole books.** The old app gave one book
to one key at a time, which left keys idle whenever there were fewer books in
flight than keys. Claiming per page keeps every key busy, and since pages are
handed out in (book, page) order the corpus still fills in roughly book by book.
"""

from __future__ import annotations

import re
import threading
import time
from pathlib import Path

from .config import APP_VERSION
from .db import PAGE_DONE, PAGE_FLAGGED, Database
from .gemini import (
    BadResponse,
    GeminiClient,
    KeyRejected,
    RateLimited,
    TransientError,
)
from .prompts import ILLEGIBLE_MARKER
from .quota import KeyPool
from .render import RenderError, render_page, render_pages, select_text_pages


# ---------------------------------------------------------------------
# Quality heuristics
# ---------------------------------------------------------------------
#
# These run locally on the returned text and cost nothing. Their job is to
# catch the failure modes where the API said "200 OK" but the result is not
# usable, so those pages get parked as `flagged` for review instead of being
# silently accepted into the corpus.
#
# Nothing here retries automatically. On the free tier a retry costs a fresh
# request, so draining the flagged queue is an explicit decision you make (see
# Database.requeue_pages), not something the engine does behind your back.

# A "text" page with less than this many characters is suspicious -- either the
# classification is wrong or the model gave up partway.
MIN_TEXT_LENGTH = 20

# If more than this share of the page is illegible markers, the render or the
# scan is probably the problem rather than the page being genuinely damaged.
MAX_ILLEGIBLE_RATIO = 0.25


def _has_degenerate_repetition(text: str, window: int = 40, threshold: int = 4) -> bool:
    """
    Detect the loop a model can fall into where it emits the same phrase over
    and over until it hits the output limit.

    Cheap check: take a window from the middle of the text and count its
    occurrences. A genuine page essentially never repeats a 40-character span
    four times.
    """
    if len(text) < window * threshold:
        return False

    midpoint = len(text) // 2
    probe = text[midpoint : midpoint + window]

    if not probe.strip():
        return False

    return text.count(probe) >= threshold


def assess_quality(result: dict) -> tuple[bool, str]:
    """
    Decide whether a transcription looks trustworthy.

    Returns (is_ok, reason). An empty reason means it passed.
    """
    page_type = (result.get("page_type") or "").strip()
    transcription = result.get("transcription") or ""
    footnotes = result.get("footnotes") or ""
    combined = (transcription + footnotes).strip()

    # Blank and illustration-only pages are SUPPOSED to be empty. Not a fault.
    if page_type in {"blank", "illustration_only"}:
        return True, ""

    # An unreadable page is a legitimate, informative result.
    if page_type == "unreadable":
        return True, ""

    if page_type == "text" and len(combined) < MIN_TEXT_LENGTH:
        return False, (
            f"Classified as text but only {len(combined)} characters were "
            "returned"
        )

    if _has_degenerate_repetition(combined):
        return False, "Output contains a repeated phrase loop"

    if combined:
        illegible_chars = combined.count(ILLEGIBLE_MARKER) * len(ILLEGIBLE_MARKER)
        if illegible_chars / len(combined) > MAX_ILLEGIBLE_RATIO:
            return False, (
                "More than a quarter of the page was marked illegible"
            )

    # Control characters usually mean an encoding problem somewhere in the
    # chain rather than anything on the page.
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", combined):
        return False, "Output contains control characters"

    return True, ""


# ---------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------

class Engine:
    """
    Owns the worker threads and the shared resources they need.

    Lifecycle:

        engine = Engine(config, database)
        engine.start()      # non-blocking; spawns threads
        ...
        engine.stop()       # signals; workers finish their current page
        engine.join()

    Safe to start and stop repeatedly, which is what the UI's start/pause
    button does.
    """

    def __init__(self, config, database: Database):
        self.config = config
        self.db = database
        self.client = GeminiClient(config)

        # The pool persists every state change straight back to the database
        # via this callback, so quota accounting survives a restart.
        self.pool = KeyPool(database.load_keys(), on_change=database.save_key)

        self._running = False
        self._threads: list[threading.Thread] = []

        # Rendering is CPU-bound, unlike the API call. Without this, twenty
        # threads would rasterise 300 DPI pages simultaneously and starve the
        # machine while the network sat idle.
        self._render_slots = threading.Semaphore(config.max_concurrent_renders)

        # Guards the per-book profile locks and failure counts below.
        self._profile_lock = threading.Lock()
        self._book_locks: dict[int, threading.Lock] = {}

        # Failed profile attempts per book, for this process only.
        #
        # A transient failure must not be cached permanently — we want it to
        # succeed once the network recovers. But retrying on every single page
        # of a book during an outage floods the log and burns an acquire each
        # time, so after a few failures we stop trying for this book until the
        # process restarts.
        self._profile_failures: dict[int, int] = {}

    # -----------------------------------------------------------------
    # Logging
    # -----------------------------------------------------------------

    def log(self, message: str, level: str = "info", book_id=None, page_no=None):
        """Write to the shared event log, and to stdout for CLI runs."""
        self.db.log(message, level=level, book_id=book_id, page_no=page_no)
        print(f"[{level}] {message}", flush=True)

    # -----------------------------------------------------------------
    # Book profiling
    # -----------------------------------------------------------------

    # After this many failed attempts, stop trying to profile this book for
    # the rest of the session and just transcribe without a profile.
    PROFILE_ATTEMPT_LIMIT = 3

    def _note_profile_failure(self, book_id: int) -> int:
        """Count a failed profile attempt and return the new total."""
        with self._profile_lock:
            self._profile_failures[book_id] = (
                self._profile_failures.get(book_id, 0) + 1
            )
            return self._profile_failures[book_id]

    def _profile_given_up(self, book_id: int) -> bool:
        with self._profile_lock:
            return (
                self._profile_failures.get(book_id, 0) >= self.PROFILE_ATTEMPT_LIMIT
            )

    def _book_lock(self, book_id: int) -> threading.Lock:
        """One lock per book, created on demand."""
        with self._profile_lock:
            if book_id not in self._book_locks:
                self._book_locks[book_id] = threading.Lock()
            return self._book_locks[book_id]

    def _ensure_profile(self, book_id: int, pdf_path: Path) -> dict | None:
        """
        Get this book's language/script/era profile, detecting it if needed.

        Serialised per book, because without the lock several workers starting
        on the same book at once would each spend a request profiling it. With
        a scarce daily allowance that waste is worth a few lines of code to
        avoid.

        A profiling failure is never fatal: we log it and transcribe with no
        profile, which is exactly the situation describe_profile() handles by
        declining to assume a language or century.
        """
        if not self.config.profile_books:
            return None

        existing = self.db.get_book_profile(book_id)
        if existing is not None:
            return existing

        # Already tried and failed enough times this session.
        if self._profile_given_up(book_id):
            return None

        with self._book_lock(book_id):
            # Another worker may have profiled it while we waited.
            existing = self.db.get_book_profile(book_id)
            if existing is not None:
                return existing

            # Choose the sample pages BEFORE acquiring a key. This runs locally
            # on low-resolution renders and costs no API requests, so there is
            # no reason to hold a key while doing it — and if the book turns out
            # to have no text pages at all, we never spend a request.
            try:
                with self._render_slots:
                    sample_pages, strategy = select_text_pages(
                        pdf_path,
                        count=self.config.profile_sample_pages,
                        start_after_page=self.config.profile_start_after_page,
                        scan_limit=self.config.profile_scan_limit,
                    )
            except RenderError as exc:
                self.log(
                    f"Could not inspect pages for profiling: {exc}",
                    "warn",
                    book_id=book_id,
                )
                return None

            if not sample_pages:
                # Every page inspected was blank or a plate. Record an empty
                # profile so we do not re-inspect this book on every page, and
                # transcribe without one — describe_profile() handles that by
                # declining to assume a language or century.
                self.db.set_book_profile(book_id, {})
                self.log(
                    "No text pages found to profile from; transcribing without "
                    "a book profile.",
                    "warn",
                    book_id=book_id,
                )
                return None

            key = self.pool.acquire()
            if key is None:
                # No quota for the profile right now. Return None so the page
                # still gets transcribed; the profile will be picked up on a
                # later page of this book.
                return None

            try:
                with self._render_slots:
                    images = render_pages(
                        pdf_path,
                        sample_pages,
                        dpi=self.config.dpi,
                        greyscale=self.config.greyscale,
                        image_format=self.config.image_format,
                    )

                profile = self.client.profile_book(key.secret, images)
                self.pool.record_success(key)

                # Record which pages the profile came from and how they were
                # chosen. A profile built from fallback pages deserves less
                # trust than one built as intended, and that is impossible to
                # tell later unless we write it down now.
                profile["_sample_pages"] = sample_pages
                profile["_sample_strategy"] = strategy

                self.db.set_book_profile(book_id, profile)

                described = ", ".join(
                    str(profile.get(field))
                    for field in ("primary_language", "script", "era")
                    if profile.get(field)
                )
                self.log(
                    f"Profiled book from pages {sample_pages} ({strategy}): "
                    f"{described}",
                    "ok",
                    book_id=book_id,
                )
                return profile

            except RateLimited as exc:
                # Quota, not a profiling problem. Does not count an attempt:
                # the profile should still be tried once quota returns.
                self.pool.record_rate_limited(key, str(exc), exc.retry_after)
                return None
            except KeyRejected as exc:
                self.pool.record_dead(key, str(exc))
                self.log(f"Key “{key.label}” rejected: {exc}", "err")
                return None
            except (TransientError, BadResponse, RenderError) as exc:
                # Profiling is a nice-to-have. Never let it block the run.
                attempts = self._note_profile_failure(book_id)

                if attempts >= self.PROFILE_ATTEMPT_LIMIT:
                    self.log(
                        f"Giving up on profiling this book after {attempts} "
                        f"attempts; transcribing without a profile. Last error: "
                        f"{exc}",
                        "warn",
                        book_id=book_id,
                    )
                else:
                    self.log(
                        f"Could not profile book (attempt {attempts} of "
                        f"{self.PROFILE_ATTEMPT_LIMIT}), continuing without a "
                        f"profile: {exc}",
                        "warn",
                        book_id=book_id,
                    )
                return None

    # -----------------------------------------------------------------
    # One page
    # -----------------------------------------------------------------

    def _process_page(self, page_row) -> str:
        """
        Render and transcribe a single claimed page.

        Returns a short outcome string for the caller's flow control:

            "done"      -- committed (whether accepted or flagged)
            "no-quota"  -- no key available; page released untouched
            "retry"     -- genuine failure recorded; page requeued or failed
        """
        book_id = int(page_row["book_id"])
        page_no = int(page_row["page_no"])
        rel_path = page_row["rel_path"]
        title = page_row["title"]

        pdf_path = Path(self.config.pdf_root) / rel_path

        # Profile first: it may consume a key, and we want the page prompt to
        # carry the profile if one is available.
        profile = self._ensure_profile(book_id, pdf_path)

        key = self.pool.acquire()
        if key is None:
            self.db.release_page(book_id, page_no)
            return "no-quota"

        # --- Render ---------------------------------------------------
        try:
            with self._render_slots:
                image_bytes = render_page(
                    pdf_path,
                    page_no,
                    dpi=self.config.dpi,
                    greyscale=self.config.greyscale,
                    image_format=self.config.image_format,
                    deskew=self.config.deskew,
                )
        except RenderError as exc:
            # A render failure is about this page (or the file), so it counts
            # as an attempt. No key was spent, so no quota is recorded.
            status = self.db.record_page_failure(
                book_id, page_no, str(exc), self.config.max_page_attempts
            )
            self.log(
                f"{title} p.{page_no} render failed ({status}): {exc}",
                "err",
                book_id=book_id,
                page_no=page_no,
            )
            return "retry"

        # --- Transcribe -----------------------------------------------
        try:
            result = self.client.transcribe_page(key.secret, image_bytes, profile)

        except RateLimited as exc:
            # Quota, not the page. Release it untouched and let the pool decide
            # whether this key is throttled or finished for the day.
            outcome = self.pool.record_rate_limited(key, str(exc), exc.retry_after)
            self.db.release_page(book_id, page_no)
            self.log(
                f"Key “{key.label}” rate limited → {outcome}",
                "warn",
            )
            return "no-quota"

        except KeyRejected as exc:
            self.pool.record_dead(key, str(exc))
            self.db.release_page(book_id, page_no)
            self.log(
                f"Key “{key.label}” has been rejected and retired: {exc}",
                "err",
            )
            return "no-quota"

        except TransientError as exc:
            # The service's problem. Short cooldown, page untouched.
            self.pool.record_transient_error(key, str(exc))
            self.db.release_page(book_id, page_no)
            self.log(f"Transient API error, will retry: {exc}", "warn")
            return "retry"

        except BadResponse as exc:
            # The request was spent, so record the usage, but the fault lies
            # with this page or the prompt.
            self.pool.record_success(key)
            status = self.db.record_page_failure(
                book_id, page_no, str(exc), self.config.max_page_attempts
            )
            self.log(
                f"{title} p.{page_no} bad response ({status}): {exc}",
                "err",
                book_id=book_id,
                page_no=page_no,
            )
            return "retry"

        # --- Commit ---------------------------------------------------
        self.pool.record_success(key)

        is_ok, flag_reason = assess_quality(result)
        status = PAGE_DONE if is_ok else PAGE_FLAGGED

        provenance = {
            "model": self.config.model,
            "prompt_version": result.get("_prompt_version", ""),
            "app_version": APP_VERSION,
            "media_resolution": self.config.media_resolution,
            "render_dpi": self.config.dpi,
            "image_format": self.config.image_format,
            "machine_id": self.config.machine_id,
            "key_label": key.label,
            "input_tokens": result.get("_input_tokens", 0),
            "output_tokens": result.get("_output_tokens", 0),
            "latency_ms": result.get("_latency_ms", 0),
        }

        self.db.save_page_result(
            book_id, page_no, status, result, provenance, flag_reason
        )

        if is_ok:
            self.log(
                f"{title} p.{page_no} → {result['page_type']} [{key.label}]",
                "ok",
                book_id=book_id,
                page_no=page_no,
            )
        else:
            self.log(
                f"{title} p.{page_no} flagged for review: {flag_reason}",
                "warn",
                book_id=book_id,
                page_no=page_no,
            )

        return "done"

    # -----------------------------------------------------------------
    # Worker loop
    # -----------------------------------------------------------------

    def _worker(self) -> None:
        """
        One worker thread's main loop.

        Takes no arguments: the thread's identity is carried by its own name
        (set in start()), so there is nothing to pass in and nothing to get
        out of step with the thread target signature.
        """
        while self._running:
            page_row = self.db.claim_next_page()

            if page_row is None:
                # Nothing pending. Another worker may release a page shortly
                # (a rate limit puts one back), so wait briefly rather than
                # exiting -- but if there is genuinely nothing left, the
                # queue check below will keep this cheap.
                summary = self.db.queue_summary()
                if summary["pending"] == 0 and summary["in_progress"] == 0:
                    break
                time.sleep(5)
                continue

            try:
                outcome = self._process_page(page_row)
            except Exception as exc:
                # Last-resort guard. A bug here must not silently kill the
                # thread and leave the page claimed forever.
                self.db.release_page(
                    int(page_row["book_id"]), int(page_row["page_no"])
                )
                self.log(
                    f"Unexpected error on page {page_row['page_no']}: {exc}",
                    "err",
                )
                time.sleep(5)
                continue

            if outcome == "no-quota":
                # Sleep exactly until a key frees up rather than polling.
                wait = self.pool.next_available_in()

                if wait is None:
                    # No key can be used again today at all.
                    if not self.pool.has_live_keys():
                        self.log(
                            "Every key is dead. Add working keys and restart.",
                            "err",
                        )
                        break

                    capacity = self.pool.capacity_today()
                    self.log(
                        "Daily allowance spent on all keys. Waiting for the "
                        f"midnight Pacific reset in "
                        f"{capacity['seconds_to_reset'] // 3600}h.",
                        "warn",
                    )
                    wait = min(self.config.idle_sleep_seconds, 300)

                # Wake periodically so a stop() is noticed promptly rather
                # than after a five-minute sleep.
                slept = 0.0
                while slept < wait and self._running:
                    time.sleep(min(2.0, wait - slept))
                    slept += 2.0

    # -----------------------------------------------------------------
    # Control
    # -----------------------------------------------------------------

    def start(self) -> int:
        """
        Spawn worker threads. Returns how many started.

        One thread per live key, capped by max_workers. There is no point
        having more threads than keys: a thread with no key to use can only
        sleep.
        """
        if self._running:
            return len(self._threads)

        # Any in_progress rows belong to a process that no longer exists.
        reclaimed = self.db.reset_stale_claims()
        if reclaimed:
            self.log(f"Reclaimed {reclaimed} page(s) from a previous run.", "warn")

        capacity = self.pool.capacity_today()

        if capacity["keys_live"] == 0:
            self.log("No usable keys. Import keys before starting.", "err")
            return 0

        if capacity["remaining_today"] == 0:
            self.log(
                "No requests left on any key today. Next reset in "
                f"{capacity['seconds_to_reset'] // 3600}h.",
                "warn",
            )

        self._running = True

        # One worker per key IN PLAY, not per key owned. Only a few keys are
        # used at a time (see quota.MAX_CONCURRENT_KEYS), and a thread with no
        # key available to it can do nothing but sleep.
        worker_count = min(
            self.pool.working_set_size(),
            self.config.max_workers,
        )

        if worker_count == 0:
            self._running = False
            self.log(
                "No keys are in play. Check that keys are enabled and have "
                "quota left today.",
                "err",
            )
            return 0

        self._threads = []
        for index in range(worker_count):
            name = f"worker-{index + 1}"
            thread = threading.Thread(target=self._worker, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)

        # Confirm the threads are actually alive rather than trusting that
        # start() returning means they are running. A thread that dies in its
        # first instruction prints a traceback to stderr and is otherwise
        # invisible — on a headless overnight run that looks exactly like
        # "nothing happened", which is the worst thing to have to debug.
        time.sleep(0.1)
        alive = sum(1 for thread in self._threads if thread.is_alive())

        if alive == 0 and worker_count > 0:
            self._running = False
            self.log(
                "Workers failed to start — check stderr for a traceback.",
                "err",
            )
            return 0

        self.log(
            f"Started {alive} worker(s) on {capacity['keys_in_use']} key(s) in "
            f"play (limit {capacity['max_concurrent_keys']}). "
            f"{capacity['remaining_today']} request(s) available today across "
            f"{capacity['keys_live']} enabled key(s).",
            "ok",
        )
        return alive

    def stop(self) -> None:
        """Signal workers to finish their current page and exit."""
        if not self._running:
            return
        self._running = False
        self.log("Stopping — workers will finish the page they are on.", "info")

    def join(self, timeout: float | None = None) -> None:
        """Wait for all worker threads to exit."""
        for thread in self._threads:
            thread.join(timeout=timeout)
        self._threads = []

    @property
    def is_running(self) -> bool:
        return self._running

    def status(self) -> dict:
        """Everything the UI needs for one refresh."""
        return {
            "running": self._running,
            "workers": len([t for t in self._threads if t.is_alive()]),
            "queue": self.db.queue_summary(),
            "capacity": self.pool.capacity_today(),
            "keys": self.pool.snapshot(),
            "app_version": APP_VERSION,
            "machine_id": self.config.machine_id,
        }
