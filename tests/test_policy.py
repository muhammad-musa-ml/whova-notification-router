from datetime import datetime, time, timezone

import pytest

from notify_router.model import Settings
from notify_router.policy import in_quiet_hours, next_digest, next_local, quiet_release

LA = "America/Los_Angeles"


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def test_quiet_hours_wrap_midnight():
    s = Settings(zone=LA)
    assert in_quiet_hours(utc(2026, 11, 11, 6, 30), s)       # 22:30 the evening before, PST
    assert in_quiet_hours(utc(2026, 11, 11, 14, 59), s)      # 06:59 PST
    assert not in_quiet_hours(utc(2026, 11, 11, 15, 0), s)   # 07:00 PST, quiet hours have ended
    assert not in_quiet_hours(utc(2026, 11, 11, 5, 59), s)   # 21:59 PST


def test_quiet_hours_follow_the_zone_not_utc():
    # 13:00 UTC is 05:00 in Los Angeles (quiet) and 08:00 in New York (not quiet).
    at = utc(2026, 11, 10, 13, 0)
    assert in_quiet_hours(at, Settings(zone=LA))
    assert not in_quiet_hours(at, Settings(zone="America/New_York"))


def test_release_on_the_fall_back_night_is_seven_local_not_seven_hours_later():
    # 2026-11-01: Los Angeles leaves PDT (UTC-7) for PST (UTC-8) at 02:00. A notification at 23:30 PDT
    # on 31 October waits until 07:00 PST, which is 15:00 UTC, not 14:00 UTC.
    at = utc(2026, 11, 1, 6, 30)
    assert quiet_release(at, Settings(zone=LA)) == utc(2026, 11, 1, 15, 0)


def test_release_on_the_spring_forward_night():
    # 2026-03-08: PST becomes PDT at 02:00. 22:30 PST on 7 March waits until 07:00 PDT = 14:00 UTC.
    at = utc(2026, 3, 8, 6, 30)
    assert quiet_release(at, Settings(zone=LA)) == utc(2026, 3, 8, 14, 0)


def test_a_wall_time_inside_the_spring_forward_gap_lands_after_it():
    # 02:30 does not exist on 2026-03-08 in Los Angeles; it is read as PST and becomes 03:30 PDT.
    at = utc(2026, 3, 8, 8, 0)  # 00:00 PST
    assert next_local(at, time(2, 30), LA) == utc(2026, 3, 8, 10, 30)


def test_next_local_returns_the_same_instant_when_the_clock_already_reads_it():
    at = utc(2026, 11, 10, 15, 0)  # 07:00 PST
    assert next_local(at, time(7, 0), LA) == at


def test_next_digest_picks_the_earliest_upcoming_slot():
    s = Settings(zone=LA)
    assert next_digest(utc(2026, 11, 10, 18, 0), s) == utc(2026, 11, 10, 20, 30)  # 10:00 -> 12:30
    assert next_digest(utc(2026, 11, 10, 21, 0), s) == utc(2026, 11, 11, 2, 30)   # 13:00 -> 18:30
    assert next_digest(utc(2026, 11, 11, 3, 0), s) == utc(2026, 11, 11, 20, 30)   # 19:00 -> next 12:30


@pytest.mark.parametrize("kwargs, message", [
    ({"digest_times": (time(23, 0),)}, "inside quiet hours"),
    ({"digest_times": ()}, "at least one"),
    ({"muted_sources": frozenset({"organizer"})}, "cannot be muted"),
    ({"max_pushes_per_hour": 0}, "at least 1"),
])
def test_settings_refuse_impossible_values(kwargs, message):
    with pytest.raises(ValueError, match=message):
        Settings(**kwargs)
