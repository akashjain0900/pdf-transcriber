"""
Ledger — end-to-end smoke test.

Drives the REAL worker loop, database, key pool, renderer and exporter. Only
the network call into Gemini is replaced with a stub, because that is the one
part that cannot be exercised without live credentials.

The stub deliberately misbehaves in the ways the real API does: it rate limits,
returns a 500 once, hands back one degenerate page, and rejects one key
outright. The point is to prove the whole pipeline survives all four and still
lands the right number of pages in the right states — which is exactly what a
three-week unattended run depends on.

Run with:  python tests/test_end_to_end.py
"""

from __future__ import annotations

import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pymupdf

from ledger.config import Config
from ledger.db import Database
from ledger.export import export_all
from ledger.gemini import KeyRejected, RateLimited, TransientError
from ledger.scanner import scan
from ledger.worker import Engine


BODY_LINE = "Eene uitvoerige beschrijving van de oude gebruiken dezer landen,"


def add_dense_page(doc, lines: int = 34, size: int = 9) -> None:
    """A page of body text, as dense as a real book page."""
    page = doc.new_page(width=420, height=595)
    y = 70.0
    for _ in range(lines):
        page.insert_text((50, y), BODY_LINE, fontsize=size)
        y += size * 1.7


def add_blank_page(doc) -> None:
    doc.new_page(width=420, height=595)


def add_plate_page(doc) -> None:
    page = doc.new_page(width=420, height=595)
    page.draw_rect(
        pymupdf.Rect(80, 100, 340, 380), color=(0, 0, 0), fill=(0.35, 0.35, 0.35)
    )


def build_corpus(root: Path) -> int:
    """
    Three books, each exercising a different profiler selection path.

    Returns the total page count.

    - boek-1: front matter then body text  -> profiled from after page 15
    - boek-2: short, body text throughout  -> profiled via the fallback
    - boek-3: nothing but plates and blanks -> not profiled at all, and
              importantly spends no request discovering that
    """
    # boek-1: 15 pages of front matter, then 5 of text. 20 pages.
    doc = pymupdf.open()
    for _ in range(15):
        add_blank_page(doc)
    for _ in range(5):
        add_dense_page(doc)
    doc.save(root / "boek-1.pdf")
    doc.close()

    # boek-2: 6 pages, all body text.
    doc = pymupdf.open()
    for _ in range(6):
        add_dense_page(doc)
    doc.save(root / "boek-2.pdf")
    doc.close()

    # boek-3: 6 pages, all plates and blanks.
    doc = pymupdf.open()
    for _ in range(3):
        add_plate_page(doc)
        add_blank_page(doc)
    doc.save(root / "boek-3.pdf")
    doc.close()

    return 20 + 6 + 6


# Only two of the three books have text worth profiling.
EXPECTED_PROFILE_CALLS = 2


class StubGemini:
    """
    Stands in for GeminiClient, reproducing the failure modes that matter.

    Counts calls so the test can assert on how many requests were actually
    spent — the number that matters most on a metered free tier.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.transcribe_calls = 0
        self.profile_calls = 0
        self.injected = {
            "rate_limited": 0,
            "transient": 0,
            "degenerate": 0,
            "key_rejected": 0,
        }

    def profile_book(self, api_key, sample_images):
        with self.lock:
            self.profile_calls += 1
        return {
            "primary_language": "Dutch",
            "other_languages": ["Latin"],
            "script": "Latin",
            "era": "19th-century",
            "typeface": "roman",
            "orthographic_notes": "Long s appears in older sections.",
            "title_as_printed": "Oude Gebruiken",
        }

    def transcribe_page(self, api_key, image_bytes, profile):
        with self.lock:
            self.transcribe_calls += 1
            call = self.transcribe_calls

        # A key gets revoked mid-run. The engine must retire it and carry on
        # using the others rather than stalling.
        if call == 4 and self.injected["key_rejected"] == 0:
            self.injected["key_rejected"] += 1
            raise KeyRejected("HTTP 403: PERMISSION_DENIED")

        # A per-minute throttle. Must not count as a page attempt.
        if call == 6 and self.injected["rate_limited"] == 0:
            self.injected["rate_limited"] += 1
            raise RateLimited("Too many requests", retry_after=0.2)

        # A server-side blip. Also must not blame the page.
        if call == 8 and self.injected["transient"] == 0:
            self.injected["transient"] += 1
            raise TransientError("HTTP 503: overloaded")

        # One page comes back as a repetition loop and must be FLAGGED, not
        # silently accepted into the corpus.
        if call == 10 and self.injected["degenerate"] == 0:
            self.injected["degenerate"] += 1
            return {
                "page_type": "text",
                "transcription": "van den berg en zijne vrouwe " * 60,
                "footnotes": "",
                "printed_page_number": "13",
                "languages": ["Dutch"],
                "has_uncertain_text": False,
                "note": "",
                "_latency_ms": 900,
                "_input_tokens": 2400,
                "_output_tokens": 4000,
                "_prompt_version": "page-v1",
            }

        return {
            "page_type": "text",
            "transcription": (
                "Van de oude gebruiken dezer landen valt veel te verhalen, "
                "gelijk de schrijver in het voorgaande heeft aangetoond."
            ),
            "footnotes": "1. Zie het vorige hoofdstuk.",
            "printed_page_number": str(call),
            "languages": ["Dutch"],
            "has_uncertain_text": False,
            "note": "",
            "_latency_ms": 850,
            "_input_tokens": 2400,
            "_output_tokens": 700,
            "_prompt_version": "page-v1",
        }


def main() -> int:
    print("=" * 66)
    print("Ledger — end-to-end smoke test (Gemini stubbed)")
    print("=" * 66)

    failures: list[str] = []

    def check(name, condition, detail=""):
        if condition:
            print(f"  PASS  {name}")
        else:
            failures.append(f"{name}: {detail}")
            print(f"  FAIL  {name}  {detail}")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        root = tmp_path / "scans"
        root.mkdir()

        expected_pages = build_corpus(root)
        book_count = 3

        config = Config(
            pdf_root=root,
            db_path=tmp_path / "e2e.db",
            export_dir=tmp_path / "out",
            machine_id="smoke-test",
            dpi=150,               # keep the test fast
            max_workers=4,
            max_concurrent_renders=2,
            profile_books=True,
            profile_sample_pages=2,
            profile_start_after_page=15,
            idle_sleep_seconds=1,
        )

        database = Database(config.db_path)
        scan(config, database, log=lambda *a, **k: None)

        check(
            "scan queued every page",
            database.queue_summary()["pending"] == expected_pages,
            str(database.queue_summary()),
        )

        # Four keys, generous daily limits, pacing off so the test is quick.
        for index in range(4):
            database.add_key(
                f"acct-{index + 1}", f"secret-{index + 1}",
                rpm_limit=0, rpd_limit=100,
            )

        engine = Engine(config, database)
        stub = StubGemini()
        engine.client = stub  # the only substitution

        print("\nRunning the engine")
        started = engine.start()
        check("workers started", started == 4, f"got {started}")

        engine.join(timeout=120)

        summary = database.queue_summary()
        print(f"\n  queue: {summary}")

        # --- Correctness of the run -----------------------------------
        check(
            "every page reached a terminal state",
            summary["pending"] == 0 and summary["in_progress"] == 0,
            str(summary),
        )
        check(
            "the degenerate page was flagged, not accepted",
            summary["flagged"] == 1,
            f"flagged={summary['flagged']}",
        )
        check(
            "everything else completed",
            summary["done"] == expected_pages - 1,
            f"done={summary['done']} of {expected_pages - 1}",
        )
        check("nothing was written off as failed", summary["failed"] == 0, str(summary))

        # --- Injected faults were all exercised -----------------------
        check("a rate limit was injected and survived", stub.injected["rate_limited"] == 1)
        check("a 503 was injected and survived", stub.injected["transient"] == 1)
        check("a key rejection was injected and survived", stub.injected["key_rejected"] == 1)

        keys = {k["label"]: k for k in engine.pool.snapshot()}
        dead = [label for label, k in keys.items() if k["state"] == "dead"]
        check("the rejected key was retired", len(dead) == 1, f"dead={dead}")

        # --- Quota accounting ----------------------------------------
        # Each book is profiled once, and each page costs one request. The
        # three injected failures each cost a retry. Nothing should be wildly
        # off this, and in particular no page should have been done twice.
        check(
            "only books with text were profiled",
            stub.profile_calls == EXPECTED_PROFILE_CALLS,
            f"got {stub.profile_calls}, expected {EXPECTED_PROFILE_CALLS}",
        )

        recorded = engine.pool.capacity_today()["used_today"]
        print(f"  requests recorded against keys: {recorded}")
        print(f"  transcribe calls made:          {stub.transcribe_calls}")
        check(
            "no page was transcribed twice",
            stub.transcribe_calls <= expected_pages + 3,
            f"{stub.transcribe_calls} calls for {expected_pages} pages",
        )

        # --- Provenance and profiles ---------------------------------
        books = database.list_books()

        # Every book gets a profile RECORD (so it is not re-inspected on every
        # page), but the all-plate book's record is empty.
        import json as _json
        profiles = {
            b["title"]: _json.loads(b["profile_json"]) if b["profile_json"] else None
            for b in books
        }

        check(
            "the long book was profiled from after its front matter",
            profiles["boek-1"] is not None
            and profiles["boek-1"].get("_sample_strategy") == "after front matter"
            and all(p > 15 for p in profiles["boek-1"].get("_sample_pages", [])),
            str(profiles["boek-1"]),
        )
        check(
            "the short book used the fallback",
            profiles["boek-2"] is not None
            and "fallback" in profiles["boek-2"].get("_sample_strategy", ""),
            str(profiles["boek-2"]),
        )
        check(
            "the all-plate book was not profiled and spent no request",
            profiles["boek-3"] == {},
            str(profiles["boek-3"]),
        )
        check(
            "the all-plate book's pages still transcribed fine",
            all(
                r["status"] in ("done", "flagged")
                for r in database.pages_for_book(
                    int([b for b in books if b["title"] == "boek-3"][0]["id"])
                )
            ),
        )

        first_book_id = int(books[0]["id"])
        rows = database.pages_for_book(first_book_id)
        done_rows = [r for r in rows if r["status"] == "done"]
        check(
            "machine id stamped on results",
            done_rows[0]["machine_id"] == "smoke-test",
            done_rows[0]["machine_id"],
        )
        check(
            "prompt version stamped on results",
            done_rows[0]["prompt_version"] == "page-v1",
            done_rows[0]["prompt_version"],
        )
        check(
            "render settings recorded",
            done_rows[0]["render_dpi"] == 150 and done_rows[0]["image_format"] == "png",
            f"dpi={done_rows[0]['render_dpi']} fmt={done_rows[0]['image_format']}",
        )
        check(
            "key attribution recorded",
            done_rows[0]["key_label"].startswith("acct-"),
            done_rows[0]["key_label"],
        )

        # --- Resume behaviour ----------------------------------------
        # Requeue the flagged page and confirm a second run picks up only that
        # one, rather than redoing finished work.
        before = stub.transcribe_calls
        database.requeue_pages(["flagged"])

        engine2 = Engine(config, database)
        engine2.client = stub
        engine2.start()
        engine2.join(timeout=60)

        after = stub.transcribe_calls
        check(
            "requeue retried exactly one page",
            after - before == 1,
            f"{after - before} extra calls",
        )
        check(
            "the retried page now succeeds",
            database.queue_summary()["done"] == expected_pages,
            str(database.queue_summary()),
        )

        # --- Export --------------------------------------------------
        index = export_all(config, database, log=lambda *a, **k: None)
        check("export covers every book", index["book_count"] == book_count)

        jsonl_files = sorted(config.export_dir.glob("*.jsonl"))
        check("one jsonl per book", len(jsonl_files) == book_count, f"got {len(jsonl_files)}")

        total_lines = sum(
            len(path.read_text(encoding="utf-8").strip().split("\n"))
            for path in jsonl_files
        )
        check("exported one line per page", total_lines == expected_pages, f"got {total_lines}")

    print("\n" + "=" * 66)
    if failures:
        print(f"{len(failures)} failure(s):")
        for failure in failures:
            print(f"  - {failure}")
    else:
        print("End-to-end run clean.")
    print("=" * 66)

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
