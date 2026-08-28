"""
Ledger — API key pool and quota accounting.

This is the most delicate module in the project, because on the free tier your
daily request allowance IS the bottleneck. Everything here exists to answer one
question correctly: "which key, if any, may I use right now?"

Three things it gets right that the original HTML app got wrong:

1. Quota resets at midnight US PACIFIC, not local midnight. Running from India,
   local-midnight accounting zeroes the counters about eleven and a half hours
   early, after which the app believes it has a full allowance and spends half a
   day collecting 429s while blaming the wrong keys.

2. A 429 is not one thing. Daily exhaustion means "come back tomorrow";
   per-minute throttling means "wait thirty seconds". Treating them alike either
   wastes most of a day's quota or hammers a wall. They are distinguished here.

3. Keys can die permanently. With keys spread across many accounts, some will be
   disabled without warning. A dead key must be retired loudly, not retried
   forever in a silent loop.

Key state machine
-----------------

    active     -- usable now
    cooldown   -- transient problem (per-minute throttle, 5xx); retry after a delay
    exhausted  -- daily allowance spent; returns to active at the next Pacific reset
    dead       -- key rejected outright (revoked/invalid); never retried automatically

Only `dead` requires human intervention. The other three resolve themselves.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


# Google resets requests-per-day quotas at midnight Pacific time. Using the
# named zone rather than a fixed offset means daylight saving is handled for us.
#
# zoneinfo resolves this name against the operating system's timezone database.
# Linux and macOS ship one; Windows does not, and falls back to the `tzdata`
# package. Since this is a module-level constant, a missing database would
# otherwise raise an obscure ZoneInfoNotFoundError at import time and take the
# whole application down, so the failure is translated into something
# actionable.
try:
    PACIFIC = ZoneInfo("America/Los_Angeles")
except ZoneInfoNotFoundError as exc:  # pragma: no cover - platform dependent
    raise RuntimeError(
        "No timezone database found for 'America/Los_Angeles'. Ledger needs it "
        "because Gemini resets daily quotas at midnight US Pacific. On Windows, "
        "install the tzdata package:  pip install tzdata"
    ) from exc


# Key states.
STATE_ACTIVE = "active"
STATE_COOLDOWN = "cooldown"
STATE_EXHAUSTED = "exhausted"
STATE_DEAD = "dead"


# Substrings that appear in Gemini quota errors specifically about the DAILY
# limit. If we see one of these, the key is finished until the Pacific reset
# rather than merely being throttled for a few seconds.
DAILY_QUOTA_MARKERS = (
    "perday",
    "per day",
    "requests_per_day",
    "requestsperday",
    "daily limit",
)


# After this many consecutive rate-limit responses with no success in between,
# assume the key has hit a daily wall we cannot see. This protects us when the
# configured rpd_limit is higher than the account's real allowance — which is
# likely, since Google no longer publishes per-model free-tier RPD figures and
# explicitly says the numbers are not guaranteed.
CONSECUTIVE_429_MEANS_EXHAUSTED = 4


# When repeated 429s teach us the real daily ceiling, we only believe the lesson
# if the key had already completed at least this many requests today.
#
# Without this floor there is a nasty failure mode: a burst of 429s early in the
# day — from RPM pacing being slightly wrong, or from two keys unexpectedly
# sharing a project quota — would write rpd_limit down to near zero. That value
# is PERSISTED, and an exhausted key returns to active at midnight with its
# limit intact, so the key would be permanently crippled by one bad minute.
# Below the floor we still park the key for the day, but we leave its configured
# limit alone so tomorrow starts clean.
LEARN_LIMIT_MIN_OBSERVED = 10


def pacific_date(now: datetime | None = None) -> str:
    """
    The current quota day as an ISO date string, in US Pacific time.

    This string is what we store against each key's usage counter. When it
    changes, the counter resets. Everything about daily quota hangs off this
    one function.
    """
    moment = now.astimezone(PACIFIC) if now else datetime.now(PACIFIC)
    return moment.date().isoformat()


def seconds_until_pacific_reset(now: datetime | None = None) -> float:
    """
    Seconds remaining until the next midnight Pacific.

    Used only for display — telling you when exhausted keys come back.
    """
    moment = now.astimezone(PACIFIC) if now else datetime.now(PACIFIC)
    tomorrow = (moment + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return (tomorrow - moment).total_seconds()


@dataclass
class ApiKey:
    """
    One API key plus everything we know about its current standing.

    Field names match the `api_keys` table columns so persistence stays a
    straightforward mapping (see db.py).
    """

    id: int
    label: str
    secret: str

    # Requests per minute allowed for this key. Used for pacing, not for
    # counting — we simply never send two requests on one key closer together
    # than 60/rpm_limit seconds apart.
    #
    # Set to 0 to disable pacing entirely and let the daily limit be the only
    # constraint. Useful if you find your account tolerates bursts, but the
    # default of pacing is safer: a 429 storm wastes wall-clock time even
    # though it costs no quota.
    rpm_limit: int = 10

    # Requests per day. READ THIS OFF AI STUDIO for one of your accounts
    # rather than trusting a number from a blog post; free-tier figures have
    # changed repeatedly and are not guaranteed.
    rpd_limit: int = 250

    state: str = STATE_ACTIVE

    # The Pacific date `used_today` refers to. When today's Pacific date
    # differs from this, the counter is stale and gets reset.
    used_on: str = ""
    used_today: int = 0

    # Unix timestamp before which this key must not be used. Set when
    # cooling down after a throttle or server error.
    cooldown_until: float = 0.0

    # Unix timestamp of the most recent request sent on this key, for RPM
    # pacing.
    last_used_at: float = 0.0

    # Consecutive rate-limit responses with no intervening success.
    consecutive_rate_limits: int = 0

    # Human-readable reason for the current state, surfaced in the UI. The
    # most important use is explaining why a key is dead.
    last_error: str = ""

    def refresh_for_today(self, today: str | None = None) -> bool:
        """
        Roll the daily counter over if we have crossed into a new Pacific day.

        Returns True if anything changed, so the caller knows to persist.
        """
        today = today or pacific_date()

        if self.used_on == today:
            return False

        self.used_on = today
        self.used_today = 0
        self.consecutive_rate_limits = 0

        # A new day revives an exhausted key. It does NOT revive a dead one:
        # a revoked key stays revoked.
        if self.state == STATE_EXHAUSTED:
            self.state = STATE_ACTIVE
            self.last_error = ""

        return True

    @property
    def min_request_spacing(self) -> float:
        """
        Minimum seconds between two requests on this key, from its RPM limit.

        A limit of 0 (or less) means "no pacing" — the daily allowance becomes
        the only constraint.
        """
        if self.rpm_limit <= 0:
            return 0.0
        return 60.0 / float(self.rpm_limit)

    def available_at(self, now: float | None = None) -> float | None:
        """
        The earliest timestamp this key could be used, or None if it cannot be
        used today at all.

        A return value in the past means "usable right now".
        """
        now = now if now is not None else time.time()

        if self.state in (STATE_DEAD, STATE_EXHAUSTED):
            return None

        # Daily allowance spent, even if the state has not caught up yet.
        if self.used_today >= self.rpd_limit:
            return None

        # Respect both the cooldown (set by errors) and the RPM pacing gap.
        earliest = max(
            self.cooldown_until,
            self.last_used_at + self.min_request_spacing,
        )
        return earliest

    @property
    def remaining_today(self) -> int:
        return max(0, self.rpd_limit - self.used_today)


class KeyPool:
    """
    Thread-safe pool that hands out keys to worker threads.

    Usage from a worker:

        key = pool.acquire()          # None means "nothing available right now"
        if key is None: sleep and retry
        ... make the request ...
        pool.record_success(key) / record_rate_limited(key, ...) / etc.

    `acquire` reserves the key by stamping `last_used_at` immediately, so two
    threads can never be handed the same key inside its RPM spacing window.
    """

    def __init__(self, keys: list[ApiKey], on_change=None):
        """
        `on_change` is an optional callback invoked with an ApiKey whenever its
        persisted state changes, so db.py can write it back without this class
        needing to know about SQLite.
        """
        self._keys: list[ApiKey] = list(keys)
        self._lock = threading.Lock()
        self._on_change = on_change

    # -----------------------------------------------------------------
    # Internal helpers (always called with the lock held)
    # -----------------------------------------------------------------

    def _persist(self, key: ApiKey) -> None:
        if self._on_change is not None:
            self._on_change(key)

    def _expire_cooldowns(self, now: float, today: str) -> None:
        """Move keys out of transient states once their conditions have passed."""
        for key in self._keys:
            changed = key.refresh_for_today(today)

            # A cooldown that has elapsed returns the key to service.
            if key.state == STATE_COOLDOWN and now >= key.cooldown_until:
                key.state = STATE_ACTIVE
                key.last_error = ""
                changed = True

            # Reaching the daily limit through ordinary use.
            if key.state == STATE_ACTIVE and key.used_today >= key.rpd_limit:
                key.state = STATE_EXHAUSTED
                key.last_error = "Daily allowance spent"
                changed = True

            if changed:
                self._persist(key)

    # -----------------------------------------------------------------
    # Public interface
    # -----------------------------------------------------------------

    def acquire(self) -> ApiKey | None:
        """
        Reserve the best available key, or return None if none can be used now.

        "Best" means the key that has been idle longest among those whose RPM
        gap has elapsed. That spreads load evenly rather than hammering the
        first key in the list until it is exhausted, which matters because it
        keeps every account's usage pattern similar.
        """
        now = time.time()
        today = pacific_date()

        with self._lock:
            self._expire_cooldowns(now, today)

            ready: list[ApiKey] = []
            for key in self._keys:
                earliest = key.available_at(now)
                if earliest is not None and earliest <= now:
                    ready.append(key)

            if not ready:
                return None

            # Least-recently-used first.
            chosen = min(ready, key=lambda k: k.last_used_at)

            # Stamp immediately, inside the lock. This is the reservation: any
            # other thread arriving now sees the RPM gap and skips this key.
            chosen.last_used_at = now
            return chosen

    def next_available_in(self) -> float | None:
        """
        Seconds until some key becomes usable, or None if none will today.

        Lets the worker loop sleep exactly as long as needed instead of
        polling blindly.
        """
        now = time.time()
        today = pacific_date()

        with self._lock:
            self._expire_cooldowns(now, today)

            waits = []
            for key in self._keys:
                earliest = key.available_at(now)
                if earliest is not None:
                    waits.append(max(0.0, earliest - now))

            return min(waits) if waits else None

    def record_success(self, key: ApiKey) -> None:
        """Count one successful request against the key's daily allowance."""
        with self._lock:
            # `dead` is terminal. Two workers can legitimately hold the same
            # key object at once, so a slower one may report an outcome after
            # a faster one has already retired the key -- and moving it back
            # out of `dead` would put a revoked key back into rotation to be
            # retried forever.
            if key.state == STATE_DEAD:
                return

            key.refresh_for_today()
            key.used_today += 1
            key.consecutive_rate_limits = 0
            key.last_error = ""

            if key.used_today >= key.rpd_limit:
                key.state = STATE_EXHAUSTED
                key.last_error = "Daily allowance spent"

            self._persist(key)

    def record_rate_limited(
        self,
        key: ApiKey,
        message: str = "",
        retry_after: float | None = None,
    ) -> str:
        """
        Handle a 429.

        Decides between "exhausted for the day" and "throttled for a moment",
        and returns whichever state it chose so the caller can log it.

        A rate limit does NOT count as a page attempt: it says nothing about
        whether the page is transcribable.
        """
        with self._lock:
            # See record_success: `dead` is terminal and never reversed.
            if key.state == STATE_DEAD:
                return STATE_DEAD

            key.refresh_for_today()
            key.consecutive_rate_limits += 1

            lowered = (message or "").lower()
            mentions_daily = any(m in lowered for m in DAILY_QUOTA_MARKERS)

            # Three independent reasons to conclude the day is over.
            hit_configured_limit = key.used_today >= key.rpd_limit
            repeated_refusals = (
                key.consecutive_rate_limits >= CONSECUTIVE_429_MEANS_EXHAUSTED
            )

            if mentions_daily or hit_configured_limit or repeated_refusals:
                key.state = STATE_EXHAUSTED

                # Record what the account ACTUALLY allowed, so tomorrow's run
                # paces itself against reality rather than our guess. This is
                # how the pool self-corrects when rpd_limit was set too high.
                #
                # Only trusted above the floor — see LEARN_LIMIT_MIN_OBSERVED
                # for why writing down a near-zero limit would be permanent
                # damage rather than a useful correction.
                learnable = (
                    repeated_refusals
                    and not hit_configured_limit
                    and key.used_today >= LEARN_LIMIT_MIN_OBSERVED
                )

                if learnable:
                    key.rpd_limit = key.used_today
                    key.last_error = (
                        f"Daily limit reached at {key.used_today} requests "
                        "(learned from repeated 429s)"
                    )
                elif repeated_refusals and not hit_configured_limit:
                    # Refused repeatedly but too early to draw conclusions.
                    # Park it for today only; the configured limit stands.
                    key.last_error = (
                        f"Refused repeatedly after only {key.used_today} "
                        "request(s) today — parked until the next reset"
                    )
                else:
                    key.last_error = "Daily allowance spent"

                self._persist(key)
                return STATE_EXHAUSTED

            # Otherwise treat it as a per-minute throttle. Back off
            # exponentially, honouring Retry-After when the server sends it.
            backoff = retry_after if retry_after else 15.0 * key.consecutive_rate_limits
            backoff = min(backoff, 300.0)

            key.state = STATE_COOLDOWN
            key.cooldown_until = time.time() + backoff
            key.last_error = f"Rate limited, waiting {int(backoff)}s"

            self._persist(key)
            return STATE_COOLDOWN

    def record_transient_error(self, key: ApiKey, message: str = "") -> None:
        """
        Handle a 5xx or a network failure — the service's problem, not the key's.

        Short fixed cooldown so one flaky moment does not sideline a key.
        """
        with self._lock:
            # See record_success: `dead` is terminal and never reversed. This
            # path is the one that actually bit -- a transient error arriving
            # on a key another worker had just retired would revive it.
            if key.state == STATE_DEAD:
                return

            key.state = STATE_COOLDOWN
            key.cooldown_until = time.time() + 30.0
            key.last_error = f"Server error, retrying: {message[:120]}"
            self._persist(key)

    def record_dead(self, key: ApiKey, message: str = "") -> None:
        """
        Retire a key permanently (403, invalid key, account disabled).

        Never retried automatically. With keys spread across many accounts this
        will happen, and it needs to be visible rather than silently draining
        worker threads.
        """
        with self._lock:
            key.state = STATE_DEAD
            key.last_error = f"Key rejected: {message[:200]}"
            self._persist(key)

    # -----------------------------------------------------------------
    # Reporting
    # -----------------------------------------------------------------

    def snapshot(self) -> list[dict]:
        """Current standing of every key, for the UI and the status command."""
        now = time.time()
        today = pacific_date()

        with self._lock:
            self._expire_cooldowns(now, today)

            return [
                {
                    "id": key.id,
                    "label": key.label,
                    "state": key.state,
                    "used_today": key.used_today,
                    "rpd_limit": key.rpd_limit,
                    "rpm_limit": key.rpm_limit,
                    "remaining_today": key.remaining_today,
                    "cooldown_seconds": max(0, int(key.cooldown_until - now)),
                    "last_error": key.last_error,
                }
                for key in self._keys
            ]

    def capacity_today(self) -> dict:
        """
        Total remaining requests across all live keys.

        This is the number that actually tells you how much work can be done
        before the next Pacific reset.
        """
        with self._lock:
            today = pacific_date()
            for key in self._keys:
                if key.refresh_for_today(today):
                    self._persist(key)

            live = [k for k in self._keys if k.state != STATE_DEAD]

            return {
                "keys_total": len(self._keys),
                "keys_live": len(live),
                "keys_dead": len(self._keys) - len(live),

                # Spend is counted across ALL keys including dead ones: a key
                # that was revoked at noon still spent whatever it spent that
                # morning, and excluding it would understate the day's usage.
                "used_today": sum(k.used_today for k in self._keys),

                # Remaining capacity, by contrast, is only what LIVE keys can
                # still do — a dead key's unused allowance is not available.
                "remaining_today": sum(k.remaining_today for k in live),

                "seconds_to_reset": int(seconds_until_pacific_reset()),
            }

    def has_live_keys(self) -> bool:
        """False when every key is dead — the run cannot continue at all."""
        with self._lock:
            return any(k.state != STATE_DEAD for k in self._keys)
