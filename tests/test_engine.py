"""
Ledger — test suite.

Covers everything that can be tested without reaching the Gemini API: quota
accounting, the work queue's concurrency guarantees, rendering, the quality
heuristics, scanning and export.

The API client itself is exercised only for its error classification, which is
pure logic. The network calls cannot be tested from here, so they are marked
clearly in the report rather than pretended over.

Run with:  python -m pytest tests/ -v
       or:  python tests/test_engine.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pymupdf

from ledger.config import Config
from ledger.db import (
    PAGE_DONE,
    PAGE_FAILED,
    PAGE_FLAGGED,
    PAGE_PENDING,
    Database,
)
from ledger.export import export_all, export_book
from ledger.gemini import (
    BadResponse,
    KeyRejected,
    RateLimited,
    TransientError,
    _classify_error,
    _extract_json_payload,
)
from ledger.prompts import build_page_prompt, describe_profile
from ledger.quota import (
    STATE_ACTIVE,
    STATE_COOLDOWN,
    STATE_DEAD,
    STATE_EXHAUSTED,
    ApiKey,
    KeyPool,
    pacific_date,
)
from ledger.render import (
    analyse_page,
    fingerprint_file,
    page_count,
    render_page,
    select_text_pages,
)
from ledger.scanner import scan
from ledger.worker import assess_quality


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

BODY_LINE = "Van de oude gebruiken dezer landen valt zeer veel te verhalen,"


def add_dense_page(doc, lines: int = 34, size: int = 9) -> None:
    """A page of body text, as dense as a real book page."""
    page = doc.new_page(width=420, height=595)  # roughly A5
    y = 70.0
    for _ in range(lines):
        page.insert_text((50, y), BODY_LINE, fontsize=size)
        y += size * 1.7


def add_blank_page(doc) -> None:
    doc.new_page(width=420, height=595)


def add_sparse_page(doc, text: str = "OUDE GEBRUIKEN") -> None:
    """A half-title or cover: a few large words and nothing else."""
    page = doc.new_page(width=420, height=595)
    page.insert_text((80, 200), text, fontsize=28)


def add_plate_page(doc, caption: str | None = None) -> None:
    """A full-page illustration, optionally with a caption line."""
    page = doc.new_page(width=420, height=595)
    page.draw_rect(pymupdf.Rect(80, 100, 340, 380), color=(0, 0, 0), fill=(0.35, 0.35, 0.35))
    if caption:
        page.insert_text((90, 420), caption, fontsize=10)


def make_pdf(path: Path, pages: int = 3, text: str = "Van de Nederlandsche Taal") -> Path:
    """Build a small multi-page PDF of dense body text."""
    doc = pymupdf.open()
    for _ in range(pages):
        add_dense_page(doc)
    doc.save(path)
    doc.close()
    return path


def make_key(label="k1", rpm=0, rpd=10) -> ApiKey:
    """
    A key with pacing disabled (rpm=0), so quota-logic tests are not blocked by
    the RPM spacing gap. The pacing behaviour itself is tested separately in
    test_rpm_pacing.
    """
    return ApiKey(id=1, label=label, secret="secret", rpm_limit=rpm, rpd_limit=rpd)


class Results:
    """Minimal test harness so this file runs with or without pytest."""

    def __init__(self):
        self.passed = 0
        self.failed: list[str] = []

    def check(self, name: str, condition: bool, detail: str = ""):
        if condition:
            self.passed += 1
            print(f"  PASS  {name}")
        else:
            self.failed.append(f"{name}: {detail}")
            print(f"  FAIL  {name}  {detail}")

            # Under pytest, collecting failures silently is dangerous: pytest
            # collects these test_* functions and would report them all green,
            # because check() does not raise. Assert so a pytest run tells the
            # truth. The standalone runner (which reports every failure at the
            # end) is unaffected.
            if "pytest" in sys.modules:
                raise AssertionError(f"{name}: {detail}")


R = Results()


# ---------------------------------------------------------------------
# Quota accounting
# ---------------------------------------------------------------------

def test_pacific_date():
    print("\nPacific-time quota day")

    # The bug this guards against: using local midnight to reset counters.
    # From India, local midnight is ~11.5 hours before Google's Pacific reset,
    # so a local-time implementation zeroes the counters early and then spends
    # half a day collecting 429s.
    ist_early_morning = datetime(2026, 8, 28, 5, 30, tzinfo=ZoneInfo("Asia/Kolkata"))
    R.check(
        "IST early morning is still the PREVIOUS Pacific day",
        pacific_date(ist_early_morning) == "2026-08-27",
        f"got {pacific_date(ist_early_morning)}",
    )

    ist_afternoon = datetime(2026, 8, 28, 14, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    R.check(
        "IST afternoon has rolled into the same Pacific day",
        pacific_date(ist_afternoon) == "2026-08-28",
        f"got {pacific_date(ist_afternoon)}",
    )


def test_daily_reset():
    print("\nDaily counter rollover")

    key = make_key(rpd=5)
    key.used_on = "2026-01-01"
    key.used_today = 5
    key.state = STATE_EXHAUSTED

    changed = key.refresh_for_today("2026-01-02")
    R.check("rollover reports a change", changed is True)
    R.check("counter reset to zero", key.used_today == 0, f"got {key.used_today}")
    R.check("exhausted key revived", key.state == STATE_ACTIVE, key.state)

    # A dead key must NOT come back at midnight.
    dead = make_key()
    dead.state = STATE_DEAD
    dead.used_on = "2026-01-01"
    dead.refresh_for_today("2026-01-02")
    R.check("dead key stays dead across the reset", dead.state == STATE_DEAD, dead.state)


def test_pool_exhaustion():
    print("\nKey pool: exhaustion and revival")

    key = make_key(rpd=3)
    pool = KeyPool([key])

    for _ in range(3):
        acquired = pool.acquire()
        R.check("key available before limit", acquired is not None)
        pool.record_success(acquired)

    R.check("state is exhausted after the limit", key.state == STATE_EXHAUSTED, key.state)
    R.check("no key available once exhausted", pool.acquire() is None)
    R.check("next_available_in reports nothing today", pool.next_available_in() is None)

    capacity = pool.capacity_today()
    R.check("capacity shows zero remaining", capacity["remaining_today"] == 0)
    R.check("capacity still counts the key as live", capacity["keys_live"] == 1)


def test_429_daily_vs_throttle():
    print("\nKey pool: distinguishing daily exhaustion from throttling")

    # A message naming the per-day quota means the day is over.
    key = make_key(rpd=100)
    pool = KeyPool([key])
    outcome = pool.record_rate_limited(
        key, "Quota exceeded for metric GenerateRequestsPerDayPerProject"
    )
    R.check("per-day message → exhausted", outcome == STATE_EXHAUSTED, outcome)

    # A generic 429 with plenty of allowance left is a per-minute throttle.
    key2 = make_key(label="k2", rpd=100)
    pool2 = KeyPool([key2])
    outcome2 = pool2.record_rate_limited(key2, "Too many requests, slow down")
    R.check("generic message → cooldown", outcome2 == STATE_COOLDOWN, outcome2)
    R.check("cooldown is in the future", key2.cooldown_until > time.time())
    R.check("cooldown blocks acquire", pool2.acquire() is None)

    # Retry-After from the server should win over our backoff guess.
    key3 = make_key(label="k3", rpd=100)
    pool3 = KeyPool([key3])
    pool3.record_rate_limited(key3, "slow down", retry_after=42.0)
    remaining = key3.cooldown_until - time.time()
    R.check(
        "Retry-After honoured",
        40 <= remaining <= 43,
        f"got {remaining:.1f}s",
    )


def test_429_learns_real_limit():
    print("\nKey pool: learning the real daily ceiling")

    # rpd_limit is deliberately set far too high, as it would be if the
    # documented figure were wrong. Repeated refusals should teach the pool
    # what the account actually allows.
    key = make_key(rpd=5000)
    pool = KeyPool([key])

    # used_on must match today, or refresh_for_today() correctly treats the
    # counter as stale and zeroes it.
    key.used_on = pacific_date()
    key.used_today = 180

    outcome = ""
    for _ in range(4):
        outcome = pool.record_rate_limited(key, "RESOURCE_EXHAUSTED")

    R.check("repeated 429s → exhausted", outcome == STATE_EXHAUSTED, outcome)
    R.check(
        "learned limit matches observed usage",
        key.rpd_limit == 180,
        f"got {key.rpd_limit}",
    )
    R.check("reason explains the correction", "learned" in key.last_error, key.last_error)

    # The dangerous case: a burst of 429s at the very start of the day, before
    # the key has done any real work. Writing the limit down here would be
    # PERSISTED and would cripple the key permanently, since an exhausted key
    # comes back at midnight with its limit intact.
    fresh = make_key(label="fresh", rpd=250)
    fresh_pool = KeyPool([fresh])
    fresh.used_on = pacific_date()
    fresh.used_today = 2

    for _ in range(4):
        fresh_pool.record_rate_limited(fresh, "RESOURCE_EXHAUSTED")

    R.check(
        "early 429 burst still parks the key for today",
        fresh.state == STATE_EXHAUSTED,
        fresh.state,
    )
    R.check(
        "early 429 burst does NOT rewrite the daily limit",
        fresh.rpd_limit == 250,
        f"got {fresh.rpd_limit}",
    )
    R.check(
        "the key recovers its full limit at the next reset",
        (fresh.refresh_for_today("2099-01-01"), fresh.state == STATE_ACTIVE
         and fresh.rpd_limit == 250)[1],
        f"state={fresh.state} limit={fresh.rpd_limit}",
    )


def test_dead_key():
    print("\nKey pool: dead keys")

    good = make_key(label="good", rpd=10)
    bad = make_key(label="bad", rpd=10)
    bad.id = 2
    pool = KeyPool([good, bad])

    pool.record_dead(bad, "API_KEY_INVALID")
    R.check("dead key marked", bad.state == STATE_DEAD, bad.state)
    R.check("pool still has live keys", pool.has_live_keys() is True)

    # Only the healthy key should ever be handed out now.
    handed_out = []
    for _ in range(5):
        acquired = pool.acquire()
        if acquired is None:
            break
        handed_out.append(acquired.label)
        pool.record_success(acquired)

    R.check("some keys were handed out", len(handed_out) == 5, f"got {len(handed_out)}")
    R.check(
        "the dead key was never handed out",
        "bad" not in handed_out,
        f"got {handed_out}",
    )

    pool.record_dead(good, "revoked")
    R.check("no live keys once all are dead", pool.has_live_keys() is False)


def test_dead_is_terminal():
    print("\nKey pool: dead is terminal")

    # Two workers can hold the same key object at once, so a slower one may
    # report its outcome AFTER a faster one has already retired the key. If any
    # of these paths moved the key back out of `dead`, a revoked key would go
    # back into rotation and be retried forever.
    for name, apply_outcome in [
        (
            "a transient error does not revive a dead key",
            lambda pool, key: pool.record_transient_error(key, "HTTP 503"),
        ),
        (
            "a rate limit does not revive a dead key",
            lambda pool, key: pool.record_rate_limited(key, "slow down"),
        ),
        (
            "a late success does not revive a dead key",
            lambda pool, key: pool.record_success(key),
        ),
    ]:
        key = make_key(label="zombie", rpd=100)
        pool = KeyPool([key])
        pool.record_dead(key, "API_KEY_INVALID")

        apply_outcome(pool, key)

        R.check(name, key.state == STATE_DEAD, f"became {key.state}")

    # And it must stay out of rotation afterwards.
    key = make_key(label="zombie", rpd=100)
    pool = KeyPool([key])
    pool.record_dead(key, "revoked")
    pool.record_transient_error(key, "HTTP 503")
    R.check(
        "a dead key is never handed out again",
        pool.acquire() is None,
    )
    R.check(
        "a dead key's usage is not counted after death",
        key.used_today == 0,
        f"got {key.used_today}",
    )


def test_rpm_pacing():
    print("\nKey pool: RPM pacing")

    # 60 RPM means one request per second minimum spacing.
    key = make_key(rpm=60, rpd=100)
    pool = KeyPool([key])

    first = pool.acquire()
    R.check("first acquire succeeds", first is not None)

    second = pool.acquire()
    R.check("immediate second acquire is refused by pacing", second is None)

    wait = pool.next_available_in()
    R.check(
        "reported wait is about one second",
        wait is not None and 0.5 <= wait <= 1.05,
        f"got {wait}",
    )


def test_pool_load_spreading():
    print("\nKey pool: spreading load across keys")

    keys = [
        ApiKey(id=i, label=f"k{i}", secret="s", rpm_limit=0, rpd_limit=10)
        for i in range(1, 4)
    ]
    pool = KeyPool(keys)

    used = []
    for _ in range(3):
        acquired = pool.acquire()
        used.append(acquired.label)
        pool.record_success(acquired)

    R.check(
        "three acquires used three different keys",
        len(set(used)) == 3,
        f"got {used}",
    )


# ---------------------------------------------------------------------
# Database and the work queue
# ---------------------------------------------------------------------

def test_book_and_page_idempotency():
    print("\nDatabase: rescanning is safe")

    with tempfile.TemporaryDirectory() as tmp:
        database = Database(Path(tmp) / "test.db")

        book_id, created = database.upsert_book(
            "a/b.pdf", "b.pdf", "b", "quick:x", 1000, 10
        )
        R.check("first upsert creates", created is True)
        R.check("pages created", database.create_pages_for_book(book_id, 10) == 10)

        # Simulate progress, then rescan.
        database.save_page_result(
            book_id, 1, PAGE_DONE,
            {"page_type": "text", "transcription": "Hello"}, {},
        )

        same_id, created_again = database.upsert_book(
            "a/b.pdf", "b.pdf", "b", "quick:x", 1000, 10
        )
        R.check("second upsert returns same id", same_id == book_id)
        R.check("second upsert does not re-create", created_again is False)
        R.check(
            "rescan creates no duplicate pages",
            database.create_pages_for_book(book_id, 10) == 0,
        )

        rows = database.pages_for_book(book_id)
        R.check("page count unchanged", len(rows) == 10, f"got {len(rows)}")
        R.check(
            "existing transcription survived the rescan",
            rows[0]["transcription"] == "Hello",
            rows[0]["transcription"],
        )

        # A PDF that gained pages should queue only the new ones.
        added = database.create_pages_for_book(book_id, 14)
        R.check("growing a book queues only new pages", added == 4, f"got {added}")


def test_claim_is_atomic():
    print("\nDatabase: concurrent claims never collide")

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "claims.db"
        database = Database(db_path)
        book_id, _ = database.upsert_book("x.pdf", "x.pdf", "x", "f", 1, 50)
        database.create_pages_for_book(book_id, 50)

        claimed: list[tuple[int, int]] = []
        lock = threading.Lock()

        def grab():
            # Each thread gets its own connection via thread-local storage.
            local_db = Database(db_path)
            while True:
                row = local_db.claim_next_page()
                if row is None:
                    return
                with lock:
                    claimed.append((row["book_id"], row["page_no"]))

        threads = [threading.Thread(target=grab) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        R.check("all 50 pages claimed", len(claimed) == 50, f"got {len(claimed)}")
        R.check(
            "no page claimed twice",
            len(set(claimed)) == 50,
            f"{len(claimed) - len(set(claimed))} duplicates",
        )


def test_release_does_not_burn_attempts():
    print("\nDatabase: quota problems do not penalise a page")

    with tempfile.TemporaryDirectory() as tmp:
        database = Database(Path(tmp) / "r.db")
        book_id, _ = database.upsert_book("x.pdf", "x.pdf", "x", "f", 1, 3)
        database.create_pages_for_book(book_id, 3)

        # Claim and release repeatedly, as happens when every key is exhausted.
        for _ in range(10):
            row = database.claim_next_page()
            database.release_page(row["book_id"], row["page_no"])

        rows = database.pages_for_book(book_id)
        R.check(
            "attempts stayed at zero across releases",
            all(r["attempts"] == 0 for r in rows),
            f"got {[r['attempts'] for r in rows]}",
        )
        R.check(
            "all pages back to pending",
            all(r["status"] == PAGE_PENDING for r in rows),
        )


def test_failure_escalates_to_failed():
    print("\nDatabase: attempt counting and failure")

    with tempfile.TemporaryDirectory() as tmp:
        database = Database(Path(tmp) / "f.db")
        book_id, _ = database.upsert_book("x.pdf", "x.pdf", "x", "f", 1, 1)
        database.create_pages_for_book(book_id, 1)

        first = database.record_page_failure(book_id, 1, "boom", max_attempts=3)
        R.check("first failure requeues", first == PAGE_PENDING, first)

        database.record_page_failure(book_id, 1, "boom", max_attempts=3)
        third = database.record_page_failure(book_id, 1, "boom", max_attempts=3)
        R.check("third failure gives up", third == PAGE_FAILED, third)

        # Requeue should clear the counter so a manual retry gets a full run.
        count = database.requeue_pages([PAGE_FAILED])
        R.check("requeue moved one page", count == 1, f"got {count}")

        row = database.pages_for_book(book_id)[0]
        R.check("requeued page is pending", row["status"] == PAGE_PENDING)
        R.check("attempts cleared by requeue", row["attempts"] == 0)


def test_stale_claims_reclaimed():
    print("\nDatabase: startup reclaims abandoned pages")

    with tempfile.TemporaryDirectory() as tmp:
        database = Database(Path(tmp) / "s.db")
        book_id, _ = database.upsert_book("x.pdf", "x.pdf", "x", "f", 1, 5)
        database.create_pages_for_book(book_id, 5)

        # Claim three and "crash" without releasing.
        for _ in range(3):
            database.claim_next_page()

        summary = database.queue_summary()
        R.check("three pages held", summary["in_progress"] == 3, str(summary))

        reclaimed = database.reset_stale_claims()
        R.check("all three reclaimed", reclaimed == 3, f"got {reclaimed}")
        R.check(
            "queue is fully pending again",
            database.queue_summary()["pending"] == 5,
        )


def test_key_persistence():
    print("\nDatabase: key state survives a restart")

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "k.db"
        database = Database(db_path)
        database.add_key("acct-01", "secret-value", rpm_limit=10, rpd_limit=7)

        pool = KeyPool(database.load_keys(), on_change=database.save_key)
        key = pool.acquire()
        pool.record_success(key)
        pool.record_success(key)

        # Reopen as a new process would.
        reopened = Database(db_path)
        keys = reopened.load_keys()
        R.check("one key loaded", len(keys) == 1)
        R.check("usage persisted", keys[0].used_today == 2, f"got {keys[0].used_today}")
        R.check("limit persisted", keys[0].rpd_limit == 7)

        # Re-importing must not wipe today's usage.
        reopened.add_key("acct-01", "new-secret", rpm_limit=15, rpd_limit=7)
        after = reopened.load_keys()[0]
        R.check("re-import preserved usage", after.used_today == 2, f"got {after.used_today}")
        R.check("re-import updated the secret", after.secret == "new-secret")


# ---------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------

def test_render():
    print("\nRendering")

    with tempfile.TemporaryDirectory() as tmp:
        pdf = make_pdf(Path(tmp) / "book.pdf", pages=3)

        R.check("page count correct", page_count(pdf) == 3, str(page_count(pdf)))

        png = render_page(pdf, 1, dpi=300, greyscale=True, image_format="png")
        R.check("PNG magic bytes present", png[:8] == b"\x89PNG\r\n\x1a\n")
        R.check("PNG is a plausible size", len(png) > 2000, f"{len(png)} bytes")

        # 300 DPI on a 420x595pt page should be about 1750x2479 px.
        from PIL import Image
        import io as _io
        image = Image.open(_io.BytesIO(png))
        R.check(
            "300 DPI produced expected pixel dimensions",
            1700 < image.width < 1800 and 2400 < image.height < 2550,
            f"got {image.size}",
        )
        R.check("greyscale render is single-channel", image.mode == "L", image.mode)

        # The default path must preserve colour: three channels, not one.
        colour = render_page(pdf, 1, dpi=150, greyscale=False, image_format="png")
        colour_image = Image.open(_io.BytesIO(colour))
        R.check(
            "colour render keeps three channels",
            colour_image.mode in ("RGB", "RGBA"),
            colour_image.mode,
        )
        R.check("colour render is a valid PNG", colour[:8] == b"\x89PNG\r\n\x1a\n")

        # And the text detector must still work on a colour page — it renders
        # its own greyscale copy internally for the Otsu threshold, so colour
        # input must not break page selection.
        from ledger.render import analyse_page as _analyse
        R.check(
            "text detection still works with colour input",
            _analyse(pdf, 1)["looks_like_text"] is True,
            str(_analyse(pdf, 1)),
        )

        # Higher DPI must produce a proportionally larger image.
        big = render_page(pdf, 1, dpi=400, greyscale=True, image_format="png")
        big_image = Image.open(_io.BytesIO(big))
        ratio = big_image.width / image.width
        R.check(
            "400 DPI is 4/3 the width of 300 DPI",
            1.30 < ratio < 1.36,
            f"ratio {ratio:.3f}",
        )

        # Out-of-range pages must raise, not return junk.
        try:
            render_page(pdf, 99)
            R.check("out-of-range page raises", False, "no exception")
        except Exception as exc:
            R.check("out-of-range page raises", "out of range" in str(exc), str(exc))

        # Deskew path must still produce a valid PNG.
        deskewed = render_page(pdf, 1, dpi=150, deskew=True, image_format="png")
        R.check("deskew still yields valid PNG", deskewed[:8] == b"\x89PNG\r\n\x1a\n")


def test_page_content_analysis():
    print("\nLocal page content analysis (no API cost)")

    with tempfile.TemporaryDirectory() as tmp:
        doc = pymupdf.open()
        add_sparse_page(doc)                              # 1 cover
        add_blank_page(doc)                               # 2 blank
        add_dense_page(doc)                               # 3 body text
        add_dense_page(doc, lines=32, size=11)            # 4 larger body text
        add_plate_page(doc)                               # 5 plate, no caption
        add_plate_page(doc, "Fig. 4 — de oude molen")     # 6 captioned plate
        add_dense_page(doc, lines=6)                      # 7 half-title
        pdf = Path(tmp) / "kinds.pdf"
        doc.save(pdf)
        doc.close()

        expectations = [
            (1, False, "cover with a few big words is too sparse to sample"),
            (2, False, "blank page detected"),
            (3, True, "dense body text detected"),
            (4, True, "larger body text detected"),
            (5, False, "uncaptioned plate rejected"),
            (6, False, "captioned plate rejected (one line is not a sample)"),
            (7, False, "six-line page rejected as too sparse"),
        ]

        for page_no, expected, name in expectations:
            result = analyse_page(pdf, page_no)
            R.check(
                name,
                result["looks_like_text"] is expected,
                f"ink={result['ink_ratio']} empty={result['empty_row_fraction']} "
                f"reason={result['reason']}",
            )

        # Yellowed paper must measure like white paper. This is the whole
        # reason for using Otsu rather than a fixed threshold: a fixed cutoff
        # would read a tinted scan as either all ink or all paper.
        tinted = pymupdf.open()
        page = tinted.new_page(width=420, height=595)
        page.draw_rect(
            pymupdf.Rect(0, 0, 420, 595),
            color=(0.72, 0.70, 0.62),
            fill=(0.72, 0.70, 0.62),
        )
        y = 70.0
        for _ in range(34):
            page.insert_text((50, y), BODY_LINE, fontsize=9)
            y += 15.3
        tinted_pdf = Path(tmp) / "tinted.pdf"
        tinted.save(tinted_pdf)
        tinted.close()

        white = analyse_page(pdf, 3)
        yellow = analyse_page(tinted_pdf, 1)
        R.check(
            "yellowed paper is still read as text",
            yellow["looks_like_text"] is True,
            str(yellow),
        )
        R.check(
            "yellowed paper measures like white paper",
            abs(yellow["ink_ratio"] - white["ink_ratio"]) < 0.01,
            f"white={white['ink_ratio']} yellow={yellow['ink_ratio']}",
        )


def test_profiler_page_selection():
    print("\nProfiler page selection")

    with tempfile.TemporaryDirectory() as tmp:
        # A realistic book: 15 pages of front matter (cover, blanks, half-title,
        # frontispiece) and then the text proper.
        doc = pymupdf.open()
        add_sparse_page(doc, "OUDE GEBRUIKEN")   # 1 cover
        add_blank_page(doc)                      # 2
        add_blank_page(doc)                      # 3
        add_sparse_page(doc, "OUDE GEBRUIKEN")   # 4 half-title
        add_plate_page(doc)                      # 5 frontispiece
        for _ in range(6, 16):                   # 6-15 more front matter
            add_blank_page(doc)
        for _ in range(16, 31):                  # 16-30 body text
            add_dense_page(doc)
        book = Path(tmp) / "book.pdf"
        doc.save(book)
        doc.close()

        pages, strategy = select_text_pages(book, count=3, start_after_page=15)
        R.check("three sample pages chosen", len(pages) == 3, f"got {pages}")
        R.check(
            "all samples come from after the front matter",
            all(p > 15 for p in pages),
            f"got {pages}",
        )
        R.check("intended strategy used", strategy == "after front matter", strategy)

        # A book with body text starting immediately, but only 8 pages long, so
        # the intended window does not exist at all.
        short_doc = pymupdf.open()
        for _ in range(8):
            add_dense_page(short_doc)
        short = Path(tmp) / "short.pdf"
        short_doc.save(short)
        short_doc.close()

        pages, strategy = select_text_pages(short, count=3, start_after_page=15)
        R.check("short book still gets samples", len(pages) == 3, f"got {pages}")
        R.check("short book used the fallback", "fallback" in strategy, strategy)

        # A volume of nothing but plates and blanks. Must return nothing rather
        # than picking a bad sample -- and crucially, the caller then spends no
        # request at all.
        plates_doc = pymupdf.open()
        for _ in range(20):
            add_plate_page(plates_doc)
            add_blank_page(plates_doc)
        plates = Path(tmp) / "plates.pdf"
        plates_doc.save(plates)
        plates_doc.close()

        pages, strategy = select_text_pages(plates, count=3, start_after_page=15)
        R.check("all-plate volume yields no samples", pages == [], f"got {pages}")
        R.check("reason reported", strategy == "no text pages found", strategy)

        # scan_limit must bound the work on a long book whose text starts late.
        late_doc = pymupdf.open()
        for _ in range(120):
            add_blank_page(late_doc)
        for _ in range(20):
            add_dense_page(late_doc)
        late = Path(tmp) / "late.pdf"
        late_doc.save(late)
        late_doc.close()

        pages, strategy = select_text_pages(
            late, count=3, start_after_page=15, scan_limit=10
        )
        R.check(
            "scan_limit stops the search before the text begins",
            pages == [] or strategy == "no text pages found",
            f"got {pages} / {strategy}",
        )

        # With a limit large enough to reach the text, it finds it.
        pages, strategy = select_text_pages(
            late, count=2, start_after_page=15, scan_limit=200
        )
        R.check(
            "a generous scan_limit reaches late-starting text",
            len(pages) == 2 and all(p > 120 for p in pages),
            f"got {pages}",
        )



def test_fingerprint():
    print("\nFile fingerprinting")

    with tempfile.TemporaryDirectory() as tmp:
        a = make_pdf(Path(tmp) / "a.pdf", pages=2, text="Alpha")
        b = make_pdf(Path(tmp) / "b.pdf", pages=2, text="Beta beta beta")

        fa = fingerprint_file(a)
        fb = fingerprint_file(b)

        R.check("fingerprint is stable across calls", fa == fingerprint_file(a))
        R.check("different files differ", fa != fb)
        R.check("quick fingerprint is labelled", fa.startswith("quick:"), fa[:12])

        full = fingerprint_file(a, full=True)
        R.check("full hash is labelled", full.startswith("sha256:"), full[:12])
        R.check("full hash differs from quick", full != fa)


# ---------------------------------------------------------------------
# Quality heuristics
# ---------------------------------------------------------------------

def test_quality_heuristics():
    print("\nQuality heuristics")

    ok, _ = assess_quality({
        "page_type": "text",
        "transcription": "Dit is een volledige bladzijde met veel tekst erop geschreven.",
    })
    R.check("a normal text page passes", ok is True)

    ok, reason = assess_quality({"page_type": "text", "transcription": "ab"})
    R.check("near-empty text page is flagged", ok is False, reason)

    ok, _ = assess_quality({"page_type": "blank", "transcription": ""})
    R.check("blank page is not a fault", ok is True)

    ok, _ = assess_quality({"page_type": "illustration_only", "transcription": ""})
    R.check("illustration-only page is not a fault", ok is True)

    ok, _ = assess_quality({
        "page_type": "unreadable",
        "transcription": "",
        "note": "water damage",
    })
    R.check("unreadable is a legitimate result", ok is True)

    # Degeneration loop.
    ok, reason = assess_quality({
        "page_type": "text",
        "transcription": "de heer van den berg en zijne vrouwe " * 40,
    })
    R.check("repetition loop is flagged", ok is False, reason)

    # Mostly illegible.
    ok, reason = assess_quality({
        "page_type": "text",
        "transcription": "[illegible] [illegible] [illegible] [illegible] abc",
    })
    R.check("mostly-illegible page is flagged", ok is False, reason)

    ok, reason = assess_quality({
        "page_type": "text",
        "transcription": "Goede tekst hier\x00met een nulbyte erin verstopt.",
    })
    R.check("control characters are flagged", ok is False, reason)

    # Footnotes should count towards the length test.
    ok, _ = assess_quality({
        "page_type": "text",
        "transcription": "",
        "footnotes": "1. Zie het voorgaande hoofdstuk, bladzijde 44 en volgende.",
    })
    R.check("footnote-only page passes on combined length", ok is True)


# ---------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------

def test_error_classification():
    print("\nAPI error classification")

    R.check(
        "429 → RateLimited",
        isinstance(_classify_error(429, "RESOURCE_EXHAUSTED"), RateLimited),
    )
    R.check(
        "403 naming a permission problem → KeyRejected",
        isinstance(_classify_error(403, "PERMISSION_DENIED"), KeyRejected),
    )
    R.check(
        "401 naming an invalid key → KeyRejected",
        isinstance(_classify_error(401, "API_KEY_INVALID"), KeyRejected),
    )

    # A 403 from a proxy or firewall must NOT retire the key. Retiring is
    # permanent and never retried, so a proxy outage classified as a key
    # rejection would silently kill all twenty keys within minutes.
    proxy_403 = _classify_error(
        403, "Host not in allowlist: generativelanguage.googleapis.com"
    )
    R.check(
        "403 from an intermediary → TransientError, not KeyRejected",
        isinstance(proxy_403, TransientError),
        type(proxy_403).__name__,
    )
    R.check(
        "the intermediary case says to check the network",
        "proxy" in str(proxy_403) or "network" in str(proxy_403),
        str(proxy_403)[:80],
    )
    captive_portal = _classify_error(403, "<html>Access blocked by policy</html>")
    R.check(
        "an HTML block page → TransientError",
        isinstance(captive_portal, TransientError),
        type(captive_portal).__name__,
    )
    R.check(
        "500 → TransientError",
        isinstance(_classify_error(500, "internal"), TransientError),
    )
    R.check(
        "503 → TransientError",
        isinstance(_classify_error(503, "overloaded"), TransientError),
    )

    # A 400 is normally our fault...
    R.check(
        "400 with a schema complaint → BadResponse",
        isinstance(_classify_error(400, "Unknown name 'thinking_level'"), BadResponse),
    )
    # ...but Google also returns 400 for a bad key, which must not be retried
    # as though the page were at fault.
    R.check(
        "400 naming an invalid key → KeyRejected",
        isinstance(_classify_error(400, "API_KEY_INVALID: not valid"), KeyRejected),
    )

    error = _classify_error(429, "slow", {"Retry-After": "30"})
    R.check("Retry-After parsed from headers", error.retry_after == 30.0, str(error.retry_after))


def test_response_parsing():
    print("\nResponse parsing")

    good = {
        "steps": [
            {"type": "other", "content": []},
            {
                "type": "model_output",
                "content": [
                    {"type": "text", "text": '{"page_type":"text","transcription":"Hoi"}'}
                ],
            },
        ]
    }
    parsed = _extract_json_payload(good)
    R.check("payload extracted from the right step", parsed["transcription"] == "Hoi")

    # Markdown-fenced JSON should still be recovered rather than wasting a
    # request that otherwise succeeded.
    fenced = {
        "steps": [
            {
                "type": "model_output",
                "content": [
                    {"type": "text", "text": '```json\n{"page_type":"blank"}\n```'}
                ],
            }
        ]
    }
    R.check(
        "markdown fences stripped",
        _extract_json_payload(fenced)["page_type"] == "blank",
    )

    for name, payload in [
        ("no steps", {"steps": []}),
        ("no model_output", {"steps": [{"type": "other", "content": []}]}),
        ("no text block", {"steps": [{"type": "model_output", "content": []}]}),
        (
            "invalid JSON",
            {
                "steps": [
                    {
                        "type": "model_output",
                        "content": [{"type": "text", "text": "not json at all"}],
                    }
                ]
            },
        ),
    ]:
        try:
            _extract_json_payload(payload)
            R.check(f"{name} raises BadResponse", False, "no exception")
        except BadResponse:
            R.check(f"{name} raises BadResponse", True)
        except Exception as exc:
            R.check(f"{name} raises BadResponse", False, f"got {type(exc).__name__}")


# ---------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------

def test_prompts():
    print("\nPrompt construction")

    # With no profile the prompt must NOT assert a century or language --
    # that was the original app's mistake.
    blind = build_page_prompt(None)
    R.check("blind prompt avoids asserting a century", "19th-century" not in blind)
    R.check("blind prompt says the period is unknown", "not known in advance" in blind)

    gujarati = describe_profile({
        "primary_language": "Gujarati",
        "script": "Gujarati",
        "era": "19th-century",
        "typeface": "hand-set metal type",
    })
    R.check("profile mentions the language", "Gujarati" in gujarati)
    R.check("non-Latin script is called out", "script" in gujarati)

    # An empty era (what gemini.py writes when the model says "unknown") must
    # not produce a sentence claiming one.
    vague = describe_profile({"primary_language": "Dutch", "script": "Latin", "era": ""})
    R.check("empty era does not invent one", "a  book" not in vague and "None" not in vague)

    reflowed = build_page_prompt(None, preserve_layout=False)
    R.check("layout flag changes the instruction", "Reflow" in reflowed)
    R.check("layout preserved by default", "Preserve the original line breaks" in blind)


# ---------------------------------------------------------------------
# Scanning and export
# ---------------------------------------------------------------------

def test_scan_and_export():
    print("\nScanning and export")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "scans"
        (root / "dutch").mkdir(parents=True)
        (root / ".git").mkdir(parents=True)

        make_pdf(root / "dutch" / "eerste.pdf", pages=4)
        make_pdf(root / "tweede.pdf", pages=2)
        make_pdf(root / ".git" / "ignored.pdf", pages=1)
        (root / "notes.txt").write_text("not a pdf")

        config = Config(
            pdf_root=root,
            db_path=Path(tmp) / "scan.db",
            export_dir=Path(tmp) / "out",
            machine_id="test-machine",
        )
        database = Database(config.db_path)

        summary = scan(config, database, log=lambda *a, **k: None)
        R.check("found both real PDFs", summary["found"] == 2, str(summary))
        R.check("both registered as new", summary["added"] == 2, str(summary))
        R.check("all six pages queued", summary["pages_added"] == 6, str(summary))

        books = database.list_books()
        R.check("two books listed", len(books) == 2, f"got {len(books)}")

        # Rescan must be a no-op.
        again = scan(config, database, log=lambda *a, **k: None)
        R.check("rescan adds nothing", again["added"] == 0 and again["pages_added"] == 0)

        # Fill in some results, including a flagged one, then export.
        book_id = int(books[0]["id"])
        database.set_book_profile(book_id, {
            "primary_language": "Dutch",
            "script": "Latin",
            "era": "19th-century",
        })
        database.save_page_result(
            book_id, 1, PAGE_DONE,
            {
                "page_type": "text",
                "transcription": "Van de Nederlandsche Taal",
                "footnotes": "1. Zie boven.",
                "printed_page_number": "xii",
                "languages": ["Dutch"],
                "has_uncertain_text": False,
                "note": "",
            },
            {
                "model": "gemini-3.5-flash-lite",
                "prompt_version": "page-v1",
                "app_version": "0.1.0",
                "media_resolution": "ultra_high",
                "render_dpi": 300,
                "image_format": "png",
                "machine_id": "test-machine",
                "key_label": "acct-01",
                "input_tokens": 2400,
                "output_tokens": 900,
                "latency_ms": 3100,
            },
        )
        database.save_page_result(
            book_id, 2, PAGE_FLAGGED,
            {"page_type": "text", "transcription": "ab"}, {},
            flag_reason="Classified as text but only 2 characters were returned",
        )

        manifest = export_book(database, book_id, config.export_dir)
        jsonl = config.export_dir / manifest["transcriptions_file"]
        R.check("jsonl file written", jsonl.exists())

        lines = jsonl.read_text(encoding="utf-8").strip().split("\n")
        R.check("one line per page", len(lines) == 4, f"got {len(lines)}")

        first = json.loads(lines[0])
        R.check("printed page number preserved", first["printed_page_number"] == "xii")
        R.check("footnotes kept separate", first["footnotes"] == "1. Zie boven.")
        R.check(
            "provenance recorded",
            first["provenance"]["media_resolution"] == "ultra_high"
            and first["provenance"]["render_dpi"] == 300,
            str(first["provenance"]),
        )
        R.check("token usage recorded", first["provenance"]["input_tokens"] == 2400)

        second = json.loads(lines[1])
        R.check("flagged page carries its reason", "2 characters" in second["flag_reason"])

        R.check("manifest carries the book profile", manifest["profile"]["era"] == "19th-century")
        R.check(
            "manifest counts statuses",
            manifest["page_status_counts"].get("done") == 1
            and manifest["page_status_counts"].get("flagged") == 1,
            str(manifest["page_status_counts"]),
        )

        index = export_all(config, database, log=lambda *a, **k: None)
        R.check("index covers both books", index["book_count"] == 2)
        R.check("index records the machine", index["machine_id"] == "test-machine")
        R.check("index.json on disk", (config.export_dir / "index.json").exists())

        # Unicode must survive as real characters, not \uXXXX escapes.
        database.save_page_result(
            book_id, 3, PAGE_DONE,
            {"page_type": "text", "transcription": "ગુજરાતી પાઠ — æthelred"}, {},
        )
        export_book(database, book_id, config.export_dir)
        raw = jsonl.read_text(encoding="utf-8")
        R.check("non-Latin text written unescaped", "ગુજરાતી" in raw)


def test_config_env():
    print("\nConfiguration")

    config = Config()
    R.check("default DPI is 300", config.dpi == 300, str(config.dpi))
    R.check("default format is PNG", config.image_format == "png")
    R.check(
        "pages are sent in colour by default",
        config.greyscale is False,
        str(config.greyscale),
    )
    R.check("media resolution maxed", config.media_resolution == "ultra_high")
    R.check("PNG mime type correct", config.image_mime_type == "image/png")
    R.check(
        "thinking is off by default (unverifiable field name)",
        config.thinking_level is None,
        str(config.thinking_level),
    )
    R.check("profiler starts after page 15", config.profile_start_after_page == 15)

    webp = Config(image_format="webp")
    R.check("webp mime type correct", webp.image_mime_type == "image/webp")


# ---------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------

def main():
    print("=" * 66)
    print("Ledger — test suite")
    print("=" * 66)

    for test in (
        test_pacific_date,
        test_daily_reset,
        test_pool_exhaustion,
        test_429_daily_vs_throttle,
        test_429_learns_real_limit,
        test_dead_key,
        test_dead_is_terminal,
        test_rpm_pacing,
        test_pool_load_spreading,
        test_book_and_page_idempotency,
        test_claim_is_atomic,
        test_release_does_not_burn_attempts,
        test_failure_escalates_to_failed,
        test_stale_claims_reclaimed,
        test_key_persistence,
        test_render,
        test_page_content_analysis,
        test_profiler_page_selection,
        test_fingerprint,
        test_quality_heuristics,
        test_error_classification,
        test_response_parsing,
        test_prompts,
        test_scan_and_export,
        test_config_env,
    ):
        test()

    print("\n" + "=" * 66)
    print(f"{R.passed} passed, {len(R.failed)} failed")
    if R.failed:
        print("\nFailures:")
        for failure in R.failed:
            print(f"  - {failure}")
    print("=" * 66)

    return 1 if R.failed else 0


if __name__ == "__main__":
    sys.exit(main())
