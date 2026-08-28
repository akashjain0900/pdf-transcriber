"""
Ledger — folder scanner.

Walks the configured PDF root and registers every PDF as a book, with one page
row per page. This is the stage that replaces the browser's File System Access
API, and it is the whole reason the Python rewrite exists: a server-side path
cannot be revoked by a permission prompt.

Rescanning is always safe. Books are identified by their path relative to the
root, so an already-known PDF keeps every transcription it has. That makes
"the drive was remounted" or "I added forty more scans" an ordinary event
rather than a reason to redo work.
"""

from __future__ import annotations

from pathlib import Path

from .render import RenderError, fingerprint_file, page_count


def _iter_pdfs(root: Path, skip_dirs: tuple[str, ...]):
    """
    Yield (absolute_path, relative_path) for every PDF under root.

    Uses os.walk semantics via rglob but filters out noise directories, so a
    stray .git or recycle bin does not end up in the corpus.
    """
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue

        if path.suffix.lower() != ".pdf":
            continue

        # Skip anything sitting inside an excluded directory.
        if any(part in skip_dirs for part in path.parts):
            continue

        yield path, path.relative_to(root)


def scan(config, database, log=print) -> dict:
    """
    Scan the PDF root and bring the database in line with it.

    Returns a summary dict. Books that fail to open are reported and skipped
    rather than aborting the scan -- with a thousand files, one corrupt PDF
    should not stop you registering the other nine hundred and ninety-nine.
    """
    root = Path(config.pdf_root)

    if not root.exists():
        raise FileNotFoundError(
            f"PDF root does not exist: {root}. "
            "Set LEDGER_PDF_ROOT to the folder containing this machine's scans."
        )

    found = 0
    added = 0
    updated = 0
    skipped: list[str] = []
    pages_added = 0

    for absolute_path, relative_path in _iter_pdfs(root, config.skip_dirs):
        found += 1
        rel = str(relative_path).replace("\\", "/")

        try:
            total_pages = page_count(absolute_path)
            fingerprint = fingerprint_file(
                absolute_path,
                full=config.full_file_hash,
            )
        except RenderError as exc:
            skipped.append(f"{rel}: {exc}")
            log(f"Skipped {rel}: {exc}", "warn")
            continue
        except OSError as exc:
            skipped.append(f"{rel}: {exc}")
            log(f"Skipped {rel}: {exc}", "warn")
            continue

        book_id, was_created = database.upsert_book(
            rel_path=rel,
            file_name=absolute_path.name,
            # Filename without extension is the working title. The profiler
            # can pick up the real printed title from the title page later.
            title=absolute_path.stem,
            fingerprint=fingerprint,
            file_size=absolute_path.stat().st_size,
            total_pages=total_pages,
        )

        new_pages = database.create_pages_for_book(book_id, total_pages)
        pages_added += new_pages

        if was_created:
            added += 1
            log(f"Registered “{absolute_path.stem}” ({total_pages} pages)", "ok")
        else:
            updated += 1
            if new_pages:
                # The PDF gained pages since we last looked. Only the new
                # ones are queued; existing transcriptions are untouched.
                log(
                    f"“{absolute_path.stem}” gained {new_pages} new page(s)",
                    "warn",
                )

    summary = {
        "found": found,
        "added": added,
        "updated": updated,
        "pages_added": pages_added,
        "skipped": skipped,
    }

    log(
        f"Scan complete: {found} PDF(s) found, {added} new, {updated} already "
        f"known, {pages_added} page(s) queued.",
        "ok",
    )
    return summary
