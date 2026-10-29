"""Decide, for each notification, whether to push it now, hold it for later, or leave it in the inbox.

Every notification still reaches the attendee's in-app inbox; the router decides only whether and when
the phone buzzes. Rules, first match wins:

1. urgent (only the organizer can mark one, e.g. a room change): push now, even in quiet hours
2. from a source the attendee muted: inbox only, no push
3. during quiet hours: hold until they end
4. a tip, an app promotion or a sponsor offer: hold for the next digest
5. a thread that buzzed within the last few minutes: hold until the window passes
6. the hourly limit is reached: hold until there is room
7. otherwise: push now

Held notifications go out together, as one push, when `flush` finds them due.
"""
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from notify_router.model import DIGEST_KINDS, Notification, Settings
from notify_router.policy import in_quiet_hours, next_digest, quiet_release

HOUR = timedelta(hours=1)


@dataclass(frozen=True)
class Decision:
    action: str  # "push", "hold" or "inbox"
    reason: str
    release_at: datetime | None = None


@dataclass(frozen=True)
class Digest:
    at: datetime
    items: tuple = field(default_factory=tuple)

    def text(self):
        by_source = Counter(n.source for n in self.items)
        parts = [f"{count} from {source}" for source, count in sorted(by_source.items())]
        return f"{len(self.items)} updates: " + ", ".join(parts)


class Router:
    def __init__(self, store, settings=None):
        self.store = store
        self.settings = settings or Settings()

    def route(self, n: Notification) -> Decision:
        s = self.settings
        if n.urgent:
            return self._push(n, "urgent")
        if n.source in s.muted_sources:
            return Decision("inbox", f"{n.source} is muted by the attendee")
        if in_quiet_hours(n.created, s):
            return self._hold(n, quiet_release(n.created, s), "quiet hours")
        if n.kind in DIGEST_KINDS:
            return self._hold(n, next_digest(n.created, s), "waits for the digest")
        if n.thread is not None:
            last = self.store.last_thread_push(n.attendee_id, n.thread)
            window = timedelta(minutes=s.thread_window_minutes)
            if last is not None and n.created - last < window:
                return self._hold(n, last + window, "thread pushed recently")
        recent = self.store.push_times(n.attendee_id, n.created - HOUR)
        if len(recent) >= s.max_pushes_per_hour:
            # The window has room again once the oldest of the last `max` pushes is an hour old.
            return self._hold(n, recent[-s.max_pushes_per_hour] + HOUR, "hourly limit")
        return self._push(n, "sent")

    def flush(self, attendee, now) -> Digest | None:
        """Send every held notification that is due by `now` as one push."""
        due = self.store.pop_due(attendee, now)
        if not due:
            return None
        threads = sorted({n.thread for n in due if n.thread is not None})
        self.store.record_push(attendee, now, threads)
        return Digest(at=now, items=tuple(due))

    def _push(self, n, reason):
        self.store.record_push(n.attendee_id, n.created, [n.thread] if n.thread else [])
        return Decision("push", reason)

    def _hold(self, n, release_at, reason):
        if in_quiet_hours(release_at, self.settings):
            release_at = quiet_release(release_at, self.settings)
        self.store.hold(n, release_at)
        return Decision("hold", reason, release_at)
