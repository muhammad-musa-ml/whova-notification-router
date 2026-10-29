"""Wall-clock rules: quiet hours and digest times are local times in the attendee's zone.

Instants are compared as timezone-aware datetimes and stored in UTC; only these functions look at the
local clock, so a daylight-saving change moves the UTC instant of 7:00 and nothing else.
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from notify_router.model import in_window


def in_quiet_hours(at, settings):
    local = at.astimezone(ZoneInfo(settings.zone))
    return in_window(local.time(), settings.quiet_start, settings.quiet_end)


def next_local(at, wall, zone):
    """The first instant at or after `at` when the clock in `zone` reads `wall`, in UTC.

    A wall time that does not exist on a spring-forward night is read with the offset in force before
    the change, which lands it the length of the gap later (02:30 becomes 03:30).
    """
    tz = ZoneInfo(zone)
    start = at.astimezone(tz).date()
    for days in range(3):
        candidate = datetime.combine(start + timedelta(days=days), wall, tzinfo=tz).astimezone(timezone.utc)
        if candidate >= at:
            return candidate
    raise AssertionError("a wall time recurs every day")  # unreachable for any real zone


def quiet_release(at, settings):
    return next_local(at, settings.quiet_end, settings.zone)


def next_digest(at, settings):
    return min(next_local(at, t, settings.zone) for t in settings.digest_times)
