"""Per-attendee state: recent pushes, the last push in each thread, and notifications on hold.

MemoryStore is for tests and the simulation; RedisStore keeps the same state in Redis so several
router processes can share it. Times are kept as integer microseconds since the epoch (UTC): a Redis
score is a double, which holds these exactly, while float seconds would round.
"""
import json
import uuid
from datetime import datetime, timedelta, timezone

from notify_router.model import Notification

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
KEEP_PUSHES = timedelta(hours=2)  # longer than any window the router asks about


def to_us(at):
    return (at - EPOCH) // timedelta(microseconds=1)


def from_us(us):
    return EPOCH + timedelta(microseconds=int(us))


def dump(n):
    return json.dumps({
        "id": n.id, "attendee_id": n.attendee_id, "source": n.source, "kind": n.kind,
        "created": n.created.isoformat(), "urgent": n.urgent, "thread": n.thread,
    }, sort_keys=True)


def load(text):
    d = json.loads(text)
    d["created"] = datetime.fromisoformat(d["created"])
    return Notification(**d)


class MemoryStore:
    def __init__(self):
        self.pushes = {}
        self.threads = {}
        self.held = {}

    def record_push(self, attendee, at, threads=()):
        self.pushes.setdefault(attendee, []).append(at)
        for thread in threads:
            self.threads[(attendee, thread)] = at

    def push_times(self, attendee, since):
        """Push instants after `since`, oldest first."""
        return sorted(t for t in self.pushes.get(attendee, []) if t > since)

    def last_thread_push(self, attendee, thread):
        return self.threads.get((attendee, thread))

    def hold(self, notification, release_at):
        self.held.setdefault(notification.attendee_id, []).append((release_at, notification))

    def pop_due(self, attendee, now):
        """Remove and return the held notifications released at or before `now`, oldest first."""
        held = self.held.get(attendee, [])
        due = [n for r, n in held if r <= now]
        self.held[attendee] = [(r, n) for r, n in held if r > now]
        return sorted(due, key=lambda n: (n.created, n.id))

    def held_count(self, attendee):
        return len(self.held.get(attendee, []))


class RedisStore:
    def __init__(self, client, prefix="nr"):
        self.r = client
        self.prefix = prefix

    def _key(self, kind, attendee):
        return f"{self.prefix}:{kind}:{attendee}"

    def record_push(self, attendee, at, threads=()):
        pushes = self._key("pushes", attendee)
        pipe = self.r.pipeline(transaction=True)
        pipe.zadd(pushes, {uuid.uuid4().hex: to_us(at)})
        pipe.zremrangebyscore(pushes, "-inf", to_us(at - KEEP_PUSHES))
        pipe.expire(pushes, KEEP_PUSHES)
        for thread in threads:
            pipe.hset(self._key("threads", attendee), thread, to_us(at))
        pipe.execute()

    def push_times(self, attendee, since):
        scores = self.r.zrangebyscore(self._key("pushes", attendee), f"({to_us(since)}", "+inf", withscores=True)
        return [from_us(score) for _, score in scores]

    def last_thread_push(self, attendee, thread):
        value = self.r.hget(self._key("threads", attendee), thread)
        return None if value is None else from_us(value)

    def hold(self, notification, release_at):
        self.r.zadd(self._key("held", notification.attendee_id), {dump(notification): to_us(release_at)})

    def pop_due(self, attendee, now):
        held = self._key("held", attendee)
        pipe = self.r.pipeline(transaction=True)  # MULTI/EXEC: no other router can take the same items
        pipe.zrangebyscore(held, "-inf", to_us(now))
        pipe.zremrangebyscore(held, "-inf", to_us(now))
        members, _ = pipe.execute()
        due = [load(m) for m in members]
        return sorted(due, key=lambda n: (n.created, n.id))

    def held_count(self, attendee):
        return self.r.zcard(self._key("held", attendee))
