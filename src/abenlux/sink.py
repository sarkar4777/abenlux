"""
Derived-record sink: the boundary between the on-device edge agent and the central collector.

This is the piece that makes the privacy posture hold for thousands of developers. Two modes:

  * LOCAL (SqliteSink)  - a single-machine / solo deployment writes derived records to a local
                          store. Used by the demo and by a developer running everything locally.
  * FORWARD (HttpSink)  - the org topology. The edge agent runs on the developer's machine,
                          redacts and derives there, then ships ONLY content-free DerivedRecords
                          to the central collector. Raw prompts, responses, and the raw identity
                          never leave the device.

At thousands of users, one HTTP POST per model call would melt the collector, so HttpSink
batches: records accumulate in a bounded buffer and flush when the buffer fills or ages out,
as a single multi-record POST. If the collector is unreachable the buffer is retained (a bounded
spool) and retried on the next flush - delivery is at-least-once, and the collector dedups on
event_id, so a retried batch is idempotent. A collector outage degrades to delayed/dropped
telemetry, never a broken developer call. `flush()`/`close()` drain the buffer (wired to atexit).
"""
from __future__ import annotations

import atexit
import threading
from collections import deque
from typing import Callable, Protocol

from abenlux.schema import DerivedRecord


class DerivedSink(Protocol):
    def insert(self, record: DerivedRecord) -> None: ...


class SqliteSink:
    """local persistence wrapper. delegates to a store so the demo/solo path is unchanged."""

    def __init__(self, store):
        self.store = store

    def insert(self, record: DerivedRecord) -> None:
        self.store.insert(record)

    def flush(self) -> None:  # parity with HttpSink
        pass

    def stats(self) -> dict:
        return {"mode": "local", "dropped": 0, "buffered": 0}


def _default_post(url: str, batch: list[dict], token: str, timeout: float) -> bool:
    import httpx
    r = httpx.post(url, json=batch, headers={"Authorization": f"Bearer {token}"}, timeout=timeout)
    if r.status_code < 300:
        return True
    if 400 <= r.status_code < 500 and r.status_code not in (408, 429):
        # a PERMANENT client error (bad token, malformed) - retrying forever would wedge the spool
        # head behind a poison batch, so we drop it (loudly) instead of blocking delivery of the rest.
        import sys
        print(f"abenlux: collector rejected a derived batch ({r.status_code}), dropping {len(batch)} "
              f"records (check ABEN_INGEST_TOKEN)", file=sys.stderr)
        return True
    return False  # 5xx / 408 / 429 -> transient, keep and retry


class HttpSink:
    """forward derived records to the central collector, batched and spooled. thread-safe: the
    gateway inserts from BackgroundTask threadpool threads."""

    def __init__(
        self,
        url: str,
        token: str,
        *,
        batch_size: int = 50,
        max_age_s: float = 5.0,
        max_spool: int = 10_000,
        timeout: float = 5.0,
        post: Callable[[str, list, str, float], bool] | None = None,
        clock: Callable[[], float] | None = None,
        auto_flush: bool = False,
    ):
        self.endpoint = url.rstrip("/") + "/v1/derived"
        self.token = token
        self.batch_size = batch_size
        self.max_age_s = max_age_s
        self.max_spool = max_spool
        self.timeout = timeout
        self._post = post or _default_post
        import time as _time
        self._clock = clock or _time.monotonic
        self._buf: deque[dict] = deque()
        self._lock = threading.Lock()
        self._last_flush = self._clock()
        self.dropped = 0
        atexit.register(self.flush)
        if auto_flush:
            # a background heartbeat flushes an aged, partial batch even when no new call arrives to
            # trigger the size/age check (otherwise the tail of a session can sit buffered indefinitely)
            self._stop = threading.Event()
            threading.Thread(target=self._age_loop, daemon=True).start()

    def _age_loop(self) -> None:
        while not self._stop.wait(self.max_age_s):
            try:
                if self._buf:
                    self.flush()
            except Exception:
                pass

    def insert(self, record: DerivedRecord) -> None:
        with self._lock:
            self._buf.append(record.to_dict())
            while len(self._buf) > self.max_spool:
                self._buf.popleft()           # bounded spool: drop oldest under sustained outage
                self.dropped += 1
            due = len(self._buf) >= self.batch_size or (self._clock() - self._last_flush) >= self.max_age_s
        if due:
            self.flush()

    def flush(self) -> None:
        # take the batch OUT of the buffer before the (slow) POST, so concurrent inserts and any spool
        # overflow during the POST can't shift indices and make the success-path pop the wrong records.
        with self._lock:
            if not self._buf:
                self._last_flush = self._clock()
                return
            batch = list(self._buf)
            self._buf.clear()
            self._last_flush = self._clock()
        ok = False
        try:
            ok = self._post(self.endpoint, batch, self.token, self.timeout)
        except Exception:
            ok = False
        if not ok:
            with self._lock:
                self._buf.extendleft(reversed(batch))     # put it back at the FRONT, order preserved
                while len(self._buf) > self.max_spool:
                    self._buf.popleft()
                    self.dropped += 1

    def stats(self) -> dict:
        # content-free delivery health for /health: how many records are buffered awaiting delivery,
        # and how many the spool dropped under a sustained collector outage (otherwise invisible loss).
        with self._lock:
            return {"mode": "forward", "dropped": self.dropped, "buffered": len(self._buf)}

    def close(self) -> None:
        if getattr(self, "_stop", None):
            self._stop.set()
        self.flush()


def build_sink(settings, *, local_store) -> DerivedSink:
    """choose the sink from config. forward to the collector if a URL is set, else write local."""
    if settings.collector_url:
        return HttpSink(settings.collector_url, settings.ingest_token, auto_flush=True)
    return SqliteSink(local_store)
