"""
Ledger — command line interface.

The engine is fully usable from here, before any web UI exists. That is
deliberate: it means the transcription pipeline can be tested and run on a
headless machine, and the UI (coming next) becomes a view over this rather than
the only way in.

After `pip install -e .` you get a `ledger` command that works from anywhere:

    ledger check            # validate setup (add --live to test one request)
    ledger keys import keys.json
    ledger keys list
    ledger scan
    ledger run
    ledger serve            # web UI at http://127.0.0.1:8000
    ledger status
    ledger export
    ledger requeue --flagged

Without installing, use `python -m ledger.cli <command>` instead — but note
that only resolves when the current directory IS the project root, because
Python finds the `ledger` package via the current directory.

The keys file is JSON and should be kept out of version control:

    [
      {"label": "acct-01", "secret": "AIza...", "rpm_limit": 10, "rpd_limit": 250},
      {"label": "acct-02", "secret": "AIza...", "rpm_limit": 10, "rpd_limit": 250}
    ]

Set rpd_limit from what AI Studio actually shows for that account rather than a
figure from a blog post -- Google no longer publishes free-tier daily limits and
says the numbers are not guaranteed. If you set it too high the pool will learn
the real ceiling from repeated 429s and correct itself, but starting close is
better.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from pathlib import Path

from .config import APP_VERSION, Config
from .db import PAGE_FAILED, PAGE_FLAGGED, Database
from .export import export_all
from .scanner import scan
from .worker import Engine


def _open(config: Config) -> Database:
    return Database(config.db_path)


# ---------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------

def cmd_scan(config: Config, args) -> int:
    database = _open(config)

    def log(message, level="info"):
        database.log(message, level=level)
        print(f"[{level}] {message}")

    scan(config, database, log=log)
    return 0


def cmd_keys(config: Config, args) -> int:
    database = _open(config)

    if args.keys_action == "import":
        path = Path(args.file)
        if not path.exists():
            print(f"Keys file not found: {path}", file=sys.stderr)
            return 1

        entries = json.loads(path.read_text(encoding="utf-8"))
        for entry in entries:
            database.add_key(
                label=entry["label"],
                secret=entry["secret"],
                rpm_limit=int(entry.get("rpm_limit", 10)),
                rpd_limit=int(entry.get("rpd_limit", 250)),
            )
        print(f"Imported or updated {len(entries)} key(s).")
        return 0

    if args.keys_action == "list":
        keys = database.load_keys()
        if not keys:
            print("No keys configured. Import some with: keys import keys.json")
            return 0

        # Secrets are never printed. Only enough to identify a key.
        print(f"{'LABEL':<16} {'STATE':<10} {'USED':>10}  {'RPM':>4}  NOTE")
        for key in keys:
            usage = f"{key.used_today}/{key.rpd_limit}"
            print(
                f"{key.label:<16} {key.state:<10} {usage:>10}  "
                f"{key.rpm_limit:>4}  {key.last_error}"
            )
        return 0

    if args.keys_action == "remove":
        removed = database.delete_key(args.label)
        print(f"Removed {removed} key(s) labelled {args.label!r}.")
        return 0

    return 1


def cmd_status(config: Config, args) -> int:
    database = _open(config)
    engine = Engine(config, database)
    status = engine.status()

    queue = status["queue"]
    capacity = status["capacity"]

    print(f"Ledger {APP_VERSION} — machine {status['machine_id']}")
    print()
    print("Queue")
    print(f"  total       {queue['total']:>8}")
    print(f"  done        {queue['done']:>8}")
    print(f"  pending     {queue['pending']:>8}")
    print(f"  flagged     {queue['flagged']:>8}")
    print(f"  failed      {queue['failed']:>8}")
    print(f"  in progress {queue['in_progress']:>8}")
    print()
    print("Quota today (resets midnight US Pacific)")
    print(f"  keys live       {capacity['keys_live']:>8}")
    print(f"  keys dead       {capacity['keys_dead']:>8}")
    print(f"  requests used   {capacity['used_today']:>8}")
    print(f"  requests left   {capacity['remaining_today']:>8}")
    print(f"  reset in        {capacity['seconds_to_reset'] // 3600:>6}h")

    # A rough finishing estimate is more useful than a raw page count when the
    # run spans weeks.
    if capacity["remaining_today"] > 0 and queue["pending"] > 0:
        daily = sum(k["rpd_limit"] for k in status["keys"] if k["state"] != "dead")
        if daily:
            days = queue["pending"] / daily
            print()
            print(f"At {daily} requests/day this is about {days:.1f} more day(s).")

    return 0


def cmd_check(config: Config, args) -> int:
    """
    Validate the setup before committing to a long run.

    Without `--live` this touches nothing external: it reports the resolved
    settings, confirms the PDF root and database are usable, and counts what is
    registered. With `--live` it sends exactly ONE real request so you can
    confirm the request shape is accepted before starting a job that runs for
    weeks — which is otherwise impossible to test without just starting it.
    """
    print(f"Ledger {APP_VERSION}")
    print()

    problems: list[str] = []
    warnings: list[str] = []

    # --- Resolved settings ------------------------------------------
    print("Settings")
    print(f"  machine id        {config.machine_id}")
    print(f"  model             {config.model}")
    print(f"  media resolution  {config.media_resolution}")
    print(f"  render            {config.dpi} DPI, {config.image_format}"
          f"{', greyscale' if config.greyscale else ''}"
          f"{', deskew' if config.deskew else ''}")
    print(f"  thinking          {config.thinking_level or 'off (field omitted)'}")
    print(f"  book profiling    "
          f"{'on' if config.profile_books else 'off'}"
          f"{f', from after page {config.profile_start_after_page}' if config.profile_books else ''}")
    print(f"  workers           up to {config.max_workers}")
    print()

    # --- PDF root ---------------------------------------------------
    print("PDF root")
    root = Path(config.pdf_root)
    if not root.exists():
        problems.append(
            f"PDF root does not exist: {root}. Set LEDGER_PDF_ROOT."
        )
        print(f"  {root}  — MISSING")
    elif not root.is_dir():
        problems.append(f"PDF root is not a directory: {root}")
        print(f"  {root}  — NOT A DIRECTORY")
    else:
        pdf_count = sum(
            1 for path in root.rglob("*")
            if path.is_file() and path.suffix.lower() == ".pdf"
            and not any(part in config.skip_dirs for part in path.parts)
        )
        print(f"  {root.resolve()}")
        print(f"  {pdf_count} PDF(s) on disk")
        if pdf_count == 0:
            warnings.append("No PDFs found under the root — nothing to do.")
    print()

    # --- Database ---------------------------------------------------
    # SQLite needs no installation or server: it ships with Python and the
    # file plus its schema are created on first use.
    print("Database")
    try:
        database = _open(config)
        summary = database.queue_summary()
        keys = database.load_keys()
        print(f"  {Path(config.db_path).resolve()}")
        print(f"  {len(database.list_books())} book(s), "
              f"{summary['total']} page(s) registered")
        print(f"  {summary['done']} done, {summary['pending']} pending, "
              f"{summary['flagged']} flagged, {summary['failed']} failed")
    except Exception as exc:
        problems.append(f"Cannot open the database: {exc}")
        print(f"  {config.db_path}  — FAILED: {exc}")
        keys = []
    print()

    # --- Keys -------------------------------------------------------
    print("API keys")
    if not keys:
        problems.append(
            "No keys imported. Run: ledger keys import keys.json"
        )
        print("  none imported")
    else:
        engine = Engine(config, database)
        capacity = engine.pool.capacity_today()
        print(f"  {capacity['keys_total']} imported, "
              f"{capacity['keys_live']} live, {capacity['keys_dead']} dead")
        print(f"  {capacity['used_today']} request(s) used today, "
              f"{capacity['remaining_today']} remaining")
        print(f"  quota resets in {capacity['seconds_to_reset'] // 3600}h "
              "(midnight US Pacific)")

        if capacity["keys_dead"]:
            warnings.append(
                f"{capacity['keys_dead']} key(s) are dead and will never be "
                "retried. Check `ledger keys list`."
            )
        if capacity["keys_live"] == 0:
            problems.append("No live keys — the engine cannot run.")

        # Duplicate secrets across "different" keys is a silent trap: they
        # share one project quota, so the pool believes it has more capacity
        # than it does.
        secrets = [k.secret for k in keys]
        if len(set(secrets)) != len(secrets):
            problems.append(
                "Two or more keys have the SAME secret. They share one "
                "project's quota, so the pool will overcount its capacity."
            )
    print()

    # --- One live request -------------------------------------------
    if args.live:
        print("Live request (spends exactly 1 request)")

        if problems:
            print("  skipped — fix the problems above first")
        else:
            code = _live_check(config, database, print)
            if code != 0:
                problems.append("The live request failed. See above.")
        print()

    # --- Verdict ----------------------------------------------------
    for warning in warnings:
        print(f"WARNING: {warning}")
    for problem in problems:
        print(f"PROBLEM: {problem}")

    if problems:
        print("\nNot ready to run.")
        return 1

    if not args.live:
        print("Setup looks fine. Add --live to send one real test request.")
    else:
        print("Ready to run.")
    return 0


def _live_check(config: Config, database, log) -> int:
    """
    Render one real page and transcribe it, reporting exactly what happened.

    This is the single most valuable thing to do before a multi-week run: it
    proves the request shape is accepted, the key works, and the model returns
    parseable output — none of which can be confirmed any other way short of
    starting the job.
    """
    from .gemini import (
        BadResponse,
        GeminiClient,
        KeyRejected,
        RateLimited,
        TransientError,
    )
    from .render import render_page, select_text_pages

    books = database.list_books()
    if not books:
        log("  no books registered — run `ledger scan` first")
        return 1

    book = books[0]
    pdf_path = Path(config.pdf_root) / book["rel_path"]

    # Pick a page that actually has text on it, so a blank page does not make
    # a working setup look broken.
    try:
        pages, strategy = select_text_pages(
            pdf_path, count=1,
            start_after_page=config.profile_start_after_page,
            scan_limit=config.profile_scan_limit,
        )
    except Exception as exc:
        log(f"  could not inspect “{book['title']}”: {exc}")
        return 1

    page_no = pages[0] if pages else 1
    log(f"  book   “{book['title']}”")
    log(f"  page   {page_no}"
        f"{'' if pages else ' (no text page found; using page 1)'}")

    try:
        image_bytes = render_page(
            pdf_path, page_no,
            dpi=config.dpi, greyscale=config.greyscale,
            image_format=config.image_format, deskew=config.deskew,
        )
        log(f"  render {len(image_bytes) // 1024} KB {config.image_format}")
    except Exception as exc:
        log(f"  RENDER FAILED: {exc}")
        return 1

    engine = Engine(config, database)
    key = engine.pool.acquire()
    if key is None:
        log("  no key available right now (all exhausted or cooling down)")
        return 1

    client = GeminiClient(config)

    try:
        result = client.transcribe_page(key.secret, image_bytes, None)
    except RateLimited as exc:
        engine.pool.record_rate_limited(key, str(exc), exc.retry_after)
        log(f"  RATE LIMITED on “{key.label}”: {exc}")
        log("  The request shape is probably fine — this is a quota response.")
        return 1
    except KeyRejected as exc:
        engine.pool.record_dead(key, str(exc))
        log(f"  KEY REJECTED “{key.label}”: {exc}")
        log("  That key is invalid or its project is disabled.")
        return 1
    except BadResponse as exc:
        engine.pool.record_success(key)
        log(f"  BAD REQUEST OR RESPONSE: {exc}")
        log("  If this mentions an unknown field, set LEDGER_THINKING_LEVEL=")
        log("  to drop the thinking config, then try again.")
        return 1
    except TransientError as exc:
        log(f"  TRANSIENT ERROR: {exc}")
        log("  The service or network is having trouble; try again shortly.")
        return 1

    engine.pool.record_success(key)

    log(f"  key    {key.label}")
    log(f"  type   {result['page_type']}")
    log(f"  tokens {result.get('_input_tokens', 0)} in, "
        f"{result.get('_output_tokens', 0)} out")
    log(f"  time   {result.get('_latency_ms', 0)} ms")

    transcription = (result.get("transcription") or "").strip()
    if transcription:
        preview = " ".join(transcription.split())[:300]
        log("")
        log("  First 300 characters returned — read this and judge the quality:")
        log(f"    {preview}")
    else:
        log("  (no text returned; check the page_type above)")

    return 0



def cmd_run(config: Config, args) -> int:
    database = _open(config)
    engine = Engine(config, database)

    # Ctrl-C should stop cleanly: workers finish the page they are on, commit
    # it, and exit. Killing mid-page is survivable too (the page returns to
    # pending on the next start), but this is tidier.
    def handle_interrupt(signum, frame):
        print("\nInterrupt received — finishing current pages…")
        engine.stop()

    signal.signal(signal.SIGINT, handle_interrupt)
    signal.signal(signal.SIGTERM, handle_interrupt)

    started = engine.start()
    if started == 0:
        return 1

    try:
        # Poll rather than join so we can print progress and notice the queue
        # draining.
        while engine.is_running:
            time.sleep(5)

            queue = database.queue_summary()
            if queue["pending"] == 0 and queue["in_progress"] == 0:
                print("Queue is empty.")
                engine.stop()
                break
    finally:
        engine.stop()
        engine.join(timeout=120)
        database.trim_events()

    queue = database.queue_summary()
    print(
        f"Stopped. {queue['done']} done, {queue['flagged']} flagged, "
        f"{queue['failed']} failed, {queue['pending']} still pending."
    )
    return 0


def cmd_serve(config: Config, args) -> int:
    """
    Serve the web UI and API.

    The UI is a view over the same engine `run` uses, not a separate
    implementation, so starting the queue from the browser and starting it from
    the command line do exactly the same thing.

    Binds to 127.0.0.1 by default. Use --host 0.0.0.0 to reach it from another
    machine on the LAN, but be aware there is no authentication: anyone who can
    reach the port can read your transcriptions and add keys.
    """
    import uvicorn

    from .api import create_app

    app = create_app(config)

    print(f"Ledger {APP_VERSION} — http://{args.host}:{args.port}")
    if args.host not in ("127.0.0.1", "localhost"):
        print(
            "  Reachable from the network, with no authentication. Only do "
            "this on a network you trust."
        )

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


def cmd_export(config: Config, args) -> int:
    database = _open(config)
    export_all(config, database)
    return 0


def cmd_requeue(config: Config, args) -> int:
    """
    Put flagged and/or failed pages back on the queue.

    Explicitly manual. Each requeued page costs a fresh request from a scarce
    daily allowance, so this is a decision you make rather than something the
    engine does on its own.
    """
    database = _open(config)

    statuses = []
    if args.flagged:
        statuses.append(PAGE_FLAGGED)
    if args.failed:
        statuses.append(PAGE_FAILED)

    if not statuses:
        print("Nothing selected. Pass --flagged and/or --failed.")
        return 1

    count = database.requeue_pages(statuses, book_id=args.book)
    print(f"Requeued {count} page(s): {', '.join(statuses)}.")
    return 0


# ---------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ledger",
        description="Ledger — batch transcription of scanned historical books.",
    )
    parser.add_argument(
        "--env",
        default=".env",
        help="Path to the .env file (default: .env)",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("scan", help="Scan the PDF root and queue pages")

    keys_parser = subparsers.add_parser("keys", help="Manage API keys")
    keys_sub = keys_parser.add_subparsers(dest="keys_action", required=True)
    import_parser = keys_sub.add_parser("import", help="Import keys from a JSON file")
    import_parser.add_argument("file")
    keys_sub.add_parser("list", help="List keys and their quota standing")
    remove_parser = keys_sub.add_parser("remove", help="Remove a key by label")
    remove_parser.add_argument("label")

    check_parser = subparsers.add_parser(
        "check", help="Validate the setup before a long run"
    )
    check_parser.add_argument(
        "--live",
        action="store_true",
        help="Also send ONE real request to confirm the API accepts it",
    )

    subparsers.add_parser("status", help="Show queue and quota status")
    subparsers.add_parser("run", help="Run the transcription engine")

    serve_parser = subparsers.add_parser("serve", help="Serve the web UI")
    serve_parser.add_argument(
        "--host", default="127.0.0.1",
        help="Interface to bind (default 127.0.0.1; use 0.0.0.0 for LAN access)",
    )
    serve_parser.add_argument("--port", type=int, default=8000)
    subparsers.add_parser("export", help="Write JSONL exports and manifests")

    requeue_parser = subparsers.add_parser(
        "requeue", help="Return flagged/failed pages to the queue"
    )
    requeue_parser.add_argument("--flagged", action="store_true")
    requeue_parser.add_argument("--failed", action="store_true")
    requeue_parser.add_argument(
        "--book", type=int, default=None, help="Limit to one book id"
    )

    return parser


HANDLERS = {
    "scan": cmd_scan,
    "check": cmd_check,
    "keys": cmd_keys,
    "status": cmd_status,
    "run": cmd_run,
    "serve": cmd_serve,
    "export": cmd_export,
    "requeue": cmd_requeue,
}


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    config = Config.from_env(args.env)

    try:
        return HANDLERS[args.command](config, args)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except (FileNotFoundError, NotADirectoryError, PermissionError) as exc:
        # Setup mistakes — a missing folder, an unwritable database. These are
        # the most common first-run failures and deserve one clear line rather
        # than a traceback that buries the message.
        print(f"\nError: {exc}", file=sys.stderr)
        print("Run `ledger check` to validate your setup.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
