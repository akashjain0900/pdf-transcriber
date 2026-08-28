"""
Ledger — Old Book Transcriber.

Batch transcription of scanned historical PDFs via the Gemini API, designed for
a long-running free-tier job spread across several machines and many API keys.

Module map, in the order data flows through it:

    config.py    every tunable setting, read from the environment
    scanner.py   walks the PDF folder and fills the work queue
    render.py    rasterises one PDF page to PNG bytes
    prompts.py   the transcription and book-profiling instructions
    gemini.py    the API client, and the error classification everything relies on
    quota.py     API key pool, Pacific-time daily accounting, RPM pacing
    db.py        SQLite: the durable record of every page and every key
    worker.py    the engine that ties the above together
    export.py    turns the database into the JSON files you keep
    sheets.py    batched publishing to Google Sheets
    appsscript.py  the Apps Script source the UI hands you to paste
    api.py       HTTP API and web server
    static/      the single-file web UI
    cli.py       command line entry point
"""

from .config import APP_VERSION, Config
from .db import Database
from .worker import Engine

__all__ = ["APP_VERSION", "Config", "Database", "Engine"]
__version__ = APP_VERSION
