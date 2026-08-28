"""
Ledger — export.

Turns the database into the JSON files you actually keep. The database is the
working store; these files are the artifact.

Format choice: JSONL (one JSON object per line) rather than one big JSON array.
A 450-page book is 450 lines you can grep, stream, tail, or append to, and a
truncated file still parses line by line up to the break. A single top-level
array has none of those properties and has to be held in memory whole.

Each book produces two files:

    <book>.jsonl      one line per page, full detail
    <book>.manifest.json   book-level metadata and page-status counts

Plus one index.json across the whole export, which is what the Sheets upload
step reads.

The provenance block on every page is the part that will matter in three years,
when someone asks which model produced a passage and at what settings. It costs
nothing to write now and cannot be reconstructed afterwards.
"""

from __future__ import annotations

import json
from pathlib import Path

from .config import APP_VERSION


def _safe_filename(name: str) -> str:
    """
    Make a book title safe to use as a filename on any platform.

    Deliberately conservative: Windows, macOS and Linux disagree about what is
    legal, and these exports will be copied between machines.
    """
    cleaned = "".join(
        character if character.isalnum() or character in " ._-" else "_"
        for character in name
    ).strip()

    cleaned = " ".join(cleaned.split())  # collapse runs of whitespace
    return (cleaned or "book")[:120]


def _page_to_record(row, book_row) -> dict:
    """
    Build the full export record for one page.

    Grouped into four blocks so the file is readable by eye: what the page is,
    what was read from it, how confident we are, and how it was produced.
    """
    return {
        # --- Identity ------------------------------------------------
        "book": {
            "id": int(book_row["id"]),
            "title": book_row["title"],
            "file_name": book_row["file_name"],
            "rel_path": book_row["rel_path"],
            "fingerprint": book_row["fingerprint"],
        },
        "pdf_page_number": int(row["page_no"]),

        # Often differs from the PDF index because of front matter, inserted
        # plates and printers' errors. This is the number a citation uses.
        "printed_page_number": row["printed_page_number"] or None,

        # --- Content -------------------------------------------------
        "page_type": row["page_type"],
        "transcription": row["transcription"],
        "footnotes": row["footnotes"] or "",
        "languages": json.loads(row["languages"] or "[]"),

        # --- Confidence ----------------------------------------------
        "status": row["status"],
        "has_uncertain_text": bool(row["has_uncertain_text"]),
        "note": row["note"] or "",
        "flag_reason": row["flag_reason"] or "",
        "attempts": int(row["attempts"]),
        "last_error": row["last_error"] or "",

        # --- Provenance ----------------------------------------------
        "provenance": {
            "model": row["model"],
            "prompt_version": row["prompt_version"],
            "app_version": row["app_version"],
            "media_resolution": row["media_resolution"],
            "render_dpi": int(row["render_dpi"]),
            "image_format": row["image_format"],
            "machine_id": row["machine_id"],
            "key_label": row["key_label"],
            "input_tokens": int(row["input_tokens"]),
            "output_tokens": int(row["output_tokens"]),
            "latency_ms": int(row["latency_ms"]),
            "transcribed_at": row["updated_at"],
        },
    }


def export_book(database, book_id: int, out_dir: Path) -> dict:
    """
    Write one book's JSONL and manifest. Returns the manifest dict.

    Pages with no result yet are included with their status, so the file is a
    complete picture of the book rather than only its finished parts. That
    matters when you are merging exports from several machines and need to know
    what is genuinely missing versus merely absent from this file.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    book_row = database.get_book(book_id)
    if book_row is None:
        raise ValueError(f"No book with id {book_id}")

    page_rows = database.pages_for_book(book_id)

    stem = f"{book_id:04d}_{_safe_filename(book_row['title'])}"
    jsonl_path = out_dir / f"{stem}.jsonl"
    manifest_path = out_dir / f"{stem}.manifest.json"

    counts: dict[str, int] = {}
    total_input_tokens = 0
    total_output_tokens = 0

    with jsonl_path.open("w", encoding="utf-8") as handle:
        for row in page_rows:
            counts[row["status"]] = counts.get(row["status"], 0) + 1
            total_input_tokens += int(row["input_tokens"])
            total_output_tokens += int(row["output_tokens"])

            record = _page_to_record(row, book_row)

            # ensure_ascii=False is essential: these texts are full of
            # accented Latin, Gujarati and archaic characters, and escaping
            # them to \uXXXX makes the file unreadable to a human reviewer.
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    manifest = {
        "book_id": int(book_row["id"]),
        "title": book_row["title"],
        "file_name": book_row["file_name"],
        "rel_path": book_row["rel_path"],
        "fingerprint": book_row["fingerprint"],
        "file_size": int(book_row["file_size"]),
        "total_pages": int(book_row["total_pages"]),
        "profile": json.loads(book_row["profile_json"]) if book_row["profile_json"] else None,
        "page_status_counts": counts,
        "tokens": {
            "input": total_input_tokens,
            "output": total_output_tokens,
        },
        "transcriptions_file": jsonl_path.name,
        "exported_by_app_version": APP_VERSION,
    }

    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return manifest


def export_all(config, database, log=print) -> dict:
    """
    Export every book, plus an index.json tying them together.

    The index is what the Sheets upload reads, so it deliberately carries the
    per-book completeness counts: you should be able to see what is unfinished
    before you publish anything.
    """
    out_dir = Path(config.export_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    manifests = []
    for book_row in database.list_books():
        manifest = export_book(database, int(book_row["id"]), out_dir)
        manifests.append(manifest)
        log(
            f"Exported “{manifest['title']}” "
            f"({manifest['page_status_counts'].get('done', 0)}"
            f"/{manifest['total_pages']} pages done)"
        )

    index = {
        "machine_id": config.machine_id,
        "app_version": APP_VERSION,
        "book_count": len(manifests),
        "queue_summary": database.queue_summary(),
        "books": manifests,
    }

    index_path = out_dir / "index.json"
    index_path.write_text(
        json.dumps(index, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    log(f"Wrote {len(manifests)} book export(s) and index.json to {out_dir}")
    return index
