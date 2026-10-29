"""What the router reads: one notification, and one attendee's settings."""
from dataclasses import dataclass
from datetime import datetime, time

SOURCES = ("organizer", "whova", "attendee", "sponsor")
# Sources an attendee can turn off. The organizer's messages are why the app is on the phone, and the
# existing per-type settings already cover them, so the router never mutes them wholesale.
MUTABLE_SOURCES = frozenset({"whova", "attendee", "sponsor"})
# Kinds that can wait for the next digest instead of buzzing the phone now.
DIGEST_KINDS = frozenset({"app_tip", "app_promotion", "sponsor_offer"})


@dataclass(frozen=True)
class Notification:
    id: str
    attendee_id: str
    source: str
    kind: str
    created: datetime  # timezone-aware
    urgent: bool = False
    thread: str | None = None  # a conversation; pushes in one thread collapse

    def __post_init__(self):
        if self.source not in SOURCES:
            raise ValueError(f"unknown source {self.source!r}")
        if self.created.tzinfo is None:
            raise ValueError("created must be timezone-aware")
        if self.urgent and self.source != "organizer":
            raise ValueError("only the organizer can send an urgent notification")


@dataclass(frozen=True)
class Settings:
    zone: str = "America/Los_Angeles"  # IANA zone whose wall clock quiet hours and digests follow
    quiet_start: time = time(22, 0)
    quiet_end: time = time(7, 0)
    digest_times: tuple = (time(12, 30), time(18, 30))
    max_pushes_per_hour: int = 6
    thread_window_minutes: int = 10
    muted_sources: frozenset = frozenset()

    def __post_init__(self):
        if not self.digest_times:
            raise ValueError("at least one digest time is needed")
        unknown = set(self.muted_sources) - MUTABLE_SOURCES
        if unknown:
            raise ValueError(f"these sources cannot be muted: {sorted(unknown)}")
        if self.max_pushes_per_hour < 1:
            raise ValueError("max_pushes_per_hour must be at least 1")
        for t in self.digest_times:
            if in_window(t, self.quiet_start, self.quiet_end):
                raise ValueError(f"digest time {t} falls inside quiet hours")


def in_window(t, start, end):
    """Whether wall time t lies in [start, end); a window whose start is after its end wraps midnight."""
    if start <= end:
        return start <= t < end
    return t >= start or t < end
