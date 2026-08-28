"""
Ledger — Gemini client.

Deliberately built on `requests` against the REST endpoint rather than the
google-genai SDK. The SDK adds its own retry, quota and auth behaviour, which
is exactly the behaviour we need precise control over when the whole design
hangs on knowing which key spent which request. Plain HTTP means what we send
is what we wrote.

The most important thing in this file is not the request building -- it is
`_classify_error`. Everything upstream depends on knowing WHICH KIND of failure
just happened:

    RateLimited     -> quota problem. Do not blame the page. Do not count an
                       attempt. The key pool decides whether this means
                       "wait 30 seconds" or "come back tomorrow".
    KeyRejected     -> the key is dead. Retire it loudly; never auto-retry.
    TransientError  -> the service's problem. Short cooldown, try again.
    BadResponse     -> a problem with THIS page. Count an attempt.

Collapsing these into one generic exception is how you end up either burning a
day's allowance retrying a wall or marking perfectly good pages as failed.
"""

from __future__ import annotations

import base64
import json
import time

import requests

from .prompts import (
    PAGE_RESPONSE_SCHEMA,
    PROFILE_RESPONSE_SCHEMA,
    PROMPT_VERSION,
    build_page_prompt,
    build_profile_prompt,
)


# ---------------------------------------------------------------------
# Typed errors
# ---------------------------------------------------------------------

class GeminiError(Exception):
    """Base class for everything this module raises."""


class RateLimited(GeminiError):
    """
    HTTP 429. Either the daily allowance is spent or we are being throttled
    per-minute; this class carries the raw message so the key pool can tell
    which (see quota.DAILY_QUOTA_MARKERS).
    """

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class KeyRejected(GeminiError):
    """
    The key itself was refused: revoked, invalid, or its project disabled.
    Permanent until a human intervenes.
    """


class TransientError(GeminiError):
    """A 5xx, a timeout, or a connection failure. Worth retrying shortly."""


class BadResponse(GeminiError):
    """
    The call succeeded but the payload was unusable -- malformed JSON, a
    missing required field, no model output. This one IS the page's fault (or
    the prompt's), so it counts as an attempt.
    """


# Substrings in an error body that mean the key is finished for good rather
# than merely rate limited. These are the phrasings Google's own error payloads
# use — see the 401/403 handling in _classify_error for why matching one is a
# REQUIREMENT before retiring a key, not just a hint.
DEAD_KEY_MARKERS = (
    "api_key_invalid",
    "api key not valid",
    "api_key",
    "permission_denied",
    "unauthenticated",
    "consumer_suspended",
    "project_denied",
    "billing",
    "disabled",
    "service_disabled",
)


def _classify_error(status_code: int, body: str, headers=None) -> GeminiError:
    """
    Map an HTTP failure onto one of our four error kinds.

    Read this alongside quota.py: the two together are the whole reliability
    story for a run that has to survive weeks and twenty keys of varying
    health.
    """
    lowered = (body or "").lower()
    snippet = (body or "")[:400]

    # 429 is always a quota signal. Honour Retry-After if the server sent one,
    # since its number beats our backoff guess.
    if status_code == 429:
        retry_after = None
        if headers:
            raw = headers.get("Retry-After") or headers.get("retry-after")
            if raw:
                try:
                    retry_after = float(raw)
                except (TypeError, ValueError):
                    retry_after = None
        return RateLimited(snippet, retry_after=retry_after)

    # 401 and 403 usually mean the credential is not acceptable — but NOT
    # always, and getting this wrong is expensive.
    #
    # Anything between us and Google can also answer 403: a corporate proxy, a
    # firewall with a host allowlist, a captive portal, a misconfigured VPN.
    # Retiring a key is permanent and never retried, so if a proxy outage were
    # classified this way it would silently retire all twenty keys in a couple
    # of minutes and the run would be unrecoverable without manual repair.
    #
    # So we require positive evidence that GOOGLE rejected the credential —
    # one of the markers above, which appear in its error payloads. A 403 that
    # says nothing about keys or permissions came from something else in the
    # path, and that is a transient condition: retry once the network is fixed.
    if status_code in (401, 403):
        if any(marker in lowered for marker in DEAD_KEY_MARKERS):
            return KeyRejected(f"HTTP {status_code}: {snippet}")
        return TransientError(
            f"HTTP {status_code} from an intermediary, not from Google — "
            f"check network/proxy/firewall access to the API host: {snippet}"
        )

    # A 400 is normally OUR fault (bad request shape) -- but Google also
    # returns 400 for an invalid API key, so check the body before deciding.
    if status_code == 400:
        if any(marker in lowered for marker in DEAD_KEY_MARKERS):
            return KeyRejected(f"HTTP 400: {snippet}")
        return BadResponse(f"HTTP 400 (bad request): {snippet}")

    # Everything the server admits is its own problem.
    if status_code >= 500:
        return TransientError(f"HTTP {status_code}: {snippet}")

    return BadResponse(f"HTTP {status_code}: {snippet}")


# ---------------------------------------------------------------------
# Request building
# ---------------------------------------------------------------------

def _image_content(
    image_bytes: bytes,
    mime_type: str,
    media_resolution: str | None,
) -> dict:
    """
    Build one image content item.

    `resolution` is the per-content-item media resolution, available on Gemini
    3 models. "ultra_high" allocates 2240 image tokens against 1120 at the
    default -- twice the visual detail.

    Worth being explicit about why we always max this: on the free tier a
    request is a request, so the extra tokens are free. Even on paid tier the
    input side is a small fraction of the bill next to transcription output,
    so there is no reading of the economics where skimping here makes sense.
    """
    content: dict = {
        "type": "image",
        "data": base64.b64encode(image_bytes).decode("ascii"),
        "mime_type": mime_type,
    }

    if media_resolution:
        content["resolution"] = media_resolution

    return content


def _build_body(
    model: str,
    prompt: str,
    images: list[tuple[bytes, str]],
    schema: dict,
    media_resolution: str | None,
    thinking_level: str | None,
) -> dict:
    """Assemble an Interactions API request body."""
    body: dict = {
        "model": model,

        # We keep our own durable record in SQLite; there is no reason to have
        # Google retain the interaction as well.
        "store": False,

        "input": [{"type": "text", "text": prompt}]
        + [
            _image_content(image_bytes, mime_type, media_resolution)
            for image_bytes, mime_type in images
        ],

        # Structured output. Constraining the model to a schema is what makes
        # the results machine-usable without post-hoc parsing heuristics.
        "response_format": {
            "type": "text",
            "mime_type": "application/json",
            "schema": schema,
        },
    }

    # ------------------------------------------------------------------
    # THINKING CONFIGURATION -- VERIFY BEFORE A LONG RUN.
    #
    # Transcription gains nothing from deliberation, and thinking tokens bill
    # as output and add latency, so we want this at its floor.
    #
    # The catch: the shape of this field has changed between API versions
    # (`thinking_budget` was replaced by `thinking_level`), and it is the one
    # thing in this file I could not verify against a live endpoint. If your
    # first request comes back as a 400 complaining about an unknown field,
    # set LEDGER_THINKING_LEVEL="" to drop it entirely -- the default
    # behaviour is fine, just slightly slower.
    # ------------------------------------------------------------------
    if thinking_level:
        body["thinking_level"] = thinking_level

    return body


def _extract_json_payload(response_json: dict) -> dict:
    """
    Pull the model's JSON object out of an Interactions API response.

    The response is a list of steps; we want the text content of the
    `model_output` step. Written defensively because a malformed response here
    must raise BadResponse (costing one page attempt) rather than an
    AttributeError that kills the worker thread.
    """
    steps = response_json.get("steps") or []

    model_step = next(
        (step for step in steps if step.get("type") == "model_output"),
        None,
    )
    if model_step is None:
        raise BadResponse("No model_output step in response")

    text_block = next(
        (
            content
            for content in (model_step.get("content") or [])
            if content.get("type") == "text"
        ),
        None,
    )
    if text_block is None or not text_block.get("text"):
        raise BadResponse("No text content in model_output")

    raw = text_block["text"].strip()

    # Strip markdown fences if the model wrapped its JSON despite being asked
    # for a bare object. Cheap insurance against an otherwise wasted request.
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1]
        if raw.rstrip().endswith("```"):
            raw = raw.rstrip()[:-3]

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BadResponse(f"Model did not return valid JSON: {raw[:200]}") from exc

    if not isinstance(parsed, dict):
        raise BadResponse(f"Expected a JSON object, got {type(parsed).__name__}")

    return parsed


def _extract_token_usage(response_json: dict) -> tuple[int, int]:
    """
    Best-effort input/output token counts.

    Several field names have been used across API generations, so we try the
    ones we know and fall back to zeros. This is bookkeeping for your own
    spend tracking, never load-bearing -- a missing count must not fail a page
    that transcribed perfectly well.
    """
    for container_key in ("usage", "usage_metadata", "usageMetadata"):
        usage = response_json.get(container_key)
        if not isinstance(usage, dict):
            continue

        input_tokens = (
            usage.get("input_tokens")
            or usage.get("prompt_token_count")
            or usage.get("promptTokenCount")
            or 0
        )
        output_tokens = (
            usage.get("output_tokens")
            or usage.get("candidates_token_count")
            or usage.get("candidatesTokenCount")
            or 0
        )
        return int(input_tokens), int(output_tokens)

    return 0, 0


# ---------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------

class GeminiClient:
    """
    One client, shared across worker threads.

    The API key is passed per call rather than held on the instance, because
    which key to use is a scheduling decision that belongs to the KeyPool. A
    `requests.Session` is used for connection reuse and is thread-safe for
    this pattern.
    """

    def __init__(self, config):
        self.config = config
        self.session = requests.Session()

    def _post(self, api_key: str, body: dict) -> tuple[dict, int]:
        """
        Send one request. Returns (parsed_json, latency_ms).

        Raises one of our four typed errors on any failure.
        """
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        }

        started = time.monotonic()

        try:
            response = self.session.post(
                self.config.api_base,
                headers=headers,
                json=body,
                timeout=self.config.request_timeout,
            )
        except requests.Timeout as exc:
            raise TransientError(f"Request timed out: {exc}") from exc
        except requests.RequestException as exc:
            # DNS failures, connection resets, proxy problems: all the
            # service's or the network's problem, none of them the page's.
            raise TransientError(f"Network error: {exc}") from exc

        latency_ms = int((time.monotonic() - started) * 1000)

        if not response.ok:
            raise _classify_error(
                response.status_code,
                response.text,
                response.headers,
            )

        try:
            return response.json(), latency_ms
        except ValueError as exc:
            raise BadResponse(
                f"Response was not JSON: {response.text[:200]}"
            ) from exc

    # -----------------------------------------------------------------
    # Page transcription
    # -----------------------------------------------------------------

    def transcribe_page(
        self,
        api_key: str,
        image_bytes: bytes,
        profile: dict | None,
    ) -> dict:
        """
        Transcribe one rendered page.

        Returns a dict of the model's fields plus `_latency_ms`,
        `_input_tokens` and `_output_tokens`. The underscore prefix marks
        metadata we added, keeping it distinguishable from what the model
        actually said.
        """
        prompt = build_page_prompt(
            profile,
            preserve_layout=self.config.preserve_layout,
        )

        body = _build_body(
            model=self.config.model,
            prompt=prompt,
            images=[(image_bytes, self.config.image_mime_type)],
            schema=PAGE_RESPONSE_SCHEMA,
            media_resolution=self.config.media_resolution,
            thinking_level=self.config.thinking_level,
        )

        response_json, latency_ms = self._post(api_key, body)
        result = _extract_json_payload(response_json)

        # page_type drives everything downstream, so its absence is fatal to
        # the result even though the schema asked for it.
        if not result.get("page_type"):
            raise BadResponse("Response is missing page_type")

        # Normalise optional fields so callers never have to guard for them.
        result.setdefault("transcription", "")
        result.setdefault("footnotes", "")
        result.setdefault("printed_page_number", "")
        result.setdefault("languages", [])
        result.setdefault("has_uncertain_text", False)
        result.setdefault("note", "")

        input_tokens, output_tokens = _extract_token_usage(response_json)
        result["_latency_ms"] = latency_ms
        result["_input_tokens"] = input_tokens
        result["_output_tokens"] = output_tokens
        result["_prompt_version"] = PROMPT_VERSION

        return result

    # -----------------------------------------------------------------
    # Book profiling
    # -----------------------------------------------------------------

    def profile_book(
        self,
        api_key: str,
        sample_images: list[bytes],
    ) -> dict:
        """
        Establish a book's language, script and era from its opening pages.

        All sample pages go in ONE request, so this costs a single unit of
        daily allowance per book rather than one per sample page.
        """
        if not sample_images:
            raise BadResponse("No sample pages supplied for profiling")

        body = _build_body(
            model=self.config.model,
            prompt=build_profile_prompt(),
            images=[
                (image_bytes, self.config.image_mime_type)
                for image_bytes in sample_images
            ],
            schema=PROFILE_RESPONSE_SCHEMA,
            media_resolution=self.config.media_resolution,
            thinking_level=self.config.thinking_level,
        )

        response_json, _ = self._post(api_key, body)
        profile = _extract_json_payload(response_json)

        if not profile.get("primary_language"):
            raise BadResponse("Profile response is missing primary_language")

        # "unknown" is a legitimate, useful answer -- describe_profile() knows
        # to omit an unknown era rather than asserting one.
        for key in ("era", "script"):
            if str(profile.get(key, "")).strip().lower() in {"unknown", "n/a", ""}:
                profile[key] = ""

        return profile
