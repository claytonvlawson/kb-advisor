#!/usr/bin/env python3
"""ratelimit.py -- bound what one client can spend.

`/chat` calls a paid model, so an unmetered endpoint is an open wallet. The
frontend proxy has a limiter, but it lives in a serverless instance's memory: it
resets on cold start, is not shared, and does not exist for anything talking to
the port directly. This is the server-side control.

Two mechanisms, deliberately separate:

  TokenBucketLimiter  smooths request rate per client identity. Requests over
                      the bucket get 429 with Retry-After.
  Semaphore cap       bounds how many generations are in flight at once, because
                      each one holds prompt buffers and a paid request. Over the
                      cap a request waits briefly and then gets 503 rather than
                      piling up.

Identity matters more than the algorithm. Trusting a client-supplied
X-Forwarded-For header turns the limiter into decoration, so it is only read
when ADVISOR_TRUST_PROXY=1, which is correct only behind a proxy that overwrites
the header (see deploy/Caddyfile: header_up X-Forwarded-For {remote_host}).
"""
from __future__ import annotations

import threading
import time


class TokenBucketLimiter:
    """Per-identity token bucket with a bounded number of tracked identities.

    `rate_per_min` is the sustained allowance and also the burst capacity, which
    keeps the maths obvious: a client may spend its bucket immediately and then
    refill at rate_per_min / 60 tokens per second.
    """

    def __init__(self, rate_per_min=30, max_identities=4096, clock=time.monotonic):
        self.rate_per_min = float(rate_per_min)
        self.capacity = max(1.0, self.rate_per_min)
        self.refill_per_s = self.rate_per_min / 60.0
        self.max_identities = int(max_identities)
        self.clock = clock
        self._buckets = {}
        self._lock = threading.Lock()
        self.last_prune = 0.0

    @property
    def enabled(self):
        return self.rate_per_min > 0

    def allow(self, identity, cost=1.0):
        """Return (allowed, retry_after_seconds)."""
        if not self.enabled:
            return True, 0.0
        now = self.clock()
        with self._lock:
            self._maybe_prune(now)
            tokens, last = self._buckets.get(identity, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - last) * self.refill_per_s)
            if tokens >= cost:
                self._buckets[identity] = (tokens - cost, now)
                return True, 0.0
            self._buckets[identity] = (tokens, now)
            shortfall = cost - tokens
            wait = shortfall / self.refill_per_s if self.refill_per_s > 0 else 60.0
            return False, max(1.0, min(3600.0, wait))

    def _maybe_prune(self, now):
        """Drop buckets that have refilled completely: they carry no state.

        Without this the dict is itself a memory attack, one key per spoofed
        identity, which is exactly what an unmetered endpoint invites.
        """
        if len(self._buckets) < self.max_identities and (now - self.last_prune) < 60:
            return
        self.last_prune = now
        full_refill = self.capacity / self.refill_per_s if self.refill_per_s else float("inf")
        stale = [k for k, (tokens, last) in self._buckets.items()
                 if tokens >= self.capacity or (now - last) > full_refill]
        for k in stale:
            self._buckets.pop(k, None)
        if len(self._buckets) >= self.max_identities:
            # Still full: drop the least recently seen half rather than grow.
            ordered = sorted(self._buckets.items(), key=lambda kv: kv[1][1])
            for k, _ in ordered[: len(ordered) // 2]:
                self._buckets.pop(k, None)

    def tracked(self):
        with self._lock:
            return len(self._buckets)


class ConcurrencyGate:
    """A semaphore with a bounded wait, so load sheds instead of queueing."""

    def __init__(self, limit=2, wait_s=10.0):
        self.limit = max(1, int(limit))
        self.wait_s = float(wait_s)
        self._sem = threading.BoundedSemaphore(self.limit)
        self._in_flight = 0
        self._lock = threading.Lock()
        self.rejected = 0

    @property
    def in_flight(self):
        with self._lock:
            return self._in_flight

    def __enter__(self):
        if not self._sem.acquire(timeout=self.wait_s):
            with self._lock:
                self.rejected += 1
            raise BusyError("more than %d generations in flight" % self.limit)
        with self._lock:
            self._in_flight += 1
        return self

    def __exit__(self, *exc):
        with self._lock:
            self._in_flight -= 1
        self._sem.release()
        return False


class BusyError(RuntimeError):
    pass
