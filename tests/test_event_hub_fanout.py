"""One poller per Project behind every SSE subscriber (#359).

Every ``/events/stream`` connection used to run its own ``list_events`` loop, so
a Room with three viewers read the same rows three times per interval and the
database cost of watching a Project grew with the number of watchers. These tests
pin the two properties that matter: one read per Project however many
subscribers there are, and unchanged ``Last-Event-ID`` resume semantics.
"""

from __future__ import annotations

import threading
import time

from agentchatroom.event_hub import CLOSED, TIMEOUT, ProjectEventHub

POLL = 0.01


class FakeLog:
    """A minimal stand-in for one Project's event log."""

    def __init__(self):
        self.events: list[dict] = []
        self.reads = 0
        self._lock = threading.Lock()

    def append(self, count: int = 1) -> list[dict]:
        with self._lock:
            added = []
            for _ in range(count):
                seq = len(self.events) + 1
                event = {"project_seq": seq, "event_type": "message.posted"}
                self.events.append(event)
                added.append(event)
            return added

    def fetch(self, project_id: str, after: int, limit: int) -> dict:
        # The hub is the only caller; counting here is how "one read per
        # Project" is observed rather than assumed.
        with self._lock:
            self.reads += 1
            return {
                "events": [
                    event for event in self.events if event["project_seq"] > after
                ][:limit],
                "latest_cursor": len(self.events),
            }


def _drain(subscriber, expected: int, timeout: float = 2.0) -> list[dict]:
    got: list[dict] = []
    deadline = time.monotonic() + timeout
    while len(got) < expected and time.monotonic() < deadline:
        item = subscriber.get(POLL)
        if item is TIMEOUT:
            continue
        if item is CLOSED:
            break
        got.append(item)
    return got


def _wait_for(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(POLL)
    return predicate()


# ---------------------------------------------------------------------------
# fan-out: one read per Project, not per subscriber
# ---------------------------------------------------------------------------


def test_many_subscribers_cost_one_read_per_project():
    log = FakeLog()
    hub = ProjectEventHub(log.fetch, poll_interval_seconds=lambda: POLL)
    subscribers = [hub.subscribe("project_a", 0) for _ in range(8)]
    try:
        log.append(1)
        assert _wait_for(lambda: all(s.cursor == 1 for s in subscribers))
        first = log.reads
        assert first >= 1
        # Eight subscribers, one Project: the read count must track elapsed
        # polls, not subscriber count.
        time.sleep(POLL * 20)
        added = log.reads - first
        assert added < 20, f"read count grew with subscribers: {added} extra reads"
    finally:
        for subscriber in subscribers:
            hub.unsubscribe(subscriber)


def test_every_subscriber_receives_the_same_event():
    log = FakeLog()
    hub = ProjectEventHub(log.fetch, poll_interval_seconds=lambda: POLL)
    subscribers = [hub.subscribe("project_a", 0) for _ in range(4)]
    try:
        log.append(2)
        for subscriber in subscribers:
            events = _drain(subscriber, 2)
            assert [event["project_seq"] for event in events] == [1, 2]
    finally:
        for subscriber in subscribers:
            hub.unsubscribe(subscriber)


def test_projects_poll_independently():
    log = FakeLog()
    hub = ProjectEventHub(log.fetch, poll_interval_seconds=lambda: POLL)
    first = hub.subscribe("project_a", 0)
    second = hub.subscribe("project_b", 0)
    try:
        log.append(1)
        assert _wait_for(lambda: first.cursor == 1 and second.cursor == 1)
        assert hub.subscriber_count("project_a") == 1
        assert hub.subscriber_count("project_b") == 1
    finally:
        hub.unsubscribe(first)
        hub.unsubscribe(second)


def test_last_subscriber_leaving_stops_the_worker():
    log = FakeLog()
    hub = ProjectEventHub(log.fetch, poll_interval_seconds=lambda: POLL)
    subscriber = hub.subscribe("project_a", 0)
    assert _wait_for(lambda: hub.poll_count >= 1)
    hub.unsubscribe(subscriber)
    settled = log.reads
    time.sleep(POLL * 10)
    assert log.reads == settled
    assert hub.subscriber_count() == 0


def test_subscribe_racing_the_last_unsubscribe_leaves_one_live_worker():
    """A client reconnecting as another leaves must not kill the new poller."""
    log = FakeLog()
    hub = ProjectEventHub(log.fetch, poll_interval_seconds=lambda: POLL)
    leaving = hub.subscribe("project_a", 0)
    assert _wait_for(lambda: hub.poll_count >= 1)

    arriving = hub.subscribe("project_a", 0)
    hub.unsubscribe(leaving)
    try:
        # The hub must still be polling for the subscriber that stayed.
        before = hub.poll_count
        assert _wait_for(lambda: hub.poll_count > before, timeout=5.0)
        log.append(1)
        assert _wait_for(lambda: arriving.cursor == 1, timeout=5.0)
    finally:
        hub.unsubscribe(arriving)


# ---------------------------------------------------------------------------
# resume semantics: Last-Event-ID still means "everything after this number"
# ---------------------------------------------------------------------------


def test_a_late_subscriber_still_receives_events_older_than_the_hub_head():
    log = FakeLog()
    hub = ProjectEventHub(log.fetch, poll_interval_seconds=lambda: POLL)
    early = hub.subscribe("project_a", 0)
    try:
        log.append(3)
        assert _wait_for(lambda: early.cursor == 3)
        # A client reconnecting with Last-Event-ID: 0 asks for the whole log
        # even though the hub has already read past it.
        late = hub.subscribe("project_a", 0)
        events = _drain(late, 3)
        assert [event["project_seq"] for event in events] == [1, 2, 3]
        hub.unsubscribe(late)
    finally:
        hub.unsubscribe(early)


def test_a_subscriber_resuming_from_a_cursor_gets_only_newer_events():
    log = FakeLog()
    hub = ProjectEventHub(log.fetch, poll_interval_seconds=lambda: POLL)
    keeper = hub.subscribe("project_a", 0)
    try:
        log.append(3)
        assert _wait_for(lambda: keeper.cursor == 3)
        resumed = hub.subscribe("project_a", 3)
        log.append(2)
        events = _drain(resumed, 2)
        assert [event["project_seq"] for event in events] == [4, 5]
        hub.unsubscribe(resumed)
    finally:
        hub.unsubscribe(keeper)


def test_each_event_is_delivered_to_a_subscriber_exactly_once():
    log = FakeLog()
    hub = ProjectEventHub(log.fetch, poll_interval_seconds=lambda: POLL)
    subscriber = hub.subscribe("project_a", 0)
    try:
        log.append(5)
        events = _drain(subscriber, 5)
        time.sleep(POLL * 10)
        seqs = [event["project_seq"] for event in events]
        assert seqs == sorted(seqs)
        assert len(seqs) == len(set(seqs)) == 5
    finally:
        hub.unsubscribe(subscriber)


def test_subscriber_cursor_never_moves_backwards():
    log = FakeLog()
    hub = ProjectEventHub(log.fetch, poll_interval_seconds=lambda: POLL)
    subscriber = hub.subscribe("project_a", 0)
    try:
        log.append(4)
        assert _wait_for(lambda: subscriber.cursor == 4)
        seen = subscriber.cursor
        # The shared read position never rewinds below a live subscriber's
        # own position, which is what used to make a resumed client re-read.
        for _ in range(5):
            hub._poll_once()
            assert subscriber.cursor >= seen
    finally:
        hub.unsubscribe(subscriber)


# ---------------------------------------------------------------------------
# lifecycle and backpressure
# ---------------------------------------------------------------------------


def test_unsubscribe_wakes_the_subscriber():
    log = FakeLog()
    hub = ProjectEventHub(log.fetch, poll_interval_seconds=lambda: POLL)
    subscriber = hub.subscribe("project_a", 0)
    hub.unsubscribe(subscriber)
    # A closed subscriber must not sit blocking on an empty queue forever.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        item = subscriber.get(POLL)
        if item is CLOSED:
            return
    raise AssertionError("unsubscribe did not wake the subscriber")


def test_a_stalled_subscriber_is_dropped_instead_of_stalling_the_project():
    """One browser tab that stops reading must not block the other clients."""
    log = FakeLog()
    hub = ProjectEventHub(
        log.fetch, poll_interval_seconds=lambda: POLL, batch_size=200
    )
    stalled = hub.subscribe("project_a", 0)
    healthy = hub.subscribe("project_a", 0)
    try:
        # Far more events than the stalled subscriber's bounded queue holds,
        # so it must be dropped rather than allowed to hold the poller.
        for _ in range(60):
            log.append(200)
        assert _wait_for(lambda: stalled.dropped, timeout=10.0)
        # The healthy subscriber was still being served throughout, and the
        # worker was never blocked by the one that stopped reading.
        assert _wait_for(lambda: healthy.cursor > 200, timeout=10.0)
        assert hub.poll_count > 1
    finally:
        hub.unsubscribe(stalled)
        hub.unsubscribe(healthy)


def test_stop_ends_every_subscriber_and_the_worker():
    log = FakeLog()
    hub = ProjectEventHub(log.fetch, poll_interval_seconds=lambda: POLL)
    subscribers = [hub.subscribe("project_a", 0) for _ in range(3)]
    assert _wait_for(lambda: hub.poll_count >= 1)
    hub.stop()
    for subscriber in subscribers:
        assert subscriber.get(POLL) is CLOSED
    settled = log.reads
    time.sleep(POLL * 10)
    assert log.reads == settled


def test_a_failing_read_does_not_kill_the_worker():
    log = FakeLog()
    calls = {"count": 0}

    def flaky(project_id, after, limit):
        calls["count"] += 1
        if calls["count"] % 2:
            raise RuntimeError("transient read failure")
        return log.fetch(project_id, after, limit)

    hub = ProjectEventHub(flaky, poll_interval_seconds=lambda: POLL)
    subscriber = hub.subscribe("project_a", 0)
    try:
        log.append(1)
        # The hub keeps polling after the error, so the event still arrives.
        assert _wait_for(lambda: subscriber.cursor == 1)
    finally:
        hub.unsubscribe(subscriber)