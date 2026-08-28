"""
Ledger — HTTP API and web server.

Serves the single-file UI and exposes the engine over JSON. Everything the
browser used to do itself now happens here:

    browser folder handle   ->  a server-side path in .env  (never revoked)
    IndexedDB               ->  SQLite
    pdf.js in a canvas      ->  PyMuPDF, streamed as PNG
    browser worker loop     ->  the Engine's threads
    per-page Sheets POST    ->  one batched publish, on a button

That first line is the whole reason the rewrite exists: the browser's File
System Access API revokes folder permission, which made unattended remote
deployment impossible.

The UI is a view over the engine, never the owner of it. `ledger run` on a
headless box and this server drive exactly the same code, and both can even run
against the same database — SQLite is in WAL mode, so a reader does not block
the writers.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel

from .appsscript import APPS_SCRIPT_CODE, SCRIPT_VERSION
from .config import APP_VERSION, Config
from .db import PAGE_FAILED, PAGE_FLAGGED, Database
from .export import export_all
from .quota import KeyPool
from .render import RenderError, render_page
from .scanner import scan
from .sheets import PublishJob, SheetsError, test_connection
from .worker import Engine


STATIC_DIR = Path(__file__).parent / "static"

SETTING_SHEETS_URL = "sheets_url"
SETTING_SHEETS_SECRET = "sheets_secret"


# ---------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------

class KeyInput(BaseModel):
    label: str
    secret: str
    rpm_limit: int = 10
    rpd_limit: int = 250


class SheetsSettings(BaseModel):
    sheets_url: str = ""
    sheets_secret: str = ""


class RequeueInput(BaseModel):
    flagged: bool = False
    failed: bool = False


class PublishInput(BaseModel):
    book_id: int | None = None


# ---------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------

def create_app(config: Config) -> FastAPI:
    """
    Build the application.

    A factory rather than a module-level app so the config is explicit and the
    tests can spin up an isolated instance against a temporary database.
    """
    app = FastAPI(title="Ledger", version=APP_VERSION, docs_url="/api/docs")

    database = Database(config.db_path)
    engine = Engine(config, database)

    # Any in_progress rows belong to a process that no longer exists.
    reclaimed = database.reset_stale_claims()
    if reclaimed:
        database.log(f"Reclaimed {reclaimed} page(s) from a previous run.", "warn")

    # One publish at a time per process. Held here rather than in a global so
    # it dies with the app.
    state: dict = {"publish": None}

    # -----------------------------------------------------------------
    # UI
    # -----------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def index():
        html = STATIC_DIR / "index.html"
        if not html.exists():
            raise HTTPException(500, "index.html is missing from ledger/static")
        return HTMLResponse(html.read_text(encoding="utf-8"))

    # -----------------------------------------------------------------
    # Status and control
    # -----------------------------------------------------------------

    @app.get("/api/status")
    def status():
        """Everything the header, keys table and progress need, in one call."""
        payload = engine.status()

        # The read-only engine settings, so the UI can show what it is actually
        # running with rather than the user having to open .env.
        payload["config"] = {
            "pdf_root": str(config.pdf_root),
            "db_path": str(config.db_path),
            "export_dir": str(config.export_dir),
            "model": config.model,
            "media_resolution": config.media_resolution,
            "dpi": config.dpi,
            "image_format": config.image_format,
            "greyscale": config.greyscale,
            "deskew": config.deskew,
            "thinking_level": config.thinking_level or "",
            "preserve_layout": config.preserve_layout,
            "profile_books": config.profile_books,
            "profile_start_after_page": config.profile_start_after_page,
            "max_workers": config.max_workers,
        }

        publish = state["publish"]
        payload["publish"] = publish.snapshot() if publish else {"state": "idle"}

        # Rough finishing estimate, which is the number that actually matters
        # when a run spans weeks.
        daily = sum(
            k["rpd_limit"] for k in payload["keys"] if k["state"] != "dead"
        )
        pending = payload["queue"]["pending"]
        payload["days_remaining"] = round(pending / daily, 1) if daily else None

        return payload

    @app.post("/api/run/start")
    def run_start():
        started = engine.start()
        if started == 0:
            raise HTTPException(
                400,
                "Could not start. Check that keys are imported and have quota "
                "left today.",
            )
        return {"workers": started}

    @app.post("/api/run/stop")
    def run_stop():
        engine.stop()
        return {"running": False}

    @app.post("/api/scan")
    def rescan():
        try:
            return scan(
                config,
                database,
                log=lambda message, level="info": database.log(message, level),
            )
        except FileNotFoundError as exc:
            raise HTTPException(400, str(exc))

    # -----------------------------------------------------------------
    # Books and pages
    # -----------------------------------------------------------------

    @app.get("/api/books")
    def list_books():
        return [
            {
                "id": int(row["id"]),
                "title": row["title"],
                "rel_path": row["rel_path"],
                "total_pages": int(row["total_pages"]),
                "pages_done": int(row["pages_done"] or 0),
                "pages_pending": int(row["pages_pending"] or 0),
                "pages_flagged": int(row["pages_flagged"] or 0),
                "pages_failed": int(row["pages_failed"] or 0),
                "profiled": bool(row["profile_json"]),
            }
            for row in database.list_books()
        ]

    @app.get("/api/books/{book_id}")
    def get_book(book_id: int):
        book = database.get_book(book_id)
        if book is None:
            raise HTTPException(404, "No such book")

        return {
            "id": int(book["id"]),
            "title": book["title"],
            "rel_path": book["rel_path"],
            "file_name": book["file_name"],
            "total_pages": int(book["total_pages"]),
            "profile": database.get_book_profile(book_id),
            "pages": [
                {
                    "page_no": int(page["page_no"]),
                    "status": page["status"],
                    "page_type": page["page_type"],
                    "printed_page_number": page["printed_page_number"],
                    "transcription": page["transcription"],
                    "footnotes": page["footnotes"],
                    "note": page["note"],
                    "flag_reason": page["flag_reason"],
                    "last_error": page["last_error"],
                    "attempts": int(page["attempts"]),
                    "key_label": page["key_label"],
                    "model": page["model"],
                    "render_dpi": int(page["render_dpi"]),
                    "media_resolution": page["media_resolution"],
                    "input_tokens": int(page["input_tokens"]),
                    "output_tokens": int(page["output_tokens"]),
                    "latency_ms": int(page["latency_ms"]),
                }
                for page in database.pages_for_book(book_id)
            ],
        }

    @app.get("/api/books/{book_id}/pages/{page_no}/image")
    def page_image(book_id: int, page_no: int, dpi: int = Query(110, ge=40, le=400)):
        """
        Render a page for the reading pane.

        Deliberately low DPI by default: this is for a human comparing the scan
        against the transcription on screen, not for the model. Rendering the
        full 300 DPI page here would be several megabytes for no visible gain.
        """
        book = database.get_book(book_id)
        if book is None:
            raise HTTPException(404, "No such book")

        pdf_path = Path(config.pdf_root) / book["rel_path"]

        try:
            image_bytes = render_page(
                pdf_path,
                page_no,
                dpi=dpi,
                greyscale=config.greyscale,
                image_format="png",
                deskew=False,
            )
        except RenderError as exc:
            raise HTTPException(404, str(exc))

        return Response(
            content=image_bytes,
            media_type="image/png",
            # The scan on disk never changes, so let the browser keep it.
            headers={"Cache-Control": "public, max-age=3600"},
        )

    @app.post("/api/books/{book_id}/requeue")
    def requeue_book(book_id: int, body: RequeueInput):
        statuses = []
        if body.flagged:
            statuses.append(PAGE_FLAGGED)
        if body.failed:
            statuses.append(PAGE_FAILED)

        if not statuses:
            raise HTTPException(400, "Select flagged and/or failed pages.")

        count = database.requeue_pages(statuses, book_id=book_id)
        database.log(f"Requeued {count} page(s).", "info", book_id=book_id)
        return {"requeued": count}

    @app.post("/api/requeue")
    def requeue_all(body: RequeueInput):
        statuses = []
        if body.flagged:
            statuses.append(PAGE_FLAGGED)
        if body.failed:
            statuses.append(PAGE_FAILED)

        if not statuses:
            raise HTTPException(400, "Select flagged and/or failed pages.")

        count = database.requeue_pages(statuses)
        database.log(f"Requeued {count} page(s) across all books.", "info")
        return {"requeued": count}

    @app.delete("/api/books/{book_id}")
    def remove_book(book_id: int):
        book = database.get_book(book_id)
        if book is None:
            raise HTTPException(404, "No such book")

        database.delete_book(book_id)
        database.log(
            f"Removed “{book['title']}” and its transcriptions. The PDF on "
            "disk is untouched; a rescan will register it again as new.",
            "warn",
        )
        return {"removed": True}

    # -----------------------------------------------------------------
    # Keys
    # -----------------------------------------------------------------

    @app.get("/api/keys")
    def list_keys():
        """
        Key standing. Secrets are never returned — only enough of the tail to
        tell two keys apart when checking them against AI Studio.
        """
        by_label = {key.label: key for key in database.load_keys()}

        return [
            {
                **snapshot,
                "secret_tail": (by_label[snapshot["label"]].secret or "")[-4:],
            }
            for snapshot in engine.pool.snapshot()
            if snapshot["label"] in by_label
        ]

    @app.post("/api/keys")
    def add_key(body: KeyInput):
        if not body.secret.strip():
            raise HTTPException(400, "The key cannot be empty.")

        database.add_key(
            label=body.label.strip(),
            secret=body.secret.strip(),
            rpm_limit=body.rpm_limit,
            rpd_limit=body.rpd_limit,
        )
        _reload_keys()
        return {"saved": True}

    @app.delete("/api/keys/{label}")
    def delete_key(label: str):
        removed = database.delete_key(label)
        _reload_keys()
        return {"removed": removed}

    @app.post("/api/keys/{label}/revive")
    def revive_key(label: str):
        """
        Return a dead or exhausted key to service.

        Manual on purpose. The engine never revives a dead key by itself,
        because a key it retired was rejected outright and retrying it forever
        would waste a worker thread. But you may have fixed the account, or the
        rejection may have come from a network problem, so there has to be a
        way to say so.
        """
        keys = {key.label: key for key in database.load_keys()}
        if label not in keys:
            raise HTTPException(404, "No such key")

        key = keys[label]
        key.state = "active"
        key.cooldown_until = 0.0
        key.consecutive_rate_limits = 0
        key.last_error = ""
        database.save_key(key)

        _reload_keys()
        database.log(f"Key “{label}” returned to service manually.", "info")
        return {"revived": True}

    def _reload_keys() -> None:
        """
        Rebuild the pool after keys change.

        The pool holds its keys in memory, so an edit through the UI has to be
        reflected there as well as in the database.
        """
        engine.pool = KeyPool(database.load_keys(), on_change=database.save_key)

    # -----------------------------------------------------------------
    # Settings
    # -----------------------------------------------------------------

    @app.get("/api/settings")
    def get_settings():
        secret = database.get_setting(SETTING_SHEETS_SECRET)
        return {
            "sheets_url": database.get_setting(SETTING_SHEETS_URL),
            # Never echo the secret back; just say whether one is set.
            "sheets_secret_set": bool(secret),
        }

    @app.put("/api/settings")
    def put_settings(body: SheetsSettings):
        database.set_setting(SETTING_SHEETS_URL, body.sheets_url.strip())

        # An empty submitted secret means "leave it alone", so you can save the
        # URL without having to retype the secret every time.
        if body.sheets_secret.strip():
            database.set_setting(SETTING_SHEETS_SECRET, body.sheets_secret.strip())

        return {"saved": True}

    # -----------------------------------------------------------------
    # Google Sheets
    # -----------------------------------------------------------------

    @app.get("/api/appsscript")
    def apps_script():
        return {"version": SCRIPT_VERSION, "code": APPS_SCRIPT_CODE}

    @app.post("/api/sheets/test")
    def sheets_test():
        url = database.get_setting(SETTING_SHEETS_URL)
        if not url:
            raise HTTPException(400, "Set the Apps Script Web App URL first.")

        try:
            return test_connection(url, database.get_setting(SETTING_SHEETS_SECRET))
        except SheetsError as exc:
            raise HTTPException(400, str(exc))

    @app.post("/api/sheets/publish")
    def sheets_publish(body: PublishInput):
        url = database.get_setting(SETTING_SHEETS_URL)
        if not url:
            raise HTTPException(400, "Set the Apps Script Web App URL first.")

        current = state["publish"]
        if current and current.snapshot()["state"] == "running":
            raise HTTPException(409, "A publish is already running.")

        job = PublishJob(
            config,
            database,
            url,
            database.get_setting(SETTING_SHEETS_SECRET),
            book_id=body.book_id,
        )
        state["publish"] = job
        job.start()
        return job.snapshot()

    @app.post("/api/sheets/publish/cancel")
    def sheets_publish_cancel():
        job = state["publish"]
        if job:
            job.cancel()
        return {"cancelled": True}

    # -----------------------------------------------------------------
    # Export
    # -----------------------------------------------------------------

    @app.post("/api/export")
    def export():
        index = export_all(
            config, database, log=lambda message: database.log(message, "info")
        )
        return {
            "book_count": index["book_count"],
            "export_dir": str(config.export_dir),
        }

    @app.get("/api/export/download")
    def download_export():
        """Hand back index.json, the entry point to everything just exported."""
        path = Path(config.export_dir) / "index.json"
        if not path.exists():
            raise HTTPException(404, "Nothing exported yet. Run an export first.")
        return FileResponse(path, filename="index.json")

    # -----------------------------------------------------------------
    # Log
    # -----------------------------------------------------------------

    @app.get("/api/events")
    def events(after_id: int = 0, limit: int = Query(200, ge=1, le=1000)):
        """
        Log lines, oldest first so the console can append.

        `after_id` lets the UI fetch only what it has not seen, instead of
        pulling the whole tail every second.
        """
        rows = database.recent_events(limit=limit, after_id=after_id)
        return [
            {
                "id": int(row["id"]),
                "ts": row["ts"],
                "level": row["level"],
                "message": row["message"],
            }
            for row in reversed(rows)
        ]

    return app
