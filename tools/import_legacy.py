#!/usr/bin/env python3
"""
Import a transcriptions backup from the old single-file HTML app.

One-off migration. Run it once, then delete it if you like — nothing else in
Ledger depends on it.

    python tools/import_legacy.py transcriptions-backup-2026-08-28.json
    python tools/import_legacy.py transcriptions-backup-2026-08-28.json --apply

It does NOT write anything unless you pass --apply. The default is a dry run
that prints exactly what would change, because this writes into a database that
may already hold real work.

Run `ledger scan` first. Books are matched to what the scan registered, by their
path relative to the PDF root, so the importer never invents a book that has no
PDF behind it.


How the old statuses map
------------------------

    synced                ->  done      (already transcribed and pushed to the sheet)
    transcribed_unsynced  ->  done      (transcribed, never pushed)
    pending               ->  pending   (left alone)
    error                 ->  pending   (see below)

Errors becoming `pending` rather than `failed` is the one interesting decision,
and the backup settles it. Every error record in this export carries the same
message: the browser revoked the folder permission mid-run. Those pages were
never read at all, so there is nothing wrong with them — marking them `failed`
would permanently exclude perfectly good pages from a run that can now actually
read them. The importer verifies this assumption per record and says so if it
finds an error that looks like something else.

A page the old app classified as `text` but returned no text for is imported as
`flagged`, not `done` — that is what Ledger's own quality check would have done
with it, and it puts those pages in the review queue rather than silently into
the corpus.


What the old backup does not contain
------------------------------------

The old app recorded page number, type, text and a note. It did not record:

  - the printed page number as it appears on the page (needed for citation)
  - footnotes separated from body text
  - detected languages
  - any provenance: which model, at what resolution, on which key

So imported pages will be thinner than pages transcribed by the new engine.
Their provenance is stamped as an import so you can always tell them apart, and
the summary at the end tells you what re-transcribing them would cost.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

# Import Ledger from the parent directory, so this works from a checkout
# without needing the package installed.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ledger.config import APP_VERSION, Config
from ledger.db import (
    PAGE_DONE,
    PAGE_FLAGGED,
    PAGE_PENDING,
    Database,
)


# The old app's error message when the browser withdrew folder permission. Any
# error record matching this was never read, so the page is untouched work
# rather than a failure.
FOLDER_REVOKED_MARKER = "could not be read"

# Stamped into the provenance of every imported page.
IMPORT_MODEL = "legacy-html-app"
IMPORT_PROMPT_VERSION = "legacy-html"


def old_status_to_new(page: dict) -> tuple[str, str]:
    """
    Map one old page record onto a Ledger status.

    Returns (status, reason). `reason` is only used for flagged pages.
    """
    status = page.get("status")
    page_type = page.get("pageType") or ""
    text = (page.get("text") or "").strip()

    if status in ("synced", "transcribed_unsynced"):
        # Ledger's own quality check treats a "text" page with no text as
        # suspect. Applying the same rule here keeps imported pages held to the
        # same standard as new ones instead of trusting them more.
        if page_type == "text" and not text:
            return PAGE_FLAGGED, "Classified as text but the old app returned no text"
        return PAGE_DONE, ""

    # Everything else goes back on the queue.
    return PAGE_PENDING, ""


def load_backup(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"That file is not valid JSON: {exc}")

    books = data.get("books")
    if not isinstance(books, list):
        raise SystemExit(
            "This does not look like a Ledger HTML backup — no 'books' list at "
            "the top level."
        )

    print(f"Backup exported {data.get('exportedAt', 'at an unknown time')}")
    print(f"{len(books)} book(s) in the file")
    return books


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Import an old HTML-app backup into Ledger's database."
    )
    parser.add_argument("backup", help="Path to transcriptions-backup-*.json")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write. Without this it is a dry run.",
    )
    parser.add_argument(
        "--env", default=".env", help="Path to the .env file (default: .env)"
    )
    args = parser.parse_args()

    config = Config.from_env(args.env)
    database = Database(config.db_path)

    backup_path = Path(args.backup)
    if not backup_path.exists():
        raise SystemExit(f"No such file: {backup_path}")

    books = load_backup(backup_path)

    # ------------------------------------------------------------------
    # Match the backup's books against what `ledger scan` registered.
    # ------------------------------------------------------------------

    registered = database.list_books()
    if not registered:
        raise SystemExit(
            "No books in the database yet. Run `ledger scan` first so the "
            "importer has real PDFs to attach these transcriptions to."
        )

    by_rel_path = {row["rel_path"]: row for row in registered}
    by_file_name = {row["file_name"]: row for row in registered}

    print(f"{len(registered)} book(s) registered in {config.db_path}")
    print()

    matched: list[tuple[dict, object]] = []
    unmatched: list[str] = []

    for book in books:
        # The old app stored the path as a list of path segments.
        rel_path = "/".join(book.get("relativePath") or [book.get("fileName", "")])

        target = by_rel_path.get(rel_path)
        if target is None:
            # Fall back to the bare filename: the folder may have been
            # reorganised between the two apps.
            target = by_file_name.get(book.get("fileName", ""))

        if target is None:
            unmatched.append(f"{book.get('name')} ({rel_path})")
        else:
            matched.append((book, target))

    if unmatched:
        print(f"{len(unmatched)} book(s) in the backup have no matching PDF:")
        for name in unmatched:
            print(f"  - {name}")
        print("  These are skipped. Check they are under LEDGER_PDF_ROOT and rescan.")
        print()

    # ------------------------------------------------------------------
    # Work out every change before writing any of it.
    # ------------------------------------------------------------------

    planned: list[tuple[int, int, str, dict, str]] = []
    totals = Counter()
    suspicious_errors: list[str] = []
    per_book: list[tuple[str, Counter, int]] = []

    for book, target in matched:
        book_id = int(target["id"])

        # Only pages that exist on both sides. If the PDF has changed since the
        # old run, the page numbers may no longer line up.
        existing = {
            int(row["page_no"]): row for row in database.pages_for_book(book_id)
        }

        book_counts = Counter()
        skipped_missing = 0

        for page in book.get("pages", []):
            page_no = page.get("n")
            if not isinstance(page_no, int) or page_no not in existing:
                skipped_missing += 1
                continue

            current = existing[page_no]

            # Never overwrite work the new engine has already done. This is
            # what makes the script safe to run twice.
            if current["status"] in (PAGE_DONE, PAGE_FLAGGED):
                book_counts["already done here"] += 1
                totals["already done here"] += 1
                continue

            # Sanity-check the assumption behind mapping errors to pending.
            if page.get("status") == "error":
                note = page.get("note") or ""
                if FOLDER_REVOKED_MARKER not in note:
                    suspicious_errors.append(
                        f"{book.get('name')} p.{page_no}: {note[:90]}"
                    )

            new_status, flag_reason = old_status_to_new(page)

            if new_status == PAGE_PENDING:
                # Already pending in the new database; nothing to write.
                book_counts["left pending"] += 1
                totals["left pending"] += 1
                continue

            planned.append((book_id, page_no, new_status, page, flag_reason))
            book_counts[new_status] += 1
            totals[new_status] += 1

        per_book.append((book.get("name", "?"), book_counts, skipped_missing))

    # ------------------------------------------------------------------
    # Report.
    # ------------------------------------------------------------------

    print(f"{'BOOK':<10} {'IMPORT':>8} {'FLAG':>6} {'PENDING':>8} {'HAVE':>6} {'N/A':>5}")
    for name, counts, skipped in per_book:
        print(
            f"{name:<10} {counts[PAGE_DONE]:>8} {counts[PAGE_FLAGGED]:>6} "
            f"{counts['left pending']:>8} {counts['already done here']:>6} "
            f"{skipped:>5}"
        )

    print()
    print("IMPORT   pages to bring in as transcribed")
    print("FLAG     brought in but marked for review (classified text, no text)")
    print("PENDING  left on the queue — the old app never transcribed these")
    print("HAVE     already transcribed by the new engine; left untouched")
    print("N/A      page numbers not present in the current PDF")
    print()

    if suspicious_errors:
        print(
            f"{len(suspicious_errors)} error record(s) do NOT look like the "
            "folder-permission problem:"
        )
        for line in suspicious_errors[:10]:
            print(f"  - {line}")
        print("  They are still queued for a retry, which is harmless.")
        print()

    print(f"Total to import as transcribed : {totals[PAGE_DONE]}")
    print(f"Total to import but flag       : {totals[PAGE_FLAGGED]}")
    print(f"Total left on the queue        : {totals['left pending']}")
    print(f"Total already done here        : {totals['already done here']}")
    print()

    # What the import saves, in the only unit that matters on the free tier.
    saved = totals[PAGE_DONE] + totals[PAGE_FLAGGED]
    print(f"Importing saves {saved} request(s) of your daily allowance.")
    print()
    print("Worth knowing: the old backup has no printed page numbers, no")
    print("footnotes separated from body text, and no provenance. Imported")
    print("pages will be thinner than ones the new engine produces. If the")
    print(f"printed page numbers matter for citation, re-transcribing these {saved}")
    print("pages is the way to get them — at 20 keys that is well under a day.")
    print("To do that instead, skip this import and just run the queue.")
    print()

    if not args.apply:
        print("DRY RUN — nothing written. Add --apply to commit these changes.")
        return 0

    # ------------------------------------------------------------------
    # Write.
    # ------------------------------------------------------------------

    provenance = {
        "model": IMPORT_MODEL,
        "prompt_version": IMPORT_PROMPT_VERSION,
        "app_version": APP_VERSION,
        "media_resolution": "",
        "render_dpi": 0,
        "image_format": "",
        # Stamped so these pages are always identifiable as imported rather
        # than produced by this machine.
        "machine_id": f"imported-from-html-app",
        "key_label": "",
        "input_tokens": 0,
        "output_tokens": 0,
        "latency_ms": 0,
    }

    written = 0
    for book_id, page_no, status, page, flag_reason in planned:
        result = {
            "page_type": page.get("pageType") or "text",
            "transcription": page.get("text") or "",
            # The old app had no concept of either of these.
            "footnotes": "",
            "printed_page_number": "",
            "languages": [],
            "has_uncertain_text": False,
            "note": page.get("note") or "",
        }

        database.save_page_result(
            book_id, page_no, status, result, provenance, flag_reason
        )
        written += 1

        if written % 500 == 0:
            print(f"  {written}/{len(planned)} pages…", flush=True)

    database.log(
        f"Imported {written} page(s) from {backup_path.name} "
        f"({totals[PAGE_DONE]} transcribed, {totals[PAGE_FLAGGED]} flagged).",
        "ok",
    )

    print(f"Imported {written} page(s).")
    print()
    summary = database.queue_summary()
    print("Queue now:")
    print(f"  done        {summary['done']:>8}")
    print(f"  flagged     {summary['flagged']:>8}")
    print(f"  pending     {summary['pending']:>8}")
    print(f"  failed      {summary['failed']:>8}")
    print()
    print("Start the queue and it will pick up only what is genuinely left.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
