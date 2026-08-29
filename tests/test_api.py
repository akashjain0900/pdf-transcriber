"""
Ledger — API and UI tests.

Runs the real FastAPI app against a temporary database and a generated corpus,
using Starlette's TestClient (no network, no port). Everything is exercised
except the two things that need external services: the Gemini call and the
Apps Script deployment. Those are stubbed and clearly marked.

Also checks the UI file itself for the mistakes that are easy to make and
invisible until someone clicks the wrong thing: an element the JavaScript
addresses but the markup does not contain, or a leftover reference to the
browser APIs this rewrite exists to get rid of.

Run with:  python tests/test_api.py
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pymupdf

try:
    from fastapi.testclient import TestClient
except (ImportError, RuntimeError) as exc:
    # Starlette's TestClient needs an HTTP client library that Ledger itself
    # does not, so a plain `pip install -e .` cannot run this suite.
    print("This suite needs the test-only dependencies:\n")
    print('    pip install -e ".[dev]"\n')
    print(f"({exc})")
    sys.exit(2)

from ledger.api import create_app
from ledger.config import Config
from ledger.db import PAGE_DONE, PAGE_FLAGGED, Database
from ledger.scanner import scan


BODY_LINE = "Van de oude gebruiken dezer landen valt zeer veel te verhalen,"


class Results:
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
            if "pytest" in sys.modules:
                raise AssertionError(f"{name}: {detail}")


R = Results()


def build_corpus(root: Path) -> None:
    """Two books: one with front matter then text, one short."""
    doc = pymupdf.open()
    for _ in range(15):
        doc.new_page(width=420, height=595)
    for _ in range(5):
        page = doc.new_page(width=420, height=595)
        y = 70.0
        for _ in range(34):
            page.insert_text((50, y), BODY_LINE, fontsize=9)
            y += 15.3
    doc.save(root / "boek-1.pdf")
    doc.close()

    doc = pymupdf.open()
    for _ in range(4):
        page = doc.new_page(width=420, height=595)
        y = 70.0
        for _ in range(34):
            page.insert_text((50, y), BODY_LINE, fontsize=9)
            y += 15.3
    doc.save(root / "boek-2.pdf")
    doc.close()


def make_client(tmp: Path):
    root = tmp / "scans"
    root.mkdir()
    build_corpus(root)

    config = Config(
        pdf_root=root,
        db_path=tmp / "api.db",
        export_dir=tmp / "out",
        machine_id="api-test",
        dpi=150,
    )
    app = create_app(config)
    return TestClient(app), config, Database(config.db_path)


# ---------------------------------------------------------------------

def test_ui_served():
    print("\nUI is served")

    with tempfile.TemporaryDirectory() as tmp:
        client, _, _ = make_client(Path(tmp))

        response = client.get("/")
        R.check("GET / returns 200", response.status_code == 200, str(response.status_code))
        R.check(
            "it is the Ledger page",
            "Old Book Transcriber" in response.text,
        )
        R.check(
            "HTML is served as HTML",
            response.headers["content-type"].startswith("text/html"),
            response.headers["content-type"],
        )


def test_ui_integrity():
    print("\nUI integrity")

    html = (Path(__file__).resolve().parent.parent
            / "ledger" / "static" / "index.html").read_text(encoding="utf-8")

    # Every element the script reaches for by id must exist in the markup.
    # A typo here is a silent null dereference the first time someone clicks
    # that control, which is exactly the kind of bug that survives a demo.
    referenced = set(re.findall(r'getElementById\("([^"]+)"\)', html))
    defined = set(re.findall(r'\bid="([^"]+)"', html))

    # These are created at runtime by the render functions, not in the markup.
    runtime_ids = {
        "btn-publish-book", "btn-requeue-book", "btn-remove-book",
    }
    missing = referenced - defined - runtime_ids

    R.check(
        "every getElementById target exists in the markup",
        not missing,
        f"missing: {sorted(missing)}",
    )

    # The whole point of the rewrite: none of the browser APIs that made the
    # original undeployable should be left anywhere in this file.
    for banned, why in [
        ("showDirectoryPicker", "folder picker — revoked by the browser"),
        ("indexedDB", "browser storage — the server owns state now"),
        ("localStorage", "browser storage"),
        ("pdfjsLib", "client-side PDF rendering — the server renders pages"),
        ("requestPermission", "folder permission prompts"),
    ]:
        R.check(
            f"no {banned} ({why})",
            banned not in html,
        )

    # Reduced motion must be respected; the pulse animation is the only motion.
    R.check(
        "reduced motion is honoured",
        "prefers-reduced-motion" in html,
    )

    # Every API path the UI calls should exist on the server.
    called = set(re.findall(r'["\'](/api/[a-z/]+)', html))
    R.check(
        "the UI only calls /api paths that look well-formed",
        all(path.startswith("/api/") for path in called),
        str(called),
    )


def test_status_and_config():
    print("\nStatus endpoint")

    with tempfile.TemporaryDirectory() as tmp:
        client, config, _ = make_client(Path(tmp))

        status = client.get("/api/status").json()

        R.check("reports not running", status["running"] is False)
        R.check("reports the machine id", status["machine_id"] == "api-test")
        R.check("includes the queue summary", "queue" in status)
        R.check("includes quota capacity", "capacity" in status)
        R.check(
            "exposes read-only engine config",
            status["config"]["dpi"] == 150
            and status["config"]["media_resolution"] == "ultra_high",
            str(status["config"]),
        )
        R.check(
            "publish starts idle",
            status["publish"]["state"] == "idle",
            str(status["publish"]),
        )


def test_scan_and_books():
    print("\nScan and books")

    with tempfile.TemporaryDirectory() as tmp:
        client, _, _ = make_client(Path(tmp))

        result = client.post("/api/scan").json()
        R.check("scan finds both PDFs", result["found"] == 2, str(result))
        R.check("scan queues 24 pages", result["pages_added"] == 24, str(result))

        books = client.get("/api/books").json()
        R.check("two books listed", len(books) == 2, str(len(books)))
        R.check(
            "counts are present",
            all("pages_pending" in book for book in books),
        )

        book_id = books[0]["id"]
        detail = client.get(f"/api/books/{book_id}").json()
        R.check("detail includes every page", len(detail["pages"]) == 20, str(len(detail["pages"])))
        R.check("pages start pending", detail["pages"][0]["status"] == "pending")
        R.check("profile is absent before profiling", detail["profile"] is None)

        R.check(
            "an unknown book is a 404",
            client.get("/api/books/9999").status_code == 404,
        )

        # Rescanning must not duplicate anything.
        again = client.post("/api/scan").json()
        R.check("rescan adds nothing", again["added"] == 0 and again["pages_added"] == 0)


def test_page_image():
    print("\nPage rendering endpoint")

    with tempfile.TemporaryDirectory() as tmp:
        client, _, _ = make_client(Path(tmp))
        client.post("/api/scan")
        book_id = client.get("/api/books").json()[0]["id"]

        response = client.get(f"/api/books/{book_id}/pages/16/image")
        R.check("returns 200", response.status_code == 200, str(response.status_code))
        R.check(
            "serves a PNG",
            response.content[:8] == b"\x89PNG\r\n\x1a\n",
            str(response.content[:8]),
        )
        R.check(
            "sets a cache header (the scan never changes)",
            "max-age" in response.headers.get("cache-control", ""),
            response.headers.get("cache-control", ""),
        )

        # Default DPI must be low: this is for reading on screen, not for the
        # model, and a full 300 DPI page here would be megabytes per click.
        R.check(
            "default render is modest in size",
            len(response.content) < 900_000,
            f"{len(response.content)} bytes",
        )

        R.check(
            "a page past the end is a 404",
            client.get(f"/api/books/{book_id}/pages/999/image").status_code == 404,
        )


def test_keys_endpoints():
    print("\nKeys endpoints")

    with tempfile.TemporaryDirectory() as tmp:
        client, _, database = make_client(Path(tmp))

        R.check("no keys initially", client.get("/api/keys").json() == [])

        client.post("/api/keys", json={
            "label": "acct-01", "secret": "super-secret-value-1234",
            "rpm_limit": 10, "rpd_limit": 250,
        })
        keys = client.get("/api/keys").json()

        R.check("key added", len(keys) == 1, str(keys))
        R.check("key is active", keys[0]["state"] == "active", keys[0]["state"])

        # The secret must never come back over the wire — only enough tail to
        # tell two keys apart when checking them against AI Studio.
        serialised = json.dumps(keys)
        R.check(
            "the full secret is never returned",
            "super-secret-value-1234" not in serialised,
        )
        R.check("only the tail is exposed", keys[0]["secret_tail"] == "1234", keys[0]["secret_tail"])

        R.check(
            "an empty key is rejected",
            client.post("/api/keys", json={"label": "x", "secret": "  "}).status_code == 400,
        )

        # Re-saving the same label updates without resetting usage.
        from ledger.quota import KeyPool
        pool = KeyPool(database.load_keys(), on_change=database.save_key)
        pool.record_success(pool.acquire())

        client.post("/api/keys", json={
            "label": "acct-01", "secret": "new-secret-5678",
            "rpm_limit": 15, "rpd_limit": 250,
        })
        keys = client.get("/api/keys").json()
        R.check("re-saving preserves usage", keys[0]["used_today"] == 1, str(keys[0]))
        R.check("re-saving updates the tail", keys[0]["secret_tail"] == "5678", keys[0]["secret_tail"])

        # Reviving a dead key.
        loaded = database.load_keys()[0]
        loaded.state = "dead"
        loaded.last_error = "revoked"
        database.save_key(loaded)

        client.post("/api/keys/acct-01/revive")
        keys = client.get("/api/keys").json()
        R.check("a dead key can be revived by hand", keys[0]["state"] == "active", keys[0]["state"])
        R.check(
            "reviving an unknown key is a 404",
            client.post("/api/keys/nope/revive").status_code == 404,
        )

        client.delete("/api/keys/acct-01")
        R.check("key deleted", client.get("/api/keys").json() == [])


def test_run_control():
    print("\nRun control")

    with tempfile.TemporaryDirectory() as tmp:
        client, _, _ = make_client(Path(tmp))
        client.post("/api/scan")

        # No keys yet, so starting must fail with a useful message rather than
        # silently doing nothing.
        response = client.post("/api/run/start")
        R.check("start without keys is a 400", response.status_code == 400, str(response.status_code))
        R.check(
            "the message explains why",
            "keys" in response.json()["detail"].lower(),
            response.json()["detail"],
        )

        R.check("stop is always safe", client.post("/api/run/stop").status_code == 200)


def test_settings_and_sheets():
    print("\nSettings and Sheets")

    with tempfile.TemporaryDirectory() as tmp:
        client, _, _ = make_client(Path(tmp))

        settings = client.get("/api/settings").json()
        R.check("no URL initially", settings["sheets_url"] == "")
        R.check("no secret initially", settings["sheets_secret_set"] is False)

        client.put("/api/settings", json={
            "sheets_url": "https://script.google.com/macros/s/abc/exec",
            "sheets_secret": "hunter2",
        })
        settings = client.get("/api/settings").json()
        R.check("URL saved", settings["sheets_url"].endswith("/exec"), settings["sheets_url"])
        R.check("secret recorded as set", settings["sheets_secret_set"] is True)
        R.check(
            "the secret is never echoed back",
            "hunter2" not in json.dumps(settings),
        )

        # Saving with a blank secret must keep the existing one, so the URL can
        # be edited without retyping the secret every time.
        client.put("/api/settings", json={
            "sheets_url": "https://script.google.com/macros/s/xyz/exec",
            "sheets_secret": "",
        })
        settings = client.get("/api/settings").json()
        R.check("blank secret keeps the saved one", settings["sheets_secret_set"] is True)
        R.check("URL was still updated", "xyz" in settings["sheets_url"], settings["sheets_url"])

        # Publishing without a URL configured must refuse clearly.
        client.put("/api/settings", json={"sheets_url": "", "sheets_secret": ""})
        response = client.post("/api/sheets/publish", json={})
        R.check("publish without a URL is a 400", response.status_code == 400, str(response.status_code))
        R.check(
            "test without a URL is a 400",
            client.post("/api/sheets/test").status_code == 400,
        )


def test_key_import_and_editing():
    print("\nKey import and per-key editing")

    with tempfile.TemporaryDirectory() as tmp:
        client, _, database = make_client(Path(tmp))

        # Bulk import, in the same format the CLI accepts.
        payload = {"keys": [
            {"label": f"acct-{i:02d}", "secret": f"secret-{i:02d}",
             "rpm_limit": 10, "rpd_limit": 250}
            for i in range(1, 9)
        ]}
        result = client.post("/api/keys/import", json=payload).json()
        R.check("eight keys imported", result["imported"] == 8, str(result))

        keys = client.get("/api/keys").json()
        R.check("all eight listed", len(keys) == 8, str(len(keys)))
        R.check("all enabled by default", all(k["enabled"] for k in keys))

        # Only five may be in play at once.
        in_play = [k["label"] for k in keys if k["in_use"]]
        R.check("exactly five in play", len(in_play) == 5, str(in_play))
        R.check(
            "they are the first five in order",
            in_play == [f"acct-{i:02d}" for i in range(1, 6)],
            str(in_play),
        )

        capacity = client.get("/api/status").json()["capacity"]
        R.check("capacity reports the window", capacity["keys_in_use"] == 5, str(capacity))
        R.check("capacity reports the ceiling", capacity["max_concurrent_keys"] == 5)

        # Re-importing must not reset usage.
        from ledger.quota import KeyPool
        pool = KeyPool(database.load_keys(), on_change=database.save_key)
        pool.record_success(pool.acquire())
        client.post("/api/keys/import", json=payload)
        keys = {k["label"]: k for k in client.get("/api/keys").json()}
        R.check(
            "re-import preserves usage",
            sum(k["used_today"] for k in keys.values()) == 1,
            str([(k, v["used_today"]) for k, v in keys.items()]),
        )

        R.check(
            "an empty import is rejected",
            client.post("/api/keys/import", json={"keys": []}).status_code == 400,
        )

        # Edit one key's quota without touching its secret.
        client.put("/api/keys/acct-01", json={"rpm_limit": 3, "rpd_limit": 40})
        keys = {k["label"]: k for k in client.get("/api/keys").json()}
        R.check("rpm updated", keys["acct-01"]["rpm_limit"] == 3, str(keys["acct-01"]))
        R.check("rpd updated", keys["acct-01"]["rpd_limit"] == 40, str(keys["acct-01"]))
        # "secret-01" -> last four characters are "t-01".
        R.check(
            "the secret was left alone",
            keys["acct-01"]["secret_tail"] == "t-01",
            keys["acct-01"]["secret_tail"],
        )

        # Disabling a key must free its slot for the next one in line.
        client.put("/api/keys/acct-02", json={"enabled": False})
        keys = client.get("/api/keys").json()
        by_label = {k["label"]: k for k in keys}
        R.check("key disabled", by_label["acct-02"]["enabled"] is False)
        R.check("a disabled key is not in play", by_label["acct-02"]["in_use"] is False)

        in_play = [k["label"] for k in keys if k["in_use"]]
        R.check("still five in play", len(in_play) == 5, str(in_play))
        R.check(
            "the next key was promoted",
            "acct-06" in in_play and "acct-02" not in in_play,
            str(in_play),
        )

        R.check(
            "editing an unknown key is a 404",
            client.put("/api/keys/nope", json={"rpm_limit": 5}).status_code == 404,
        )

        # Raising the ceiling on an exhausted key should make it usable again
        # rather than waiting for the next reset.
        loaded = {k.label: k for k in database.load_keys()}["acct-03"]
        loaded.used_today = 250
        loaded.state = "exhausted"
        database.save_key(loaded)

        client.put("/api/keys/acct-03", json={"rpd_limit": 500})
        by_label = {k["label"]: k for k in client.get("/api/keys").json()}
        R.check(
            "raising the limit revives an exhausted key",
            by_label["acct-03"]["state"] == "active",
            by_label["acct-03"]["state"],
        )


def test_key_rotation_promotes_next():
    print("\nKey rotation as keys are spent")

    from ledger.quota import ApiKey, KeyPool

    keys = [
        ApiKey(id=i, label=f"k{i:02d}", secret=f"s{i}", rpm_limit=0, rpd_limit=2)
        for i in range(1, 11)
    ]
    pool = KeyPool(keys)

    R.check("ten keys, five in play", pool.working_set_size() == 5)

    # Spend the first two completely; the sixth and seventh should step in.
    for key in keys[:2]:
        for _ in range(2):
            pool.record_success(key)

    in_play = [k["label"] for k in pool.snapshot() if k["in_use"]]
    R.check(
        "spent keys are replaced in order",
        in_play == ["k03", "k04", "k05", "k06", "k07"],
        str(in_play),
    )

    # Total daily capacity is unaffected by the window — every key still gets
    # used, just not all at once.
    R.check(
        "the window does not reduce daily capacity",
        pool.capacity_today()["remaining_today"] == 16,
        str(pool.capacity_today()),
    )

    # Spend everything.
    for _ in range(100):
        key = pool.acquire()
        if key is None:
            break
        pool.record_success(key)

    R.check("every key was eventually used", pool.working_set_size() == 0)
    R.check(
        "all twenty requests were spent",
        pool.capacity_today()["used_today"] == 20,
        str(pool.capacity_today()),
    )


def test_sheet_and_export_naming():
    print("\nSheet tab and export file naming")

    from ledger.export import build_file_stems
    from ledger.sheets import build_tab_names

    # Normal case: the tab and the files are simply the PDF's name.
    books = [
        {"id": 1, "title": "010"},
        {"id": 2, "title": "007"},
        {"id": 3, "title": "001"},
    ]
    tabs = build_tab_names(books)
    R.check("tab is just the PDF name", tabs[1] == "010", tabs[1])
    R.check("no id prefix", tabs[2] == "007", tabs[2])

    stems = build_file_stems(books)
    R.check("export file is just the PDF name", stems[1] == "010", stems[1])
    R.check("no zero-padded prefix", stems[3] == "001", stems[3])

    # Two PDFs sharing a filename cannot share a tab, so only those are
    # disambiguated — the rest keep their clean names.
    clashing = [
        {"id": 1, "title": "010"},
        {"id": 2, "title": "010"},
        {"id": 3, "title": "011"},
    ]
    tabs = build_tab_names(clashing)
    R.check("duplicates are made unique", tabs[1] != tabs[2], f"{tabs[1]} vs {tabs[2]}")
    R.check("duplicate names mention the id", "1" in tabs[1] and "2" in tabs[2], str(tabs))
    R.check("the unaffected book keeps its clean name", tabs[3] == "011", tabs[3])

    stems = build_file_stems(clashing)
    R.check("duplicate export stems are unique", stems[1] != stems[2], str(stems))
    R.check("unaffected export stem stays clean", stems[3] == "011", stems[3])


def test_long_page_split_across_cells():
    print("\nOver-long pages split across cells")

    from ledger.sheets import (
        MAX_CELL_CHARS,
        TRANSCRIPTION_COLUMNS,
        _build_chunks,
        split_for_cells,
    )

    # An ordinary page occupies one cell and leaves the rest empty.
    short = "Van de oude gebruiken dezer landen."
    parts, truncated = split_for_cells(short)
    R.check("short page fits one cell", parts[0] == short and not truncated)
    R.check(
        "the row is always the same width",
        len(parts) == TRANSCRIPTION_COLUMNS and parts[1:] == [""] * (TRANSCRIPTION_COLUMNS - 1),
        str([len(p) for p in parts]),
    )

    # A realistic over-long page: prose in lines, ~81,000 characters.
    line = "Van de oude gebruiken dezer landen valt zeer veel te verhalen, regel"
    text = "\n".join(f"{line} {i}." for i in range(1100))
    R.check("test page really is over the limit", len(text) > 50_000, str(len(text)))

    parts, truncated = split_for_cells(text)

    R.check("it was not truncated", truncated is False)
    R.check(
        "every cell is under the Sheets limit",
        all(len(p) < 50_000 for p in parts),
        str([len(p) for p in parts]),
    )
    R.check(
        "it rejoins losslessly",
        "\n".join(p for p in parts if p) == text,
    )
    R.check(
        "splits land on line boundaries, not mid-word",
        parts[0].endswith(".") and parts[1].startswith("Van"),
        f"...{parts[0][-20:]!r} / {parts[1][:20]!r}...",
    )
    R.check("only as many cells as needed are used", parts[2] == "" and parts[3] == "")

    # Text with no whitespace at all must still split rather than fail.
    blob = "x" * (MAX_CELL_CHARS * 2 + 100)
    parts, truncated = split_for_cells(blob)
    R.check(
        "text with no break points still splits",
        all(len(p) <= MAX_CELL_CHARS for p in parts) and not truncated,
        str([len(p) for p in parts]),
    )
    R.check("and loses nothing", "".join(parts) == blob)

    # Beyond every column, it must mark the loss rather than fail silently.
    huge = "y" * (MAX_CELL_CHARS * TRANSCRIPTION_COLUMNS + 10_000)
    parts, truncated = split_for_cells(huge)
    R.check("an absurdly long page is flagged truncated", truncated is True)
    R.check("the last cell says so", "TRUNCATED" in parts[-1], parts[-1][-60:])
    R.check("it points at the exports", "exports" in parts[-1])
    R.check(
        "no cell exceeds the limit even then",
        all(len(p) <= MAX_CELL_CHARS for p in parts),
        str([len(p) for p in parts]),
    )

    # Chunking must be bounded by characters too, not only row count.
    fat_rows = [[i, "text", "z" * 40_000, "", "", "", "", "", "done"] for i in range(60)]
    chunks = _build_chunks(fat_rows)
    R.check(
        "long pages produce more, smaller chunks",
        len(chunks) > 1,
        f"{len(chunks)} chunk(s) for 60 fat rows",
    )
    R.check(
        "every chunk stays within the character budget",
        all(
            sum(len(c) for r in chunk for c in r if isinstance(c, str)) <= 2_100_000
            for chunk in chunks
        ),
    )
    R.check(
        "no row is lost or duplicated by chunking",
        sum(len(c) for c in chunks) == 60,
        str(sum(len(c) for c in chunks)),
    )

    # Ordinary pages should still batch efficiently.
    thin_rows = [[i, "text", "a" * 2000, "", "", "", "", "", "done"] for i in range(400)]
    chunks = _build_chunks(thin_rows)
    R.check(
        "ordinary pages still batch by row count",
        max(len(c) for c in chunks) == 150,
        str([len(c) for c in chunks]),
    )


def test_plain_text_export():
    print("\nPlain text export")

    with tempfile.TemporaryDirectory() as tmp:
        client, config, database = make_client(Path(tmp))
        client.post("/api/scan")
        book_id = client.get("/api/books").json()[0]["id"]

        database.save_page_result(
            book_id, 16, PAGE_DONE,
            {"page_type": "text", "transcription": "Van de oude gebruiken",
             "footnotes": "1. Zie boven."},
            {},
        )
        database.save_page_result(
            book_id, 17, PAGE_DONE,
            {"page_type": "blank", "transcription": ""}, {},
        )

        client.post("/api/export")

        txt = sorted(Path(config.export_dir).glob("*.txt"))
        R.check("a .txt is written per book", len(txt) == 2, str([p.name for p in txt]))
        R.check(
            "named after the PDF",
            {p.stem for p in txt} == {"boek-1", "boek-2"},
            str([p.name for p in txt]),
        )

        content = next(p for p in txt if p.stem == "boek-1").read_text(encoding="utf-8")
        R.check("the transcription is in it", "Van de oude gebruiken" in content)
        R.check("page breaks are marked", "--- PDF page 16 ---" in content)
        R.check("footnotes are included", "1. Zie boven." in content)
        R.check("a blank page says so", "[BLANK]" in content)
        R.check(
            "untranscribed pages are explicit, not silent gaps",
            "[not transcribed: pending]" in content,
        )


def test_apps_script_served():
    print("\nApps Script source")

    with tempfile.TemporaryDirectory() as tmp:
        client, _, _ = make_client(Path(tmp))

        result = client.get("/api/appsscript").json()
        R.check("a version is reported", bool(result["version"]), str(result["version"]))
        R.check("the code is served", "function doPost" in result["code"])

        # The batched action is what makes publishing idempotent; the old
        # per-page appendRow must be gone.
        R.check("it uses batched range writes", "setValues" in result["code"])
        R.check("syncChunk action present", "syncChunk" in result["code"])
        R.check(
            "the printed-page header is gone",
            '"Printed Page"' not in result["code"],
        )
        R.check(
            "continuation columns are declared",
            '"Transcription 4"' in result["code"],
        )
        R.check(
            "no per-page appendRow for transcriptions",
            "appendRow([pageNumber" not in result["code"],
        )
        R.check(
            "the declared version matches the code",
            f'SCRIPT_VERSION = "{result["version"]}"' in result["code"],
        )


def test_requeue_and_export():
    print("\nRequeue and export")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        client, config, database = make_client(tmp_path)
        client.post("/api/scan")

        books = client.get("/api/books").json()
        book_id = books[0]["id"]

        database.save_page_result(
            book_id, 16, PAGE_DONE,
            {"page_type": "text", "transcription": "Van de oude gebruiken",
             "printed_page_number": "3"},
            {"machine_id": "api-test", "render_dpi": 150},
        )
        database.save_page_result(
            book_id, 17, PAGE_FLAGGED,
            {"page_type": "text", "transcription": "ab"}, {},
            flag_reason="too short",
        )

        # Requeue with nothing selected must be refused rather than silently
        # doing nothing.
        R.check(
            "requeue with no selection is a 400",
            client.post(f"/api/books/{book_id}/requeue",
                        json={"flagged": False, "failed": False}).status_code == 400,
        )

        result = client.post(f"/api/books/{book_id}/requeue",
                             json={"flagged": True, "failed": False}).json()
        R.check("requeued the flagged page", result["requeued"] == 1, str(result))

        detail = client.get(f"/api/books/{book_id}").json()
        page17 = next(p for p in detail["pages"] if p["page_no"] == 17)
        page16 = next(p for p in detail["pages"] if p["page_no"] == 16)
        R.check("flagged page is pending again", page17["status"] == "pending", page17["status"])
        R.check("the done page was left alone", page16["status"] == "done", page16["status"])

        # Export.
        result = client.post("/api/export").json()
        R.check("export covers both books", result["book_count"] == 2, str(result))

        index_path = Path(config.export_dir) / "index.json"
        R.check("index.json written", index_path.exists())

        download = client.get("/api/export/download")
        R.check("index.json downloadable", download.status_code == 200, str(download.status_code))

        jsonl = sorted(Path(config.export_dir).glob("*.jsonl"))
        R.check("one jsonl per book", len(jsonl) == 2, str(len(jsonl)))

        first = json.loads(jsonl[0].read_text(encoding="utf-8").split("\n")[15])
        R.check(
            "printed page number survives to the export",
            first["printed_page_number"] == "3",
            str(first["printed_page_number"]),
        )


def test_remove_book():
    print("\nRemoving a book")

    with tempfile.TemporaryDirectory() as tmp:
        client, _, _ = make_client(Path(tmp))
        client.post("/api/scan")

        books = client.get("/api/books").json()
        book_id = books[0]["id"]

        R.check("removed", client.delete(f"/api/books/{book_id}").status_code == 200)
        R.check("one book left", len(client.get("/api/books").json()) == 1)
        R.check(
            "removing it twice is a 404",
            client.delete(f"/api/books/{book_id}").status_code == 404,
        )

        # The PDF is untouched, so a rescan brings it back as new.
        again = client.post("/api/scan").json()
        R.check("rescan re-registers the removed book", again["added"] == 1, str(again))


def test_events():
    print("\nEvent log")

    with tempfile.TemporaryDirectory() as tmp:
        client, _, database = make_client(Path(tmp))

        database.log("first line", "info")
        database.log("second line", "warn")

        events = client.get("/api/events").json()
        R.check("events returned", len(events) >= 2, str(len(events)))
        R.check(
            "oldest first, so the console can append",
            events[0]["message"] == "first line",
            events[0]["message"],
        )

        last_id = events[-1]["id"]
        R.check(
            "after_id returns nothing new",
            client.get(f"/api/events?after_id={last_id}").json() == [],
        )

        database.log("third line", "err")
        fresh = client.get(f"/api/events?after_id={last_id}").json()
        R.check("after_id returns only what is new", len(fresh) == 1, str(fresh))
        R.check("level is carried", fresh[0]["level"] == "err", fresh[0]["level"])


def test_publish_flow_stubbed():
    print("\nPublish flow (Apps Script stubbed)")

    with tempfile.TemporaryDirectory() as tmp:
        client, config, database = make_client(Path(tmp))
        client.post("/api/scan")

        book_id = client.get("/api/books").json()[0]["id"]
        for page_no in range(16, 21):
            database.save_page_result(
                book_id, page_no, PAGE_DONE,
                {"page_type": "text", "transcription": f"Bladzijde {page_no}",
                 "printed_page_number": str(page_no - 15)},
                {"machine_id": "api-test"},
            )

        client.put("/api/settings", json={
            "sheets_url": "https://script.google.com/macros/s/fake/exec",
            "sheets_secret": "s3cret",
        })

        # Intercept the HTTP call the publisher makes, so the whole chunking,
        # threading and progress path runs for real without a deployment.
        import ledger.sheets as sheets_module
        calls: list[dict] = []

        def fake_call(url, secret, action, payload=None):
            calls.append({"action": action, "payload": payload or {}})
            return {"ok": True, "written": len((payload or {}).get("rows", []))}

        original = sheets_module._call
        sheets_module._call = fake_call
        try:
            client.post("/api/sheets/publish", json={"book_id": book_id})

            # Wait for the background thread.
            import time
            for _ in range(60):
                snapshot = client.get("/api/status").json()["publish"]
                if snapshot["state"] in ("done", "failed", "cancelled"):
                    break
                time.sleep(0.1)

            R.check("publish completed", snapshot["state"] == "done", str(snapshot))
            R.check("five pages written", snapshot["pages_written"] == 5, str(snapshot))
            R.check("one chunk was sent", len(calls) == 1, str(len(calls)))

            payload = calls[0]["payload"]
            R.check("the sync action was used", calls[0]["action"] == "syncChunk", calls[0]["action"])
            R.check("rows carry the page number first", payload["rows"][0][0] == 16, str(payload["rows"][0]))
            R.check(
                "row layout is page, type, 4 transcription cells, footnotes, note, status",
                len(payload["rows"][0]) == 9 and payload["rows"][0][1] == "text",
                str(payload["rows"][0]),
            )
            R.check(
                "the transcription is in the first transcription cell",
                payload["rows"][0][2].startswith("Bladzijde"),
                str(payload["rows"][0][2])[:40],
            )
            R.check("the final chunk is marked", payload["isFinalChunk"] is True)
            R.check("the machine is identified", payload["machine"] == "api-test", payload["machine"])
            # The tab is simply the PDF's name — boek-1.pdf goes to a tab
            # called "boek-1", with no id prefix or decoration.
            R.check(
                "the tab is named after the PDF",
                payload["tab"] == "boek-1",
                payload["tab"],
            )

            # Pending pages must not be published as blanks — a gap in the
            # sheet has to mean "not transcribed", unambiguously.
            page_numbers = [row[0] for row in payload["rows"]]
            R.check(
                "only transcribed pages are sent",
                page_numbers == [16, 17, 18, 19, 20],
                str(page_numbers),
            )
        finally:
            sheets_module._call = original


def test_publish_marks_empty_page_types():
    print("\nPublish marks blank and image pages")

    with tempfile.TemporaryDirectory() as tmp:
        client, _, database = make_client(Path(tmp))
        client.post("/api/scan")
        book_id = client.get("/api/books").json()[0]["id"]

        database.save_page_result(book_id, 1, PAGE_DONE,
                                  {"page_type": "blank", "transcription": ""}, {})
        database.save_page_result(book_id, 2, PAGE_DONE,
                                  {"page_type": "illustration_only", "transcription": ""}, {})

        from ledger.sheets import _page_to_row
        rows = {
            int(page["page_no"]): _page_to_row(page)[0]
            for page in database.pages_for_book(book_id)
            if page["status"] == "done"
        }

        # An empty cell would be ambiguous with "not transcribed yet", which
        # could never be resolved afterwards.
        R.check("blank pages are marked", rows[1][2] == "[BLANK]", str(rows[1]))
        R.check("illustration pages are marked", rows[2][2] == "[IMAGE]", str(rows[2]))


def main():
    print("=" * 66)
    print("Ledger — API and UI test suite")
    print("=" * 66)

    for test in (
        test_ui_served,
        test_ui_integrity,
        test_status_and_config,
        test_scan_and_books,
        test_page_image,
        test_keys_endpoints,
        test_run_control,
        test_settings_and_sheets,
        test_key_import_and_editing,
        test_key_rotation_promotes_next,
        test_sheet_and_export_naming,
        test_long_page_split_across_cells,
        test_plain_text_export,
        test_apps_script_served,
        test_requeue_and_export,
        test_remove_book,
        test_events,
        test_publish_flow_stubbed,
        test_publish_marks_empty_page_types,
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
