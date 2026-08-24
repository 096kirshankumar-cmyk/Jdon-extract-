"""
Multi-key Gemini quota pool (2026-08-11).

WHY THIS EXISTS
---------------
The free tier's daily request cap (RPD) is enforced **per Google Cloud
project**, not per API key. Several keys minted inside ONE project therefore
share ONE bucket and rotating between them buys nothing. Keys belonging to
SEPARATE projects/accounts do have independent buckets, so rotating across
them multiplies the usable daily quota:

    6 independent projects x MAX_CALLS_PER_DAY (480) = 2880 calls/day

This module owns that rotation. The pipeline keeps its existing single
`state["calls_today"]` brake semantics; the only behavioural change is that
hitting the brake now ADVANCES TO THE NEXT KEY instead of ending the run, and
the run only stops once every key in the pool is spent.

CONFIGURATION (Railway -> Variables)
------------------------------------
Any ONE of these three forms works; they are checked in this order.

  1. GEMINI_API_KEYS   -- comma / whitespace / newline separated list:
                            GEMINI_API_KEYS = AIzaAAA...,AIzaBBB...,AIzaCCC...
  2. GEMINI_API_KEY_1 .. GEMINI_API_KEY_20  -- one variable per key:
                            GEMINI_API_KEY_1 = AIzaAAA...
                            GEMINI_API_KEY_2 = AIzaBBB...
  3. GEMINI_API_KEY    -- the original single-key variable (still supported,
                          nothing breaks for existing deployments).

Forms 1 and 2 are merged if both are present. Duplicates are removed while
preserving order, so pasting the same key twice cannot silently double the
apparent quota.

STATE
-----
Per-key counters live in `state["key_pool"]`, keyed by a NON-SECRET label
(`key1`, `key2`, ...) with a short fingerprint for log correlation. Full key
material is never written to state.json and never printed.

    state["key_pool"] = {
        "key1": {"fp": "...AAAA", "calls": 480, "status": "exhausted"},
        "key2": {"fp": "...BBBB", "calls": 37,  "status": "active"},
    }

Counters reset with the pipeline's existing day_stamp rollover. NOTE that
Google's RPD window resets at midnight PACIFIC (= 12:30 IST), not local
midnight, so a same-day reset can be one cycle behind the real quota refill.
That is intentionally conservative: the pool under-counts available quota
rather than over-counting it.
"""

import os
import time
import weakref

import google.generativeai as genai


# Order matters: the first non-empty source wins for form 1/2 merging.
_ENV_LIST = "GEMINI_API_KEYS"
_ENV_NUMBERED_PREFIX = "GEMINI_API_KEY_"
_ENV_SINGLE = "GEMINI_API_KEY"
_MAX_NUMBERED = 20

# A key that returns a per-MINUTE 429 is not out of daily quota -- it is just
# bursting. Park it for this long and keep using the rest of the pool.
_RPM_COOLDOWN_SECONDS = 60

# Models built by the pipeline, so their cached clients can be invalidated
# when the active key changes. Held weakly: tracking a model must never keep
# it alive or leak memory across a long run.
_tracked_models = weakref.WeakSet()


def track(model):
    """Register a GenerativeModel so key rotation can reset its client.

    Returns the model, so call sites can wrap construction inline:
        model = gemini_keys.track(genai.GenerativeModel(GEMINI_MODEL))
    """
    try:
        _tracked_models.add(model)
    except TypeError:      # not weak-referenceable; nothing we can do
        pass
    return model


def _clear_tracked_model_clients():
    """Drop cached clients so the next request picks up the new API key."""
    for model in list(_tracked_models):
        for attr in ("_client", "_async_client"):
            if getattr(model, attr, None) is not None:
                try:
                    setattr(model, attr, None)
                except Exception:
                    pass


class NoKeysConfigured(RuntimeError):
    pass


def _fingerprint(key):
    """Last 4 chars only -- enough to correlate logs, useless if leaked."""
    k = (key or "").strip()
    return f"...{k[-4:]}" if len(k) >= 4 else "...????"


def _split_list(raw):
    out = []
    for chunk in raw.replace("\n", ",").replace("\r", ",").replace("\t", ",").split(","):
        c = chunk.strip().strip('"').strip("'")
        if c:
            out.append(c)
    return out


def discover_keys(env=None):
    """Collect keys from every supported variable form, de-duplicated,
    order preserved. Returns [(label, key), ...]."""
    env = os.environ if env is None else env
    found = []

    raw_list = env.get(_ENV_LIST, "")
    if raw_list.strip():
        found.extend(_split_list(raw_list))

    for i in range(1, _MAX_NUMBERED + 1):
        v = env.get(f"{_ENV_NUMBERED_PREFIX}{i}", "")
        if v.strip():
            found.append(v.strip())

    single = env.get(_ENV_SINGLE, "")
    if single.strip():
        found.append(single.strip())

    seen, uniq = set(), []
    for k in found:
        if k not in seen:
            seen.add(k)
            uniq.append(k)

    return [(f"key{i}", k) for i, k in enumerate(uniq, start=1)]


class KeyPool:
    """Round-robin over independent Gemini projects with per-key daily brakes."""

    def __init__(self, keys, max_calls_per_day):
        if not keys:
            raise NoKeysConfigured(
                "No Gemini API key found. Set GEMINI_API_KEYS (comma-separated), "
                "or GEMINI_API_KEY_1..N, or GEMINI_API_KEY in Railway variables."
            )
        self._keys = keys                      # [(label, secret), ...]
        self._max = max_calls_per_day
        self._idx = 0
        self._cooldown = {}                    # label -> epoch seconds
        self._configured_label = None

    # ---------- state plumbing ----------

    def _bucket(self, state):
        pool = state.setdefault("key_pool", {})
        for label, key in self._keys:
            slot = pool.setdefault(label, {})
            slot.setdefault("fp", _fingerprint(key))
            slot.setdefault("calls", 0)
            slot.setdefault("status", "active")
        return pool

    def reset_day(self, state):
        """Called from the pipeline's existing day rollover."""
        pool = self._bucket(state)
        for label in pool:
            pool[label]["calls"] = 0
            pool[label]["status"] = "active"
        self._cooldown.clear()
        self._idx = 0

    # ---------- availability ----------

    def _available(self, state, label):
        pool = self._bucket(state)
        slot = pool[label]
        if slot["status"] == "exhausted":
            return False
        if slot["calls"] >= self._max:
            slot["status"] = "exhausted"
            return False
        if self._cooldown.get(label, 0) > time.time():
            return False
        return True

    def _apply(self, label):
        """genai.configure() for the given label (idempotent).

        CRITICAL: google-generativeai's GenerativeModel caches the client it
        resolved on its FIRST request (`self._client`). A later
        genai.configure() swaps the library default but the already-built
        model object keeps using the OLD client -- i.e. the old API key --
        so rotation would silently do nothing. Every model this pipeline
        builds is registered via `track()`, and switching keys clears those
        cached clients so the next request genuinely goes out on the new key.
        """
        if self._configured_label == label:
            return
        secret = dict(self._keys)[label]
        genai.configure(api_key=secret)
        _clear_tracked_model_clients()
        self._configured_label = label

    def active_label(self):
        return self._keys[self._idx][0]

    def activate(self, state):
        """Make sure SOME usable key is configured. Returns label or None if
        the whole pool is spent."""
        n = len(self._keys)
        for step in range(n):
            label = self._keys[(self._idx + step) % n][0]
            if self._available(state, label):
                if step:
                    self._idx = (self._idx + step) % n
                self._apply(label)
                return label
        return None

    def rotate(self, state, reason=""):
        """Advance past the current key and configure the next usable one.
        Returns the new label, or None when every key is spent."""
        prev = self.active_label()
        self._idx = (self._idx + 1) % len(self._keys)
        label = self.activate(state)
        if label is None:
            print(f"  [KEYPOOL] all {len(self._keys)} keys spent{' (' + reason + ')' if reason else ''}")
            return None
        pool = self._bucket(state)
        print(f"  [KEYPOOL] switching {prev} -> {label} "
              f"({reason or 'quota'}); {label} used {pool[label]['calls']}/{self._max}")
        return label

    def quota_exhausted(self, state):
        """The pipeline's brake. True ONLY when the DAILY budget of every key
        is spent. Rotates transparently when the current key is spent.

        A key that is merely RPM-parked still has daily budget, so the pool is
        NOT exhausted -- the run must wait out the cooldown rather than quit
        for the day. Only when no key can be woken by waiting do we brake.
        """
        if self.activate(state) is not None:
            return False
        wait = self._cooldown_remaining(state)
        if wait <= 0:
            return True          # genuinely out of daily budget everywhere
        print(f"  [KEYPOOL] all keys in rate-limit cooldown -- waiting "
              f"{int(wait) + 1}s for the earliest to recover "
              f"(daily budget remains: {self.remaining(state)} calls)")
        time.sleep(wait + 1)
        return self.activate(state) is None

    def _cooldown_remaining(self, state):
        """Seconds until the soonest RPM-parked key with daily budget wakes up.
        Returns 0 when no such key exists."""
        pool = self._bucket(state)
        now = time.time()
        waits = [self._cooldown[label] - now
                 for label, _ in self._keys
                 if pool[label]["status"] != "exhausted"
                 and pool[label]["calls"] < self._max
                 and self._cooldown.get(label, 0) > now]
        return min(waits) if waits else 0

    # ---------- accounting ----------

    def note_call(self, state):
        """One request was just sent on the active key."""
        pool = self._bucket(state)
        label = self.active_label()
        pool[label]["calls"] += 1
        if pool[label]["calls"] >= self._max:
            pool[label]["status"] = "exhausted"

    def note_429(self, state, err_text=""):
        """Classify a 429 and park the key accordingly.

        A per-DAY 429 means that project is finished until reset. A per-MINUTE
        429 is a burst: the project is fine, it just needs a breather, so the
        key is only parked briefly. Rotating on either is safe because the next
        key is a different project with its own independent RPM and RPD window.

        Returns the new active label, or None if the pool is spent."""
        pool = self._bucket(state)
        label = self.active_label()
        t = (err_text or "").lower()
        per_day = ("per day" in t or "perday" in t or "daily" in t
                   or "requests per day" in t or "generate_requests_per_model_per_day" in t)
        if per_day:
            pool[label]["status"] = "exhausted"
            pool[label]["calls"] = max(pool[label]["calls"], self._max)
            reason = "daily quota hit"
        else:
            self._cooldown[label] = time.time() + _RPM_COOLDOWN_SECONDS
            reason = f"rate burst, parked {_RPM_COOLDOWN_SECONDS}s"
        return self.rotate(state, reason)

    # ---------- reporting ----------

    def summary(self, state):
        pool = self._bucket(state)
        parts = []
        for label, _ in self._keys:
            s = pool[label]
            mark = "x" if s["status"] == "exhausted" else "."
            parts.append(f"{label}{s['fp']} {s['calls']}/{self._max}{mark}")
        return " | ".join(parts)

    def remaining(self, state):
        pool = self._bucket(state)
        return sum(max(0, self._max - pool[l]["calls"]) for l, _ in self._keys)

    def size(self):
        return len(self._keys)


# ---------------------------------------------------------------
# Module-level singleton -- the pipeline has one pool per process.
# ---------------------------------------------------------------
_POOL = None


def init(state, max_calls_per_day, env=None):
    """Build the pool, configure the first usable key, print a banner.
    Safe to call more than once (later calls just re-activate)."""
    global _POOL
    if _POOL is None:
        _POOL = KeyPool(discover_keys(env), max_calls_per_day)
        print(f"[KEYPOOL] {_POOL.size()} Gemini key(s) loaded; "
              f"budget {max_calls_per_day}/key/day "
              f"= {_POOL.size() * max_calls_per_day} calls/day total")
        if _POOL.size() > 1:
            print("[KEYPOOL] reminder: this only multiplies quota if each key "
                  "belongs to a SEPARATE Google project/account "
                  "(quota is enforced per project, not per key)")
    label = _POOL.activate(state)
    if label is None:
        print(f"[KEYPOOL] every key already spent today -- {_POOL.summary(state)}")
    return _POOL


def pool():
    """The live pool. Raises if init() was never called."""
    if _POOL is None:
        raise NoKeysConfigured("gemini_keys.init(state, MAX_CALLS_PER_DAY) was never called")
    return _POOL
