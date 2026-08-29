"""
Ledger — prompts and structured-output schemas.

Two prompts live here:

  1. The BOOK PROFILE prompt, run once per book against its first few pages, to
     establish language, script, era and orthographic conventions.
  2. The PAGE prompt, run once per page, with that profile injected.

Why the two-stage arrangement matters. The original app hardcoded "a
19th-century book" in "Dutch, French, German, English or another Western
European language". That is a confident, specific claim, and it is simply wrong
for Middle Dutch or 19th-century Gujarati material -- and telling a model the
wrong century and script actively degrades its reading of a page. Detecting the
truth once per book costs one extra request against hundreds of pages and is
cached permanently on the book row.

PROMPT_VERSION is stamped onto every page result. When you change a prompt,
bump it -- otherwise you lose the ability to tell which pages were produced
under which instructions, and a corpus with silently mixed prompt generations
is very hard to reason about later.
"""

from __future__ import annotations


PROMPT_VERSION = "page-v2"
PROFILE_PROMPT_VERSION = "profile-v1"


# Deliberately NOT requested: the page number printed on the paper.
#
# Pages are identified by their position in the PDF (`page_no` in the database),
# which is the primary key, the order the queue works in, and the row a page is
# written to in the sheet. The number printed on the paper is a separate thing
# that often disagrees with it, and it is not wanted here — so the model is not
# asked for it and spends no output tokens on it.
#
# The field itself survives, empty, through the database, export, API and sheet.
# That is intentional: dropping the column would mean a schema migration for a
# field that costs nothing at rest, and it leaves the door open if the printed
# numbers are ever wanted after all.


# ---------------------------------------------------------------------
# Page transcription
# ---------------------------------------------------------------------

# Marker the model is told to use for text it cannot read. Having ONE agreed
# convention means you can later count, find, or filter uncertain passages
# instead of guessing at the model's improvised phrasing.
ILLEGIBLE_MARKER = "[illegible]"


PAGE_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "page_type": {
            "type": "string",
            "enum": [
                "text",
                "blank",
                "illustration_only",
                "partial",
                "unreadable",
            ],
            "description": "Classification of this page.",
        },
        "transcription": {
            "type": "string",
            "description": (
                "Body text of the page, transcribed exactly as printed, "
                "excluding footnotes. Empty string if the page is blank or "
                "illustration_only."
            ),
        },
        "footnotes": {
            "type": "string",
            "description": (
                "Footnote and endnote text appearing on this page, separated "
                "from the body. Empty string if there are none."
            ),
        },
        "languages": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Languages actually present on this page, most prominent "
                "first, as English names."
            ),
        },
        "has_uncertain_text": {
            "type": "boolean",
            "description": (
                "True if any part of the page could not be read confidently "
                "and was marked illegible."
            ),
        },
        "note": {
            "type": "string",
            "description": (
                "Short note only where something needs explaining, e.g. why "
                "the page is unreadable or partial. Empty string otherwise."
            ),
        },
    },
    "required": [
        "page_type",
        "transcription",
        "has_uncertain_text",
    ],
}


def describe_profile(profile: dict | None) -> str:
    """
    Turn a stored book profile into a sentence for the page prompt.

    Falls back to a deliberately vague description when there is no profile,
    rather than inventing a century or language. A model told nothing will read
    what is in front of it; a model told the wrong thing will fight the page.
    """
    if not profile:
        return (
            "This page comes from a scanned historical book. The language, "
            "script and period are not known in advance -- read what is "
            "actually printed rather than assuming any particular tradition. "
        )

    parts: list[str] = []

    era = profile.get("era")
    primary = profile.get("primary_language")
    script = profile.get("script")

    lead = "This page comes from "
    if era:
        lead += f"a {era} book"
    else:
        lead += "a historical book"
    if primary:
        lead += f" printed primarily in {primary}"
    if script and str(script).lower() not in {"latin", "roman"}:
        lead += f", in {script} script"
    parts.append(lead + ". ")

    others = profile.get("other_languages") or []
    if others:
        parts.append(
            "Passages in "
            + ", ".join(str(o) for o in others)
            + " also appear. "
        )

    typeface = profile.get("typeface")
    if typeface:
        parts.append(f"The typeface is {typeface}. ")

    conventions = profile.get("orthographic_notes")
    if conventions:
        parts.append(f"Orthographic conventions to expect: {conventions} ")

    return "".join(parts)


def build_page_prompt(profile: dict | None, preserve_layout: bool = True) -> str:
    """
    Build the per-page transcription instruction.

    Kept as one flat string rather than assembled per-call from fragments, so
    that what the model receives is easy to read here and easy to diff when you
    change it.
    """
    layout_rule = (
        "Preserve the original line breaks and the arrangement of the page as "
        "closely as you can. "
        if preserve_layout
        else "Reflow the text into continuous readable prose, ignoring the "
             "original line breaks. "
    )

    return (
        describe_profile(profile)
        + "Transcribe it exactly as printed. Do not modernise spelling, do not "
        "correct what look like errors, do not expand abbreviations, and do not "
        "translate anything. Reproduce archaic letterforms as their ordinary "
        "equivalents (long s as s, for instance) but keep the original "
        "spelling itself untouched. "
        + layout_rule
        + "Separate footnote text from body text: body goes in `transcription`, "
        "footnotes in `footnotes`. "
        f"Where you genuinely cannot read something, write {ILLEGIBLE_MARKER} "
        "in place of the unreadable span and set `has_uncertain_text` to true. "
        "Never guess at a word to fill a gap. "
        "Classify the page as exactly one of: "
        "'text' (an ordinary page of body text), "
        "'blank' (no printed content at all), "
        "'illustration_only' (a plate, photograph or drawing with no caption "
        "or body text), "
        "'partial' (a half-title, a captioned illustration, a running header "
        "alone, or any page carrying only a small amount of real text -- "
        "transcribe just that text), "
        "or 'unreadable' (content is present but too damaged, blurred or "
        "obscured to transcribe with confidence -- say briefly why in `note`). "
        "Respond only with the requested JSON."
    )


# ---------------------------------------------------------------------
# Book profiling
# ---------------------------------------------------------------------

PROFILE_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "primary_language": {
            "type": "string",
            "description": "Main language of the book, as an English name.",
        },
        "other_languages": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Any additional languages appearing in the book.",
        },
        "script": {
            "type": "string",
            "description": (
                "Writing system, e.g. Latin, Gujarati, Devanagari, Arabic."
            ),
        },
        "era": {
            "type": "string",
            "description": (
                "Approximate period of printing, e.g. '17th-century', "
                "'early 19th-century'. Say 'unknown' if there is no evidence."
            ),
        },
        "typeface": {
            "type": "string",
            "description": (
                "Character of the type, e.g. 'roman', 'Fraktur/blackletter', "
                "'italic throughout', 'hand-set metal type with worn "
                "impressions'."
            ),
        },
        "orthographic_notes": {
            "type": "string",
            "description": (
                "One or two sentences on spelling and typographic conventions "
                "a transcriber should expect: long s, ligatures, archaic "
                "spellings, abbreviation marks, catchwords."
            ),
        },
        "title_as_printed": {
            "type": "string",
            "description": (
                "Title exactly as printed on the title page, if one is "
                "visible. Empty string otherwise."
            ),
        },
    },
    "required": ["primary_language", "script", "era"],
}


def build_profile_prompt() -> str:
    """
    Build the one-off, per-book profiling instruction.

    Shown several of the book's opening pages in a single request. Asking for
    honest uncertainty matters more than asking for a confident answer -- a
    guessed century propagated across 400 page prompts is worse than no
    century at all.
    """
    return (
        "These images are the first few pages of one scanned historical book. "
        "Identify the book's language, writing system and approximate period of "
        "printing, and describe the spelling and typographic conventions that "
        "someone transcribing it should expect. "
        "Base this only on what is visible. Where the evidence does not "
        "support a confident answer, say 'unknown' rather than estimating -- "
        "this profile will be applied to every page of the book, so a wrong "
        "guess is considerably worse than an admitted gap. "
        "Do not transcribe the pages. "
        "Respond only with the requested JSON."
    )
