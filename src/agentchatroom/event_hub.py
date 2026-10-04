"""One database poller per Project, fanned out to every SSE subscriber (#359).

Every ``/events/stream`` connection used to run its own ``list_events`` poll
loop. With the 64-connection-per-Project cap that is up to 64 identical
``SELECT``s every poll interval, all reading the same rows and all decoding the
same JSON — the read cost of a Room grew with the number of viewers rather than
with the amount of activity. A Project with three clients paid three times for
one event.

:class:`ProjectEventHub` inverts that: one worker thread per Project reads the
event log once per interval and hands each event to every live subscriber's
bounded queue. Cost is now one poll per Project regardless of client count, and
a slow reader drops its own oldest backlog instead of stalling the hub.

The resume semantics are the part that must not drift. ``Last-Event-ID`` and
``?after=`` are both positions in the same ``project_seq`` space, so a
subscriber joins at its own position and receives strictly newer events in
strictly increasing order — exactly what an independent poller looping on
``list_events(after=cursor)`` delivered. A subscriber that falls behind is
dropped and told to reconnect with the cursor it last saw, which is what the
independent poller did too (it silently skipped, which is worse).
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Any, Callable

logger = logging.getLogger(__name__)

# How long a subscriber may fall behind before it is considered gone. Sized so a
# browser tab backgrounded briefly still resumes from its own cursor, while a tab
# that stops reading at all cannot pin memory in the hub.
SUBSCRIBER_QUEUE_SIZE = 512


class _Timeout:
    """Sentinel: nothing arrived within the wait, but the stream is healthy."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<timeout>"


class _Closed:
    """Sentinel: the hub or the subscriber is finished; end the stream."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<closed>"


TIMEOUT = _Timeout()
CLOSED = _Closed()


class Subscriber:
    """One SSE connection's view of a Project's event stream."""

    def __init__(self, project_id: str, after: int) -> None:
        self.project_id = project_id
        self.cursor = int(after)
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=SUBSCRIBER_QUEUE_SIZE)
        self.dropped = False
        self.closed = False

    def offer(self, event: dict[str, Any]) -> bool:
        """Queue one event, or report the subscriber as gone if it cannot keep up.

        Never blocks: a blocking offer here would let one stalled browser tab
        stall the poller and therefore every other client of the Project.
        """
        try:
            self._queue.put_nowait(event)
            return True
        except queue.Full:
            self.dropped = True
            return False

    def get(self, timeout: float) -> Any:
        """Next event, ``TIMEOUT`` if none arrived, ``CLOSED`` to stop."""
        try:
            item = self._queue.get(timeout=timeout)
        except queue.Empty:
            return TIMEOUT
        if item is None:
            self.closed = True
            return CLOSED
        return item

    def close(self) -> None:
        self.closed = True
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            # The subscriber will read its backlog and then see closed; the
            # route closes it explicitly right after unsubscribing anyway.
            self.dropped = True


class ProjectEventHub:
    """Fan-out of one Project's event log to every live SSE subscriber.

    One worker thread exists per Project *with subscribers*; it is joined and
    discarded when the last subscriber leaves, so an idle service holds no
    threads. Delivery of a *new* event to a *given* subscriber is unchanged
    from the per-connection poller: events arrive in ``project_seq`` order,
    strictly after that subscriber's own cursor, and each is yielded once.
    """

    def __init__(
        self,
        fetch: Callable[[str, int, int], dict[str, Any]],
        *,
        poll_interval_seconds: Callable[[], float],
        batch_size: int = 200,
    ) -> None:
        self._fetch = fetch
        self._poll_interval = poll_interval_seconds
        self._batch_size = max(1, int(batch_size))
        self._lock = threading.Lock()
        self._subscribers: dict[int, Subscriber] = {}
        self._cursors: dict[str, int] = {}
        self._worker: tuple[threading.Thread, threading.Event] | None = None
        self._next_id = 0
        self._polls = 0
        self._delivered = 0

    # -- subscriber lifecycle ------------------------------------------------

    def subscribe(self, project_id: str, after: int) -> Subscriber:
        """Register a subscriber positioned at ``after`` and start the worker."""
        with self._lock:
            self._next_id += 1
            subscriber = Subscriber(project_id, after)
            self._subscribers[self._next_id] = subscriber
            # The worker reads from the lowest live cursor, so a newcomer asking
            # for older events is served rather than skipped.
            current = self._cursors.get(project_id)
            self._cursors[project_id] = (
                int(after) if current is None else min(current, int(after))
            )
            start = self._worker is None
        if start:
            self._start_worker()
        return subscriber

    def unsubscribe(self, subscriber: Subscriber) -> None:
        # Whether to stop the worker is decided under the lock and the thread is
        # joined outside it: subscribe() racing this call either sees a live
        # worker and leaves it alone, or sees none and starts a fresh one.
        with self._lock:
            for key, value in list(self._subscribers.items()):
                if value is subscriber:
                    self._subscribers.pop(key, None)
            self._recompute_cursor(subscriber.project_id)
            retiring = self._worker if not self._subscribers else None
            if retiring is not None:
                self._worker = None
        subscriber.close()
        if retiring is not None:
            self._join_worker(retiring)

    def _recompute_cursor(self, project_id: str) -> None:
        cursors = [
            sub.cursor
            for sub in self._subscribers.values()
            if sub.project_id == project_id
        ]
        if cursors:
            self._cursors[project_id] = min(cursors)
        else:
            self._cursors.pop(project_id, None)

    # -- worker --------------------------------------------------------------

    def _start_worker(self) -> None:
        with self._lock:
            if self._worker is not None:
                return
            # Each worker owns its own stop flag, so a retiring thread is never
            # affected by the flag of the worker that replaces it.
            stop = threading.Event()
            worker = threading.Thread(
                target=self._run,
                args=(stop,),
                name="project-event-hub",
                daemon=True,
            )
            self._worker = (worker, stop)
            worker.start()

    @staticmethod
    def _join_worker(worker: tuple[threading.Thread, threading.Event]) -> None:
        thread, stop = worker
        stop.set()
        thread.join(timeout=2.0)

    def _stop_worker(self) -> None:
        """Join the worker if one is running (service shutdown / tests)."""
        with self._lock:
            worker = self._worker
            self._worker = None
        if worker is not None:
            self._join_worker(worker)

    def stop(self) -> None:
        """Stop the worker and wake every subscriber (service shutdown)."""
        with self._lock:
            subscribers = list(self._subscribers.values())
            self._subscribers.clear()
            self._cursors.clear()
        for subscriber in subscribers:
            subscriber.close()
        self._stop_worker()

    def _run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self._poll_once()
            except Exception:  # pragma: no cover - defensive
                logger.exception("project event hub poll failed")
            interval = max(0.01, float(self._poll_interval()))
            stop.wait(interval)

    def _poll_once(self) -> bool:
        """One read per active Project. Returns True when anything was delivered."""
        with self._lock:
            targets = dict(self._cursors)
        if not targets:
            return False
        delivered = 0
        for project_id, cursor in targets.items():
            try:
                batch = self._fetch(project_id, cursor, self._batch_size)
            except Exception:  # pragma: no cover - defensive
                logger.exception("event fetch failed for one project")
                continue
            events = list(batch.get("events") or [])
            latest = int(batch.get("latest_cursor") or cursor)
            delivered += len(events)
            self._advance(project_id, cursor, events, latest)
        with self._lock:
            self._polls += 1
            self._delivered += delivered
        return delivered > 0

    def _advance(
        self,
        project_id: str,
        cursor: int,
        events: list[dict[str, Any]],
        latest: int,
    ) -> None:
        with self._lock:
            subs = [
                sub
                for sub in self._subscribers.values()
                if sub.project_id == project_id
            ]
        for event in events:
            seq = int(event["project_seq"])
            for subscriber in subs:
                # Strictly newer than this subscriber's own position: a
                # reconnect replay and a fresh poll must not double-deliver.
                if seq > subscriber.cursor:
                    subscriber.cursor = seq
                    subscriber.offer(event)
        with self._lock:
            # The shared read position is the lowest live cursor. When a poll
            # returned nothing new, `latest` may be ahead of it, but it must
            # not advance past a position somebody still needs — a subscriber
            # resuming from an older cursor has to be served those events.
            current = self._cursors.get(project_id)
            if current is None:
                self._cursors[project_id] = int(latest)
            self._recompute_cursor(project_id)

    # -- introspection for tests --------------------------------------------

    @property
    def poll_count(self) -> int:
        with self._lock:
            return self._polls

    @property
    def delivered_count(self) -> int:
        with self._lock:
            return self._delivered

    def subscriber_count(self, project_id: str | None = None) -> int:
        with self._lock:
            if project_id is None:
                return len(self._subscribers)
            return sum(
                1
                for sub in self._subscribers.values()
                if sub.project_id == project_id
            )