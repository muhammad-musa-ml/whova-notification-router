import os
from datetime import datetime, timedelta, timezone

import fakeredis
import pytest

from notify_router.model import Notification, Settings
from notify_router.router import Router
from notify_router.store import MemoryStore, RedisStore

LA = "America/Los_Angeles"


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def stores():
    yield pytest.param(MemoryStore, id="memory")
    yield pytest.param(lambda: RedisStore(fakeredis.FakeRedis()), id="fakeredis")
    if os.environ.get("REDIS_URL"):  # CI starts a real Redis service and sets this
        import redis

        def real():
            client = redis.Redis.from_url(os.environ["REDIS_URL"])
            client.flushdb()
            return RedisStore(client)
        yield pytest.param(real, id="redis")


@pytest.fixture(params=list(stores()))
def make_router(request):
    def build(**settings):
        return Router(request.param(), Settings(zone=LA, **settings))
    return build


_ids = iter(range(1, 10**6))


def note(source="organizer", kind="announcement", at=None, urgent=False, thread=None, who="a1"):
    at = at or utc(2026, 11, 10, 18, 0)  # 10:00 PST on a conference day
    return Notification(f"n{next(_ids)}", who, source, kind, at, urgent, thread)


def test_an_ordinary_announcement_is_pushed(make_router):
    assert make_router().route(note()).action == "push"


def test_urgent_goes_out_during_quiet_hours(make_router):
    d = make_router().route(note(kind="room_change", urgent=True, at=utc(2026, 11, 10, 12, 0)))  # 04:00
    assert (d.action, d.reason) == ("push", "urgent")


def test_only_the_organizer_can_send_urgent():
    with pytest.raises(ValueError, match="only the organizer"):
        note(source="whova", kind="app_tip", urgent=True)


def test_a_muted_source_stays_in_the_inbox(make_router):
    router = make_router(muted_sources=frozenset({"whova"}))
    assert router.route(note(source="whova", kind="app_tip")).action == "inbox"
    assert router.route(note()).action == "push"  # the organizer still gets through


def test_a_message_at_five_am_waits_for_seven(make_router):
    d = make_router().route(note(at=utc(2026, 11, 10, 13, 0)))  # 05:00 PST
    assert (d.action, d.release_at) == ("hold", utc(2026, 11, 10, 15, 0))


def test_a_tip_waits_for_the_digest(make_router):
    d = make_router().route(note(source="whova", kind="app_tip"))  # 10:00 PST
    assert (d.action, d.release_at) == ("hold", utc(2026, 11, 10, 20, 30))  # 12:30 PST


def test_a_busy_thread_buzzes_once_then_collapses(make_router):
    router = make_router()
    t0 = utc(2026, 11, 10, 18, 0)
    first = router.route(note(source="attendee", kind="dm", thread="t1", at=t0))
    second = router.route(note(source="attendee", kind="dm", thread="t1", at=t0 + timedelta(minutes=2)))
    other = router.route(note(source="attendee", kind="dm", thread="t2", at=t0 + timedelta(minutes=3)))
    assert first.action == "push"
    assert (second.action, second.release_at) == ("hold", t0 + timedelta(minutes=10))
    assert other.action == "push"  # a different conversation is not held


def test_the_hourly_limit_holds_until_there_is_room(make_router):
    router = make_router(max_pushes_per_hour=3)
    t0 = utc(2026, 11, 10, 18, 0)
    for i in range(3):
        assert router.route(note(at=t0 + timedelta(minutes=10 * i))).action == "push"
    d = router.route(note(at=t0 + timedelta(minutes=30)))
    assert (d.action, d.release_at) == ("hold", t0 + timedelta(hours=1))


def test_a_hold_that_would_end_in_quiet_hours_waits_for_morning(make_router):
    router = make_router()
    t0 = utc(2026, 11, 11, 5, 55)  # 21:55 PST
    router.route(note(source="attendee", kind="dm", thread="t1", at=t0))
    d = router.route(note(source="attendee", kind="dm", thread="t1", at=t0 + timedelta(minutes=1)))
    assert d.release_at == utc(2026, 11, 11, 15, 0)  # not 22:05, but 07:00


def test_flush_sends_everything_due_as_one_push(make_router):
    router = make_router()
    router.route(note(source="whova", kind="app_tip"))
    router.route(note(source="sponsor", kind="sponsor_offer"))
    assert router.flush("a1", utc(2026, 11, 10, 20, 0)) is None  # 12:00, not due yet
    digest = router.flush("a1", utc(2026, 11, 10, 20, 30))
    assert len(digest.items) == 2
    assert digest.text() == "2 updates: 1 from sponsor, 1 from whova"
    assert router.flush("a1", utc(2026, 11, 10, 20, 31)) is None  # nothing is sent twice


def test_flush_only_touches_its_own_attendee(make_router):
    router = make_router()
    router.route(note(source="whova", kind="app_tip", who="a1"))
    router.route(note(source="whova", kind="app_tip", who="a2"))
    assert len(router.flush("a1", utc(2026, 11, 10, 20, 30)).items) == 1
    assert len(router.flush("a2", utc(2026, 11, 10, 20, 30)).items) == 1


def test_a_digest_counts_toward_the_thread_window(make_router):
    router = make_router()
    t0 = utc(2026, 11, 10, 18, 0)
    router.route(note(source="attendee", kind="dm", thread="t1", at=t0))
    router.route(note(source="attendee", kind="dm", thread="t1", at=t0 + timedelta(minutes=1)))
    router.flush("a1", t0 + timedelta(minutes=10))
    d = router.route(note(source="attendee", kind="dm", thread="t1", at=t0 + timedelta(minutes=12)))
    assert (d.action, d.release_at) == ("hold", t0 + timedelta(minutes=20))
