# Ledger — Old Book Transcriber

Batch transcription of scanned historical PDFs via the Gemini API, built for a
long-running free-tier job spread across several machines and many API keys.

**Version 0.4.1 — complete.** Engine, web UI, and Google Sheets publishing.

---

## Why this exists as Python

The original single-file HTML app worked, but browsers revoke folder access, so
it could not be deployed unattended on a remote machine. Here the PDF folder is
a server-side path, which is not revocable. Everything else about the rewrite
follows from that one change.

---

## Requirements

- **Python 3.10 or newer.**
- **No database server.** SQLite ships with Python and needs no installation,
  no service, and no configuration. The database file and all its tables are
  created automatically the first time you run any command — there is no
  migration or init step.
- **No other external services.** Just network access to
  `generativelanguage.googleapis.com`.

---

## Setup

### 1. Install

Use a virtual environment. On Ubuntu 24 and other recent distributions a bare
`pip install` into the system Python is refused outright (PEP 668), so this is
not optional advice:

```bash
cd ledger
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -e .
```

That gives you a `ledger` command that works from any directory:

```bash
ledger --help
```

Remember to activate the venv in each new shell before running `ledger`.

<details>
<summary>Running without installing</summary>

You can skip `pip install -e .` and use `python -m ledger.cli <command>`
instead, but only from the project root — Python locates the `ledger` package
via the current directory, so from anywhere else you get
`ModuleNotFoundError: No module named 'ledger'`. You will also need to install
the dependencies yourself with `pip install -r requirements.txt`.

</details>

### 2. Configure

```bash
cp env.example.txt .env
cp keys.example.json keys.json
```

In `.env` only three settings usually matter per machine:

```
LEDGER_PDF_ROOT=/path/to/this/machines/scans
LEDGER_MACHINE_ID=machine-1
LEDGER_DB_PATH=./ledger.db
```

`LEDGER_MACHINE_ID` is stamped onto every page, so give each machine a
different value — otherwise merged exports cannot be traced back. Everything
else in the file documents defaults you do not need to change.

In `keys.json`, one entry per key:

```json
[
  {"label": "acct-01", "secret": "AIza...", "rpm_limit": 10, "rpd_limit": 250}
]
```

Set `rpd_limit` from what AI Studio actually shows for that account. Google no
longer publishes free-tier daily figures and says they are not guaranteed. If
you set it too high the pool learns the real ceiling from repeated 429s, but
starting close is better.

**Keys must not be shared between machines.** Two machines using one key both
count against a single project's quota, and neither will understand why it is
getting 429s. Give each machine its own disjoint set.

### 3. Import keys and register the books

```bash
ledger keys import keys.json
ledger scan
```

`scan` walks the PDF root and queues every page. It is safe to re-run at any
time — already-registered books keep all their progress, and only genuinely new
files and pages are added.

### 4. Verify before committing to a long run

```bash
ledger check
```

Reports the resolved settings, confirms the PDF root and database are usable,
counts what is registered, and checks your keys — including catching two keys
that share the same secret, which would otherwise make the pool believe it has
twice the capacity it really has.

Then send exactly one real request:

```bash
ledger check --live
```

This renders a genuine text page from your corpus, transcribes it, and prints
the first 300 characters back. Spends one request. Do this before starting a
job that runs for weeks — read the output and judge whether the transcription
quality is what you want.

### 5. Run

Either from the browser:

```bash
ledger serve                       # http://127.0.0.1:8000
ledger serve --host 0.0.0.0        # reachable from the LAN — see the warning below
```

The UI is a view over the engine, not a separate implementation. Start the queue
in the browser and close the tab: the run keeps going, because the work happens
on the server. Reopen it and you see exactly where things stand.

**There is no authentication.** On `127.0.0.1` that is fine. Binding to
`0.0.0.0` means anyone who can reach the port can read your transcriptions and
add API keys, so only do it on a network you trust.

Or headless, which is the same engine:

```bash
ledger run
```

Ctrl-C stops cleanly; workers finish the page they are on and commit it. Killing
it outright is also survivable — unfinished pages return to the queue on the
next start.

```bash
ledger status          # progress and remaining quota
ledger export          # write the JSON files
```

### Running unattended

`ledger run` exits once the queue is empty or all keys are spent for the day,
so for a multi-week job you want it restarted periodically. The simplest
approach on Linux:

```bash
# Re-run after each Pacific reset. Adjust for your local time.
0 14 * * *  cd /path/to/ledger && .venv/bin/ledger run >> run.log 2>&1
```

For a single long session instead, `nohup .venv/bin/ledger run > run.log 2>&1 &`
survives logout. On Windows, use Task Scheduler pointing at
`.venv\Scripts\ledger.exe run`.

Either way the database is the durable record, so an interrupted or repeated
run never loses or repeats work.

---

## How it works

```
scan  ──▶  SQLite work queue  ──▶  workers  ──▶  SQLite results  ──▶  export
                                      │
                              one thread per key
```

Three stages with a durable store between them, which is what makes the run
restartable. Kill the process at page 40,000 and the next start resumes at
40,001.

| Module | Responsibility |
|---|---|
| `config.py` | Every tunable setting, read from the environment |
| `scanner.py` | Walks the PDF folder, fills the work queue |
| `render.py` | Rasterises one PDF page to PNG bytes |
| `prompts.py` | Transcription and book-profiling instructions |
| `gemini.py` | API client, and the error classification everything relies on |
| `quota.py` | Key pool, Pacific-time daily accounting, RPM pacing |
| `db.py` | SQLite — the durable record of every page and key |
| `worker.py` | The engine, plus the local quality heuristics |
| `export.py` | Turns the database into the JSON files you keep |
| `cli.py` | Command line entry point |

### Design decisions worth knowing

**SQLite is the working store; JSON is the artifact.** The old script wrote its
JSON only after a whole book finished, so a crash at page 400 of 450 lost
everything. Here every page is committed the moment it arrives, and `export`
generates the JSON on demand.

**Quota resets at midnight US Pacific, not local midnight.** Running from India,
local-time accounting zeroes the counters about eleven and a half hours early,
after which the app believes it has a full allowance and spends half a day
collecting 429s while blaming the wrong keys.

**A 429 is not one thing.** Daily exhaustion means "come back tomorrow";
per-minute throttling means "wait thirty seconds". `gemini.py` classifies every
failure into one of four kinds and `quota.py` responds to each differently.
Neither ever counts against a page's attempt limit, because a quota problem says
nothing about whether the page is transcribable.

**No fixed delay between requests.** The old app slept 4.5 seconds per page. On
the free tier the binding constraint is requests-per-*day*, so spacing requests
out does not buy more pages — it just spreads the same allowance over more
hours. Pacing here is per-key from each key's real RPM limit; workers otherwise
sprint until the daily wall and then park. Twenty keys will burn a day's
allowance in well under an hour, and idling afterwards is the correct outcome.

**Pages are claimed individually, not whole books.** The old app gave one book
to one key, leaving keys idle whenever books in flight were fewer than keys.

**Nothing retries automatically.** Flagged and failed pages wait for an explicit
`requeue`. On a metered tier an automatic retry loop can quietly eat a day.

**Quality checking costs nothing.** `assess_quality` in `worker.py` is a plain
function over the text that already came back — no second request, no network.
It catches pages classified as "text" that arrived nearly empty, responses stuck
in a repeat loop, output that is mostly illegible markers, and control
characters. All it does is write `flagged` instead of `done` so those pages are
easy to find. It is always one request per page.

### Image quality

The usual "300–400 DPI, lossless format" advice comes from classic OCR engines,
which work on the raw pixel grid. Gemini instead tokenises an image into a fixed
budget — 1120 tokens at the default, 2240 at `ultra_high` — so beyond the
resolution that fills that budget, extra pixels are downsampled away.

So the defaults are 300 DPI, PNG, original colour, and
`media_resolution=ultra_high`.
PNG matters not for resolution but because JPEG artifacts cluster on
high-contrast letter edges, which is exactly where a model reading blackletter
or the long s needs clean pixels. `ultra_high` is free on the free tier, since
one request is one request regardless of tokens.

Colour is kept for the same reason `ultra_high` is: the token budget per image
is fixed whether the page is colour or greyscale, so discarding colour saves
nothing that matters while throwing away real signal — red rubrication,
marginalia added in a different ink, and the difference between a stain and
actual ink. Set `LEDGER_GREYSCALE=true` if you want the smaller, faster
renders anyway.

### Language and era detection

Each book is profiled once — language, script, era, typeface, orthographic
conventions — and that profile is injected into every page prompt for that book.
One extra request per book, cached permanently.

This replaces the old hardcoded "19th-century book in Dutch, French, German or
English", which is a confident and specific claim that is simply wrong for
Middle Dutch or 19th-century Gujarati material. Where the model cannot tell, it
returns "unknown" and the prompt declines to assert a century rather than
guessing — a wrong century propagated across 400 page prompts is worse than an
admitted gap.

**Which pages it samples.** Not the first few: the opening pages of a scan are
cover, endpapers, half-title and frontispiece, and profiling from those
describes the front matter rather than the book. Sampling starts after page 15
(`LEDGER_PROFILE_START_AFTER_PAGE`) and only uses pages that actually carry
body text.

Whether a page carries text is worked out **locally**, with no API request, from
a low-resolution render measuring two things: how much of the page is ink, and
what fraction of pixel rows are empty. Dense body text runs about 10% ink with
half its rows empty (interline gaps and margins). A blank page is near zero ink.
A full-page plate has high ink and almost no empty rows. A half-title has almost
nothing but empty rows. Each of those is rejected.

The black/white cutoff is chosen per page by Otsu's method rather than being
fixed, because this corpus is yellowed and unevenly lit — a fixed threshold
would read one tinted scan as solid ink and another as blank. In testing, a
yellowed page measures within 0.001 of the same page on white paper.

Selection degrades rather than failing:

1. text pages after page 15 — the intended case
2. text pages anywhere in the book — short books, plate-heavy volumes
3. nothing — a volume of only plates and blanks is not profiled at all, and
   spends no request discovering that

The strategy used and the pages sampled are recorded on the profile, because a
profile built from fallback pages deserves less trust than one built as
intended, and that is impossible to tell afterwards unless it is written down.

---

## What the export contains

One `.jsonl` per book (one page per line) plus a `.manifest.json`, and one
`index.json` across the export.

Each page record carries the printed page number as it appears on the page
(often different from the PDF index, and what a citation needs), body text and
footnotes separately, detected languages, uncertainty flags, and a full
provenance block: model, prompt version, app version, media resolution, render
DPI, image format, machine, key label, token counts and latency.

The provenance is the part that matters in three years, when someone asks which
model produced a passage and at what settings. It costs nothing now and cannot
be reconstructed afterwards.

---

## Tests

```bash
python tests/test_engine.py        # 153 checks — no network needed
python tests/test_end_to_end.py    # full pipeline, Gemini stubbed

pip install -e ".[dev]"            # test_api.py needs these
python tests/test_api.py           # 90 checks — every endpoint, plus the UI file
```

Or under pytest, which needs the dev extra installed:

```bash
pip install -e ".[dev]"
python -m pytest tests/ -q
```

The end-to-end test drives the real worker loop, database, key pool, renderer
and exporter, substituting only the network call. Its stub deliberately
misbehaves the way the real API does — rate limits, a 500, a revoked key, and
one degenerate repetition-loop response — and asserts that every page still
reaches the right terminal state and no page is transcribed twice.

`test_api.py` runs the real FastAPI app against a temporary database and corpus.
It also checks the UI file itself for the mistakes that are invisible until
someone clicks the wrong control: an element the JavaScript addresses but the
markup does not define, and any leftover reference to the browser APIs this
rewrite exists to remove.

**Not covered:** live Gemini calls and a live Apps Script deployment. The Gemini
request shape follows the working HTML app plus the documented `resolution`
field; run `ledger check --live` to confirm it against your own account. The
Sheets publisher's HTTP call is stubbed, so the chunking, threading and progress
paths are all exercised without a deployment.

---

## Before your first long run

1. **Rotate the key** that was pasted in plaintext into the earlier chat.
2. **Run `ledger check --live`** to confirm the request shape is accepted. The
   thinking config is already off by default for exactly this reason — its
   field name could not be verified from here, and a wrong name would fail
   every request.
3. **Read your real RPD off AI Studio** for one account and set `rpd_limit`
   accordingly. Google no longer publishes free-tier daily figures and says the
   numbers are not guaranteed. If you set it too high the pool learns the real
   ceiling from repeated 429s, but starting close is better.
4. **Read the transcription `check --live` prints** before committing to a
   multi-week run, and after a few pages confirm the book profile looks right
   in `ledger export` output.
5. **Keep keys disjoint across machines.** Two machines sharing a key will both
   count against one project's quota and neither will understand why it is
   getting 429s.

---

## The web UI

`ledger serve` serves a single HTML file — the same ledger-paper design as the
original browser app, with everything underneath it rewired:

| Was | Now |
|---|---|
| Browser folder handle | A server-side path in `.env`, which cannot be revoked |
| IndexedDB | SQLite |
| pdf.js rendering in a canvas | PyMuPDF, streamed as PNG on request |
| Browser worker loop | The engine's threads |
| One Sheets POST per page | One batched publish, on a button |

**Books** lists every book with its progress, and a page grid you can click into.
The reading pane shows the scan beside the transcription, with footnotes,
printed page number, and how the page was produced — model, resolution, DPI, key,
tokens, latency. Arrow keys move between pages.

**API Keys** shows each key's live state (active, cooling down, exhausted for the
day, or dead) with the reason. Secrets are never sent to the browser; only the
last four characters, which is enough to match a key against AI Studio. A key
the engine retired can be returned to service by hand — the engine will never do
that itself, but you may have fixed the account, or the rejection may have come
from a network problem rather than Google.

**Sheets & Settings** holds the Sheets connection and the publish, export and
retry actions. Engine settings appear read-only, because changing DPI or
resolution mid-corpus would leave you with pages transcribed under different
settings and no easy way to tell which is which. Edit `.env` and restart.

**Sheet setup** walks through deploying the Apps Script, with the code served by
the running app so the instructions and the script can never drift apart. The
Test button reports the deployed version against the expected one, so a stale
deployment is visible rather than diagnosed later from odd sheet contents.

**Log** is the live activity tail. It only autoscrolls when you are already at
the bottom, so scrolling back to read something is not yanked away.

## Publishing to Google Sheets

Transcribe locally, publish when you choose. Each book gets its own tab; an
`Index` tab lists them all.

Pages are written in chunks to the row matching their page number, using
`setValues` on a range. That makes publishing **repeatable**: run it twice and
the sheet is identical. The original per-page `appendRow` was not idempotent, so
any retry silently duplicated rows — and at one HTTP round-trip per page it also
burned Apps Script's own execution quota.

Only pages with a result are sent, so a part-finished book publishes cleanly and
can be republished later. Blank and illustration-only pages are written as
`[BLANK]` and `[IMAGE]` rather than left empty, because an empty cell would be
indistinguishable from "not transcribed yet" — an ambiguity impossible to
resolve afterwards.

Publishing runs on a background thread with progress in the UI, since a large
corpus takes minutes.

## Still to build

- **Book-relative length outlier check** — flagging pages whose transcription
  length is a wild outlier against that book's own median. Needs book-level
  statistics that v0.4 deliberately leaves out; costs no API requests.
- **Merge tool** — combining exports from several machines on `(book, page_no)`.
  Straightforward, but worth writing once you know what the merged shape should
  be.
- **Authentication on the web UI** — only needed if you bind to anything other
  than localhost.
