#!/usr/bin/env python3
"""
Ledger — bundled application entry point.

This is the script PyInstaller freezes. Bundlers freeze a *script*, not a
console_scripts entry point, so `ledger` from `pip install -e .` is no help
here and this file exists to be that starting point.

Double-clicking the executable starts the server and opens the browser. Any
command-line argument instead runs the ordinary CLI, so the single executable
covers both:

    Ledger.exe                        -> server + browser
    Ledger.exe check --live           -> the CLI
    Ledger.exe import-legacy b.json   -> the CLI
    Ledger.exe --port 9000            -> server on another port

Run from a checkout it behaves the same way, which means the bundling path can
be tested without building anything:

    python ledger_app.py
"""

from __future__ import annotations

import sys
import threading
import time
import webbrowser

# CLI-only flags this launcher handles itself. Anything else is passed to the
# ordinary CLI.
SERVER_FLAGS = {"--port", "--host", "--no-browser"}


def looks_like_cli(argv: list[str]) -> bool:
    """
    Decide whether to run the CLI or the server.

    A bare launch (double-click) means "start the app". A first argument that is
    not one of the server flags is a CLI subcommand.
    """
    if not argv:
        return False
    return argv[0] not in SERVER_FLAGS and not argv[0].startswith("-")


def open_browser_when_ready(url: str, delay: float = 1.5) -> None:
    """
    Open the browser shortly after the server starts.

    A fixed short delay rather than polling the port: uvicorn is listening well
    inside this window, and a failed poll loop would be more confusing than a
    browser tab that needs one refresh.
    """
    def target() -> None:
        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception:
            # No browser available (a headless server, say). Not a failure —
            # the URL is printed either way.
            pass

    threading.Thread(target=target, daemon=True).start()


def run_server(argv: list[str]) -> int:
    import argparse

    import uvicorn

    # A frozen console app block-buffers stdout, so none of the startup
    # information below appears until the buffer fills — which, for a server
    # that then sits quietly, is never. Someone double-clicking the executable
    # would see an empty window and reasonably conclude it had hung.
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass

    from ledger.api import create_app
    from ledger.config import APP_VERSION, Config, app_base_dir, is_frozen

    parser = argparse.ArgumentParser(prog="Ledger", add_help=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--no-browser", action="store_true", help="Do not open a browser window"
    )
    args = parser.parse_args(argv)

    config = Config.from_env()
    url = f"http://{'127.0.0.1' if args.host == '0.0.0.0' else args.host}:{args.port}"

    print(f"Ledger {APP_VERSION}")
    print(f"  data folder : {app_base_dir()}")
    print(f"  PDF folder  : {config.pdf_root}")
    print(f"  database    : {config.db_path}")
    print()
    print(f"  Open {url}")
    print()

    if args.host not in ("127.0.0.1", "localhost"):
        print(
            "  Reachable from the network, with no authentication. Anyone who\n"
            "  can reach this port can read your transcriptions and add API\n"
            "  keys. Only do this on a network you trust.\n"
        )

    print("  Close this window to stop. A run in progress resumes where it")
    print("  left off next time.")
    print()

    app = create_app(config)

    if not args.no_browser:
        open_browser_when_ready(url)

    # log_level is kept quiet: the app's own event log is the useful record, and
    # a request line per page render would bury it.
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


def main() -> int:
    argv = sys.argv[1:]

    if looks_like_cli(argv):
        from ledger.cli import main as cli_main

        return cli_main(argv)

    return run_server(argv)


if __name__ == "__main__":
    # A frozen console app that raises on startup closes its window instantly,
    # taking the traceback with it. Holding the window open is the difference
    # between a fixable error and "it just doesn't work".
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except KeyboardInterrupt:
        print("\nStopped.")
        raise SystemExit(0)
    except Exception:
        import traceback

        traceback.print_exc()
        print()
        print("Ledger could not start. The error above is the reason.")
        if getattr(sys, "frozen", False):
            try:
                input("Press Enter to close...")
            except EOFError:
                pass
        raise SystemExit(1)
