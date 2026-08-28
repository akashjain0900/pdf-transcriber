"""
Ledger — page rendering.

Turns one page of a scanned PDF into image bytes ready to send to Gemini.

The settings here were chosen against how Gemini actually consumes images,
which differs from classic OCR advice in a way worth being explicit about:

  Traditional OCR engines (Tesseract, ABBYY) work directly on the pixel grid,
  so character height in pixels maps almost linearly onto accuracy and 300-400
  DPI is genuinely necessary. Gemini instead tokenises the image into a FIXED
  budget -- 1120 tokens at the default, 2240 at ultra_high. Beyond the
  resolution that fills that budget, extra pixels are simply downsampled away.

So: 300 DPI is a sensible floor, going past ~400 buys nothing, and the real
lever is the media_resolution setting in gemini.py, not the DPI here.

What DOES matter at this layer:

  - PNG over JPEG. Not a resolution question at all. JPEG ringing artifacts
    land on high-contrast letter edges, which is exactly where a model reading
    blackletter or the long s needs clean pixels.
  - Original colour, by default. The model's token budget per image is fixed
    whether the page is colour or greyscale, so discarding colour saves nothing
    that matters and throws away real signal: red rubrication, marginalia in a
    different ink, and stains versus ink are all clearer in colour.
  - Deskew. Crooked pages mainly damage READING ORDER -- the model can
    mis-sequence lines -- rather than character recognition.
"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pymupdf  # formerly imported as `fitz`, which is deprecated in 1.28+


# PDF coordinates are defined at 72 DPI, so this is the scale divisor.
PDF_NATIVE_DPI = 72.0

# How much of the file to read for the cheap fingerprint.
FINGERPRINT_CHUNK = 1024 * 1024  # 1 MB


class RenderError(Exception):
    """Raised when a page cannot be rasterised at all."""


# ---------------------------------------------------------------------
# File identity
# ---------------------------------------------------------------------

def fingerprint_file(path: Path, full: bool = False) -> str:
    """
    Produce a stable identifier for a PDF file.

    Full SHA-256 is the rigorous option but means reading every byte. Across a
    corpus of hundreds of gigabytes that is a long one-off wait for a question
    we only ask occasionally ("is this the same file I registered last week?").

    The default cheap fingerprint hashes the file size plus its first and last
    1 MB. That catches replacement, truncation and re-scanning while being
    effectively instant. Set full=True when you want cryptographic rigour.
    """
    path = Path(path)
    digest = hashlib.sha256()

    if full:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return "sha256:" + digest.hexdigest()

    size = path.stat().st_size
    digest.update(str(size).encode("utf-8"))

    with path.open("rb") as handle:
        digest.update(handle.read(FINGERPRINT_CHUNK))

        # Only seek for the tail if the file is big enough for it to be
        # different from the head we just read.
        if size > FINGERPRINT_CHUNK * 2:
            handle.seek(-FINGERPRINT_CHUNK, io.SEEK_END)
            digest.update(handle.read(FINGERPRINT_CHUNK))

    return "quick:" + digest.hexdigest()


def page_count(path: Path) -> int:
    """Number of pages in a PDF, without rendering anything."""
    try:
        with pymupdf.open(path) as doc:
            return doc.page_count
    except Exception as exc:
        raise RenderError(f"Could not read {path.name}: {exc}") from exc


# ---------------------------------------------------------------------
# Deskew
# ---------------------------------------------------------------------

def estimate_skew_angle(
    image,
    max_angle: float = 3.0,
    step: float = 0.25,
) -> float:
    """
    Estimate a page's skew in degrees using a horizontal projection profile.

    The idea: when text lines are perfectly horizontal, summing dark pixels
    across each row gives sharp peaks (the lines) and deep troughs (the gaps
    between them), so the VARIANCE of those row sums is at its maximum. Rotate
    the page away from level and the lines smear across rows, flattening the
    profile and lowering the variance. So we try a range of angles and keep the
    one with the highest variance.

    Runs on a downscaled copy -- skew is a global property and does not need
    full resolution to measure, and this keeps the cost to a few milliseconds.

    Returns degrees; positive means the image should be rotated
    counter-clockwise to correct it.
    """
    import numpy as np
    from PIL import Image

    # Downscale to a manageable width. Precision of a quarter degree does not
    # need thousands of pixels.
    working = image.convert("L")
    if working.width > 800:
        ratio = 800.0 / working.width
        working = working.resize(
            (800, max(1, int(working.height * ratio))),
            Image.BILINEAR,
        )

    # Invert so text is high-valued: we are measuring where the ink is.
    array = 255 - np.asarray(working, dtype=np.float32)

    best_angle = 0.0
    best_score = -1.0

    angle = -max_angle
    while angle <= max_angle + 1e-9:
        if abs(angle) < 1e-9:
            rotated = array
        else:
            rotated = np.asarray(
                Image.fromarray(array).rotate(
                    angle, resample=Image.BILINEAR, fillcolor=0
                ),
                dtype=np.float32,
            )

        # Sum ink per row, then measure how "peaky" that profile is.
        row_sums = rotated.sum(axis=1)
        score = float(np.var(row_sums))

        if score > best_score:
            best_score = score
            best_angle = angle

        angle += step

    return best_angle


def _apply_deskew(image):
    """Rotate an image to correct its estimated skew, if worth doing."""
    from PIL import Image

    angle = estimate_skew_angle(image)

    # Below a quarter degree the rotation costs more (in resampling blur) than
    # the straightening gains.
    if abs(angle) < 0.25:
        return image

    # expand=True keeps the corners; white fill matches paper.
    return image.rotate(
        angle,
        resample=Image.BICUBIC,
        expand=True,
        fillcolor=255 if image.mode == "L" else (255, 255, 255),
    )


# ---------------------------------------------------------------------
# Page content analysis (no API cost)
# ---------------------------------------------------------------------
#
# The profiler needs to look at pages that actually carry body text. Front
# matter is a bad sample: covers, blank endpapers, half-titles and frontispiece
# plates tell you little about the typeface and orthography of the book proper,
# and a blank page tells you nothing at all.
#
# We work that out locally, from a cheap low-resolution render, so choosing good
# sample pages costs zero API requests.
#
# The measurements:
#   ink_ratio          -- fraction of the page that is dark. Blank pages are
#                         near zero; full-page plates are high.
#   empty_row_fraction -- fraction of pixel rows with almost no ink. Text has
#                         gaps between its lines and margins above and below,
#                         so this is substantial. A photograph or dense
#                         engraving has very few empty rows, which is what
#                         separates a plate from a page of type even when both
#                         have similar overall ink.

# Resolution used for analysis only. Low on purpose: we are measuring gross
# layout, not reading anything, and this keeps each check to a few
# milliseconds.
ANALYSIS_DPI = 72

# Below this much ink the page is effectively empty.
BLANK_MAX_INK = 0.002

# Above this the page is dominated by something that is not type. Dense body
# text measures around 10%; a captioned plate is well above this even though it
# carries a line of type, and we do not want the profiler sampling one.
HEAVY_MAX_INK = 0.25

# A row with less than this share of dark pixels counts as empty.
EMPTY_ROW_MAX_INK = 0.005

# Text needs at least this fraction of rows empty (interline gaps + margins).
TEXT_MIN_EMPTY_ROWS = 0.10

# ...but not TOO many. A page of dense body text runs about 50% empty rows; a
# half-title or a page with three lines on it runs above 90%. Both are "text",
# but only the dense one is a useful sample of how the book is set, so the
# selector rejects the sparse ones.
TEXT_MAX_EMPTY_ROWS = 0.85


def _otsu_threshold(gray_array) -> int:
    """
    Pick a black/white cutoff automatically using Otsu's method.

    A fixed threshold does not survive this corpus: old paper is yellowed,
    foxed and unevenly lit, so 128 might read an entire page as ink on one scan
    and as blank on another. Otsu instead chooses the cutoff that best
    separates the page's two natural brightness groups — paper and ink —
    whatever their absolute values happen to be on that particular scan.
    """
    import numpy as np

    histogram, _ = np.histogram(gray_array, bins=256, range=(0, 256))
    total_pixels = gray_array.size
    weighted_total = float(np.dot(np.arange(256), histogram))

    best_threshold = 128
    best_variance = -1.0

    background_weight = 0.0
    background_sum = 0.0

    for level in range(256):
        background_weight += histogram[level]
        if background_weight == 0:
            continue

        foreground_weight = total_pixels - background_weight
        if foreground_weight == 0:
            break

        background_sum += level * histogram[level]
        background_mean = background_sum / background_weight
        foreground_mean = (weighted_total - background_sum) / foreground_weight

        # Between-class variance: maximised at the best separation.
        variance = (
            background_weight
            * foreground_weight
            * (background_mean - foreground_mean) ** 2
        )

        if variance > best_variance:
            best_variance = variance
            best_threshold = level

    return best_threshold


def _analyse_pixmap(pixmap) -> dict:
    """Measure ink coverage and row structure for one rendered page."""
    import numpy as np

    # Pixmap samples are raw bytes; reshape to (height, width) greyscale.
    array = np.frombuffer(pixmap.samples, dtype=np.uint8)

    expected = pixmap.height * pixmap.width * pixmap.n
    if array.size != expected:
        # Defensive: an unexpected buffer shape must not crash a scan of a
        # thousand books.
        return {
            "ink_ratio": 0.0,
            "empty_row_fraction": 1.0,
            "looks_like_text": False,
            "reason": "unreadable pixel buffer",
        }

    array = array.reshape(pixmap.height, pixmap.width, pixmap.n)

    # Collapse any extra channels to a single greyscale plane.
    gray = array[:, :, 0] if pixmap.n == 1 else array.mean(axis=2).astype(np.uint8)

    threshold = _otsu_threshold(gray)
    is_ink = gray < threshold

    ink_ratio = float(is_ink.mean())

    # Ink per row, as a fraction of the row's width.
    row_ink = is_ink.mean(axis=1)
    empty_row_fraction = float((row_ink < EMPTY_ROW_MAX_INK).mean())

    # Classify.
    if ink_ratio < BLANK_MAX_INK:
        looks_like_text, reason = False, "blank"
    elif ink_ratio > HEAVY_MAX_INK:
        looks_like_text, reason = False, "mostly non-text (plate or dark scan)"
    elif empty_row_fraction < TEXT_MIN_EMPTY_ROWS:
        looks_like_text, reason = False, "no line structure (likely an image)"
    elif empty_row_fraction > TEXT_MAX_EMPTY_ROWS:
        looks_like_text, reason = False, "too sparse (title page or a few lines)"
    else:
        looks_like_text, reason = True, "text"

    return {
        "ink_ratio": round(ink_ratio, 5),
        "empty_row_fraction": round(empty_row_fraction, 4),
        "looks_like_text": looks_like_text,
        "reason": reason,
    }


def analyse_page(pdf_path: Path, page_no: int, dpi: int = ANALYSIS_DPI) -> dict:
    """
    Analyse a single page's content. Exposed mainly for inspection and tests;
    the selector below does its own batched version in one document open.
    """
    with pymupdf.open(pdf_path) as doc:
        if page_no < 1 or page_no > doc.page_count:
            raise RenderError(f"Page {page_no} out of range")

        scale = dpi / PDF_NATIVE_DPI
        pixmap = doc[page_no - 1].get_pixmap(
            matrix=pymupdf.Matrix(scale, scale),
            colorspace=pymupdf.csGRAY,
        )
        return _analyse_pixmap(pixmap)


def select_text_pages(
    pdf_path: Path,
    count: int = 3,
    start_after_page: int = 15,
    scan_limit: int = 40,
) -> tuple[list[int], str]:
    """
    Choose pages that genuinely carry body text, for the book profiler.

    Starts looking AFTER `start_after_page`, on the reasoning that the opening
    pages of a scanned book are cover, endpapers, half-title and frontispiece
    rather than the text proper, and profiling from those produces a description
    of the front matter instead of the book.

    Inspects at most `scan_limit` pages so a 600-page book does not turn this
    into a long job, and gives up gracefully rather than failing:

        1. text pages after start_after_page       (the intended case)
        2. text pages anywhere in the book         (short books, heavy plates)
        3. nothing                                 (all blank or all plates)

    Returns (page_numbers, strategy_used) so the caller can log which path was
    taken -- worth knowing, because a book profiled from fallback pages is
    less trustworthy than one profiled as intended.
    """
    pdf_path = Path(pdf_path)

    try:
        with pymupdf.open(pdf_path) as doc:
            total_pages = doc.page_count
            scale = ANALYSIS_DPI / PDF_NATIVE_DPI
            matrix = pymupdf.Matrix(scale, scale)

            def scan_range(page_numbers) -> list[int]:
                """Inspect pages in order, collecting the text-like ones."""
                found: list[int] = []
                inspected = 0

                for page_no in page_numbers:
                    if len(found) >= count or inspected >= scan_limit:
                        break
                    inspected += 1

                    try:
                        pixmap = doc[page_no - 1].get_pixmap(
                            matrix=matrix, colorspace=pymupdf.csGRAY
                        )
                    except Exception:
                        # One bad page should not abort the selection.
                        continue

                    if _analyse_pixmap(pixmap)["looks_like_text"]:
                        found.append(page_no)

                return found

            # 1. The intended window: after the front matter.
            first_body_page = start_after_page + 1
            if total_pages >= first_body_page:
                pages = scan_range(range(first_body_page, total_pages + 1))
                if pages:
                    return pages, "after front matter"

            # 2. Fall back to anywhere in the book. Reached by short books and
            #    by volumes that are mostly plates.
            pages = scan_range(range(1, total_pages + 1))
            if pages:
                return pages, "whole book (fallback)"

            # 3. No page in this book looks like type at all.
            return [], "no text pages found"

    except RenderError:
        raise
    except Exception as exc:
        raise RenderError(
            f"Failed to select sample pages from {pdf_path.name}: {exc}"
        ) from exc


# ---------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------

def render_page(
    pdf_path: Path,
    page_no: int,
    dpi: int = 300,
    greyscale: bool = False,
    image_format: str = "png",
    deskew: bool = False,
) -> bytes:
    """
    Rasterise one page (1-based) and return the encoded image bytes.

    The PDF is opened and closed on every call rather than being kept open in
    a cache. That looks wasteful, but it is deliberate: MuPDF loads pages
    lazily so reopening is cheap, memory stays flat across a corpus of any
    size, and -- most importantly -- a Document object is not thread-safe, so
    reopening per call keeps twenty concurrent workers trivially correct
    instead of requiring a lock around a shared cache.
    """
    pdf_path = Path(pdf_path)

    if not pdf_path.exists():
        raise RenderError(f"File is missing: {pdf_path}")

    try:
        with pymupdf.open(pdf_path) as doc:
            if page_no < 1 or page_no > doc.page_count:
                raise RenderError(
                    f"Page {page_no} out of range (document has {doc.page_count})"
                )

            page = doc[page_no - 1]

            # Scale from PDF's native 72 DPI up to the requested resolution.
            scale = dpi / PDF_NATIVE_DPI
            matrix = pymupdf.Matrix(scale, scale)

            # When greyscale IS requested, render straight to it rather than
            # rendering RGB and converting afterwards.
            colorspace = pymupdf.csGRAY if greyscale else pymupdf.csRGB

            pixmap = page.get_pixmap(matrix=matrix, colorspace=colorspace)

            # Fast path: no post-processing needed, let MuPDF encode it.
            if not deskew:
                return pixmap.tobytes(image_format)

            # Deskew needs PIL, so hand the raw pixels over.
            from PIL import Image

            image = Image.frombytes(
                "L" if greyscale else "RGB",
                (pixmap.width, pixmap.height),
                pixmap.samples,
            )
            image = _apply_deskew(image)

            buffer = io.BytesIO()
            pil_format = {"png": "PNG", "webp": "WEBP", "jpeg": "JPEG", "jpg": "JPEG"}
            image.save(
                buffer,
                format=pil_format.get(image_format, "PNG"),
                # Lossless for WebP; ignored by PNG. Never lossy here.
                **({"lossless": True} if image_format == "webp" else {}),
            )
            return buffer.getvalue()

    except RenderError:
        raise
    except Exception as exc:
        raise RenderError(
            f"Failed to render page {page_no} of {pdf_path.name}: {exc}"
        ) from exc


def render_pages(
    pdf_path: Path,
    page_numbers: list[int],
    dpi: int = 300,
    greyscale: bool = False,
    image_format: str = "png",
) -> list[bytes]:
    """
    Render several pages in one document open.

    Used only by the book profiler, which looks at the first few pages together
    in a single API call. Deliberately does not deskew: the profiler is reading
    for language and era, not transcribing.
    """
    pdf_path = Path(pdf_path)
    images: list[bytes] = []

    try:
        with pymupdf.open(pdf_path) as doc:
            scale = dpi / PDF_NATIVE_DPI
            matrix = pymupdf.Matrix(scale, scale)
            colorspace = pymupdf.csGRAY if greyscale else pymupdf.csRGB

            for page_no in page_numbers:
                if page_no < 1 or page_no > doc.page_count:
                    continue
                pixmap = doc[page_no - 1].get_pixmap(
                    matrix=matrix, colorspace=colorspace
                )
                images.append(pixmap.tobytes(image_format))

    except Exception as exc:
        raise RenderError(
            f"Failed to render sample pages of {pdf_path.name}: {exc}"
        ) from exc

    return images
