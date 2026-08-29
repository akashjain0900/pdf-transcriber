"""
Ledger — configuration.

Every tunable setting lives here, in one place, so you never have to hunt
through the codebase to change behaviour. Settings are read from environment
variables (optionally via a .env file), which keeps secrets and per-machine
paths out of the source tree.

Nothing in this file is secret. The API keys live in the database (see db.py),
seeded from a file you keep out of version control.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


# Bumped on every behaviour change so a page's `app_version` tells you exactly
# which build produced it. Cheap traceability; do not skip bumping this.
APP_VERSION = "0.5.1"


def _load_dotenv(path: Path) -> None:
    """
    Minimal .env reader.

    We deliberately avoid the python-dotenv dependency: the format we need is
    just KEY=value lines. Existing environment variables always win, so you can
    override the file from the shell without editing it.
    """
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()

        # Skip blanks and comments.
        if not line or line.startswith("#"):
            continue

        if "=" not in line:
            continue

        name, _, value = line.partition("=")
        name = name.strip()
        value = value.strip().strip('"').strip("'")

        # Real environment variables take precedence over the file.
        os.environ.setdefault(name, value)


def _env_str(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Config:
    """Resolved settings for one running instance of Ledger."""

    # ---------------------------------------------------------------
    # Paths
    # ---------------------------------------------------------------

    # The one parent folder containing all the PDFs for THIS machine.
    # Subfolders are walked recursively. This replaces the browser's
    # File System Access API, which is what made the HTML version
    # undeployable — a server-side path is never revoked.
    pdf_root: Path = Path("./pdfs")

    # SQLite database holding books, pages, keys and the event log.
    # This file is the single source of truth for run progress.
    # NOTE: it contains your API keys — treat it as a secret.
    db_path: Path = Path("./ledger.db")

    # Where `export` writes JSONL transcriptions and manifests.
    export_dir: Path = Path("./exports")

    # ---------------------------------------------------------------
    # Machine identity
    # ---------------------------------------------------------------

    # Free-text label for this machine, stamped onto every page it
    # transcribes. When you merge exports from several machines, this
    # tells you which one produced what.
    machine_id: str = "machine-1"

    # ---------------------------------------------------------------
    # Page rendering
    # ---------------------------------------------------------------

    # 300 DPI is the sweet spot. Gemini caps how much visual detail it
    # ingests via a fixed token budget (see media_resolution below), so
    # rendering above ~400 DPI costs render time and upload bandwidth
    # while giving the model nothing extra to look at.
    dpi: int = 300

    # PNG, not JPEG. JPEG compression artifacts cluster on high-contrast
    # letter edges — exactly the pixels that matter for blackletter,
    # the long s, and worn type. PNG is lossless and the file is
    # transient anyway, so the extra size is irrelevant.
    image_format: str = "png"

    # Send pages in their original colour (the default), or convert to
    # greyscale first.
    #
    # Colour is free here, which is the point: the model allocates a fixed
    # token budget per image regardless of colour, so greyscale buys no quota
    # or cost saving — only slightly faster rendering and smaller uploads.
    # Meanwhile colour carries real information on old books: red rubrication,
    # marginalia added in different ink, and the difference between a stain and
    # actual ink are all easier to read in colour than out of it.
    greyscale: bool = False

    # Straighten crooked scans before sending. Skew mainly hurts reading
    # order (the model can mis-sequence lines), not character shapes.
    # Off by default because it costs CPU per page and most scans are
    # already square — turn it on if you see line-ordering problems.
    deskew: bool = False

    # ---------------------------------------------------------------
    # Model
    # ---------------------------------------------------------------

    # Flash-Lite is documented as optimised for document parsing, and on
    # the free tier every model costs the same (one request against your
    # daily allowance), so there is no reason to use anything cheaper.
    model: str = "gemini-3.5-flash-lite"

    api_base: str = "https://generativelanguage.googleapis.com/v1beta/interactions"

    # Visual detail budget. "ultra_high" gives 2240 image tokens versus
    # 1120 at the default — double the detail. It is a per-content-item
    # setting available on Gemini 3 models only.
    #
    # On the free tier this is FREE: one request is one request regardless
    # of how many tokens it carries. There is no reason not to max it.
    media_resolution: str = "ultra_high"

    # "Thinking" is how much internal reasoning the model does before it
    # answers. Valuable for hard reasoning problems, pointless for reading a
    # page of type — and thinking tokens bill as output and add latency.
    #
    # DEFAULT IS OFF (None), which omits the field from the request entirely.
    # That is deliberate: the field name changed between API versions
    # (`thinking_budget` became `thinking_level`) and could not be verified
    # against a live endpoint here. A wrong field name would fail EVERY
    # request; omitting it just means the model uses its own default, which is
    # slightly slower and otherwise harmless.
    #
    # If you later confirm the correct name, set LEDGER_THINKING_LEVEL=low.
    thinking_level: str | None = None

    # ---------------------------------------------------------------
    # Transcription behaviour
    # ---------------------------------------------------------------

    # Keep the printed page's original line breaks. Turn off to get
    # reflowed continuous prose instead.
    preserve_layout: bool = True

    # Detect each book's language, script and era once (from its first few
    # pages) and inject that into every page prompt for that book.
    #
    # Costs one extra request per book — roughly 1,000 requests across the
    # whole corpus, cached permanently on the book row. Worth it: the
    # alternative is a hardcoded prompt that is simply wrong for Middle
    # Dutch or 19th-century Gujarati material.
    profile_books: bool = True

    # How many pages of the book to show the profiler in its single call.
    profile_sample_pages: int = 3

    # Only profile from pages AFTER this one. The opening pages of a scanned
    # book are cover, endpapers, half-title and frontispiece; profiling from
    # those describes the front matter rather than the book. Pages are also
    # checked locally for actual text content before being used, so blanks and
    # full-page plates are skipped -- that check costs no API requests.
    #
    # Books too short to reach this point fall back to sampling anywhere they
    # can find text.
    profile_start_after_page: int = 15

    # Cap on how many pages the local text check will inspect while looking for
    # samples, so a 600-page volume does not turn book registration into a long
    # job.
    profile_scan_limit: int = 40

    # ---------------------------------------------------------------
    # Reliability
    # ---------------------------------------------------------------

    # How many times a single page may be attempted before it is parked
    # as failed. Only genuine errors count — quota exhaustion and rate
    # limiting do not burn an attempt, because they say nothing about
    # whether the page is transcribable.
    max_page_attempts: int = 3

    # HTTP timeout per request, in seconds. Dense pages at ultra_high
    # resolution can be slow; be generous.
    request_timeout: int = 180

    # ---------------------------------------------------------------
    # Concurrency
    # ---------------------------------------------------------------

    # One worker thread per usable API key, capped here. With 20 keys you
    # will normally want 20 — the API call is IO-bound, so threads are the
    # right tool and the GIL is not a factor while waiting on the network.
    max_workers: int = 20

    # Rendering IS CPU-bound, so it gets its own semaphore to stop 20
    # threads from rasterising 300 DPI pages simultaneously and starving
    # the machine. Roughly your core count.
    max_concurrent_renders: int = 4

    # When every key is exhausted for the day, how long to sleep before
    # re-checking. Quota resets at midnight US Pacific, so there is no
    # point polling aggressively.
    idle_sleep_seconds: int = 300

    # ---------------------------------------------------------------
    # Scanning
    # ---------------------------------------------------------------

    # Full SHA-256 of every PDF is the rigorous choice but means reading
    # every byte of the corpus. With hundreds of gigabytes that is a long
    # one-off wait. The default fingerprint hashes file size plus the
    # first and last 1 MB, which is ample for detecting "is this the same
    # file I registered last week" while being effectively instant.
    full_file_hash: bool = False

    # Directory names skipped while walking for PDFs.
    skip_dirs: tuple[str, ...] = field(
        default_factory=lambda: (".git", "__pycache__", ".Trash", "$RECYCLE.BIN")
    )

    @classmethod
    def from_env(cls, dotenv_path: str | Path = ".env") -> "Config":
        """
        Build a Config from environment variables.

        Every field can be overridden with a LEDGER_-prefixed variable,
        e.g. LEDGER_DPI=400 or LEDGER_PDF_ROOT=/mnt/scans.
        """
        _load_dotenv(Path(dotenv_path))

        # Unset means off (see the thinking_level field comment). An explicit
        # empty string also means off, so LEDGER_THINKING_LEVEL= is a valid
        # way to disable it if a value is set elsewhere.
        raw_thinking = os.environ.get("LEDGER_THINKING_LEVEL")
        thinking_level: str | None = (
            raw_thinking.strip() or None if raw_thinking is not None else None
        )

        return cls(
            pdf_root=Path(_env_str("LEDGER_PDF_ROOT", "./pdfs")).expanduser(),
            db_path=Path(_env_str("LEDGER_DB_PATH", "./ledger.db")).expanduser(),
            export_dir=Path(_env_str("LEDGER_EXPORT_DIR", "./exports")).expanduser(),
            machine_id=_env_str("LEDGER_MACHINE_ID", "machine-1"),
            dpi=_env_int("LEDGER_DPI", 300),
            image_format=_env_str("LEDGER_IMAGE_FORMAT", "png").lower(),
            greyscale=_env_bool("LEDGER_GREYSCALE", False),
            deskew=_env_bool("LEDGER_DESKEW", False),
            model=_env_str("LEDGER_MODEL", "gemini-3.5-flash-lite"),
            api_base=_env_str(
                "LEDGER_API_BASE",
                "https://generativelanguage.googleapis.com/v1beta/interactions",
            ),
            media_resolution=_env_str("LEDGER_MEDIA_RESOLUTION", "ultra_high"),
            thinking_level=thinking_level,
            preserve_layout=_env_bool("LEDGER_PRESERVE_LAYOUT", True),
            profile_books=_env_bool("LEDGER_PROFILE_BOOKS", True),
            profile_sample_pages=_env_int("LEDGER_PROFILE_SAMPLE_PAGES", 3),
            profile_start_after_page=_env_int(
                "LEDGER_PROFILE_START_AFTER_PAGE", 15
            ),
            profile_scan_limit=_env_int("LEDGER_PROFILE_SCAN_LIMIT", 40),
            max_page_attempts=_env_int("LEDGER_MAX_PAGE_ATTEMPTS", 3),
            request_timeout=_env_int("LEDGER_REQUEST_TIMEOUT", 180),
            max_workers=_env_int("LEDGER_MAX_WORKERS", 20),
            max_concurrent_renders=_env_int("LEDGER_MAX_CONCURRENT_RENDERS", 4),
            idle_sleep_seconds=_env_int("LEDGER_IDLE_SLEEP_SECONDS", 300),
            full_file_hash=_env_bool("LEDGER_FULL_FILE_HASH", False),
        )

    @property
    def image_mime_type(self) -> str:
        """MIME type matching image_format, for the API request body."""
        return {
            "png": "image/png",
            "webp": "image/webp",
            "jpeg": "image/jpeg",
            "jpg": "image/jpeg",
        }.get(self.image_format, "image/png")
