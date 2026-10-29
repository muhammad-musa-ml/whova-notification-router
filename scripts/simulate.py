"""Replay a synthetic two-day conference through the router and compare it with pushing everything at once.

The stream is made up, so the numbers describe these assumptions and nothing about Whova's real
traffic; what carries over is the shape: what gets held, for how long, and that nothing is lost.

Writes results/simulation.json and prints a table.

  python scripts/simulate.py                      # in-memory store
  python scripts/simulate.py --redis redis://localhost:6379/15   # the same run against a real Redis
"""
import argparse
import heapq
import json
import random
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from notify_router.model import Notification, Settings  # noqa: E402
from notify_router.policy import in_quiet_hours  # noqa: E402
from notify_router.router import Router  # noqa: E402
from notify_router.store import MemoryStore, RedisStore  # noqa: E402

ZONE = "America/Los_Angeles"
DAYS = (date(2026, 11, 10), date(2026, 11, 11))
ATTENDEES = 500
SEED = 7

# Assumptions behind the stream, per attendee per day unless it says otherwise.
ANNOUNCEMENTS_PER_DAY = 8           # organizer, to everyone, 07:00-21:30
ROOM_CHANGES_PER_EVENT = 2          # organizer, urgent, to everyone, 09:00-16:00
SESSION_SLOTS = [time(9, 0), time(9, 45), time(10, 30), time(11, 15), time(13, 0), time(13, 45),
                 time(14, 30), time(15, 15), time(16, 0), time(16, 45)]
SESSIONS_PER_DAY = 6                # a reminder 10 minutes before each
WHOVA_MESSAGES_PER_DAY = 4          # tips and app promotion, 05:00-22:30
SPONSOR_OFFERS_PER_DAY = 3          # 08:00-18:00
CONVERSATIONS_PER_DAY = (0, 3)      # uniform; each 1-6 messages, 1-5 minutes apart, starting 08:00-23:00


def at_local(day, minutes):
    return datetime.combine(day, time(0, 0), tzinfo=ZoneInfo(ZONE)) + timedelta(minutes=minutes)


def utc(dt):
    return dt.astimezone(timezone.utc)


def build_stream(rng):
    stream = []
    seq = 0

    def add(attendee, source, kind, when, urgent=False, thread=None):
        nonlocal seq
        seq += 1
        stream.append(Notification(f"n{seq}", attendee, source, kind, utc(when), urgent, thread))

    people = [f"a{i:03d}" for i in range(ATTENDEES)]
    room_change_days = [rng.choice(DAYS) for _ in range(ROOM_CHANGES_PER_EVENT)]
    for day in DAYS:
        broadcasts = [at_local(day, rng.randint(7 * 60, 21 * 60 + 30)) for _ in range(ANNOUNCEMENTS_PER_DAY)]
        changes = [at_local(day, rng.randint(9 * 60, 16 * 60)) for d in room_change_days if d == day]
        for p in people:
            for when in broadcasts:
                add(p, "organizer", "announcement", when)
            for when in changes:
                add(p, "organizer", "room_change", when, urgent=True)
            for slot in rng.sample(SESSION_SLOTS, SESSIONS_PER_DAY):
                add(p, "organizer", "session_reminder",
                    at_local(day, slot.hour * 60 + slot.minute - 10))
            for _ in range(WHOVA_MESSAGES_PER_DAY):
                add(p, "whova", rng.choice(["app_tip", "app_promotion"]),
                    at_local(day, rng.randint(5 * 60, 22 * 60 + 30)))
            for _ in range(SPONSOR_OFFERS_PER_DAY):
                add(p, "sponsor", "sponsor_offer", at_local(day, rng.randint(8 * 60, 18 * 60)))
            for c in range(rng.randint(*CONVERSATIONS_PER_DAY)):
                start = rng.randint(8 * 60, 23 * 60)
                for _ in range(rng.randint(1, 6)):
                    add(p, "attendee", "dm", at_local(day, start), thread=f"{p}-{day}-{c}")
                    start += rng.randint(1, 5)
    return sorted(stream, key=lambda n: (n.created, n.id))


def run_router(stream, settings, store):
    """Feed the stream through the router in time order, flushing each hold when it falls due."""
    router = Router(store, settings)
    queue = [(n.created, i, "notify", n) for i, n in enumerate(stream)]
    heapq.heapify(queue)
    seq = len(queue)
    pushes = []        # (attendee, instant, items in the push)
    delays = defaultdict(list)
    actions = Counter()
    while queue:
        at, _, what, payload = heapq.heappop(queue)
        if what == "notify":
            decision = router.route(payload)
            actions[decision.action] += 1
            if decision.action == "push":
                pushes.append((payload.attendee_id, at, 1))
                delays[payload.kind].append(0.0)
            elif decision.action == "hold":
                seq += 1
                heapq.heappush(queue, (decision.release_at, seq, "flush", payload.attendee_id))
        else:
            digest = router.flush(payload, at)
            if digest:
                pushes.append((payload, at, len(digest.items)))
                for n in digest.items:
                    delays[n.kind].append((at - n.created).total_seconds() / 60)
    left = sum(store.held_count(a) for a in {n.attendee_id for n in stream})
    return pushes, delays, actions, left


def baseline(stream):
    pushes = [(n.attendee_id, n.created, 1) for n in stream]
    delays = defaultdict(list)
    for n in stream:
        delays[n.kind].append(0.0)
    return pushes, delays, Counter(push=len(stream)), 0


def summarize(name, stream, settings, result):
    pushes, delays, actions, left = result
    delivered = sum(k for _, _, k in pushes) + actions["inbox"]
    if delivered + left != len(stream) or left:
        raise AssertionError(f"{name}: {len(stream)} notifications in, {delivered} accounted for, {left} still held")
    tz = ZoneInfo(settings.zone)
    per_day = Counter((a, t.astimezone(tz).date()) for a, t, _ in pushes)
    per_hour = Counter((a, t.astimezone(tz).replace(minute=0, second=0, microsecond=0)) for a, t, _ in pushes)
    counts = [per_day.get((a, d), 0) for a in {n.attendee_id for n in stream} for d in DAYS]

    def minutes(kind, q):
        values = sorted(delays.get(kind, []))
        return round(values[min(len(values) - 1, int(q * len(values)))], 1) if values else None

    return {
        "pushes_per_attendee_per_day_mean": round(statistics.mean(counts), 1),
        "pushes_per_attendee_per_day_max": max(counts),
        "busiest_hour_max_pushes": max(per_hour.values()),
        "pushes_in_quiet_hours": sum(1 for _, t, _ in pushes if in_quiet_hours(t, settings)),
        "urgent_delay_minutes_max": max(delays.get("room_change", [0.0])),
        "announcement_delay_minutes_median": minutes("announcement", 0.5),
        "announcement_delay_minutes_p90": minutes("announcement", 0.9),
        "dm_delay_minutes_median": minutes("dm", 0.5),
        "dm_delay_minutes_p90": minutes("dm", 0.9),
        "inbox_only": actions["inbox"],
        "notifications": len(stream),
    }


def make_store(url):
    if url is None:
        return MemoryStore()
    import redis
    client = redis.Redis.from_url(url)
    client.flushdb()  # point --redis at a scratch database: this empties it
    return RedisStore(client)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--redis", help="a scratch Redis database URL; it is emptied first")
    args = parser.parse_args()

    stream = build_stream(random.Random(SEED))
    settings = Settings(zone=ZONE)
    muted = replace(settings, muted_sources=frozenset({"whova"}))
    rows = {
        "push everything": summarize("baseline", stream, settings, baseline(stream)),
        "router": summarize("router", stream, settings, run_router(stream, settings, make_store(args.redis))),
        "router, Whova muted": summarize("muted", stream, muted, run_router(stream, muted, make_store(args.redis))),
    }
    out = {
        "store": "redis" if args.redis else "memory",
        "attendees": ATTENDEES, "days": [d.isoformat() for d in DAYS], "zone": ZONE, "seed": SEED,
        "settings": {"quiet_hours": f"{settings.quiet_start:%H:%M}-{settings.quiet_end:%H:%M}",
                     "digests": [f"{t:%H:%M}" for t in settings.digest_times],
                     "max_pushes_per_hour": settings.max_pushes_per_hour,
                     "thread_window_minutes": settings.thread_window_minutes},
        "results": rows,
    }
    (ROOT / "results").mkdir(exist_ok=True)
    (ROOT / "results" / "simulation.json").write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")

    keys = list(next(iter(rows.values())))
    print(f"{'':42}" + "".join(f"{name:>22}" for name in rows))
    for k in keys:
        print(f"{k:42}" + "".join(f"{str(r[k]):>22}" for r in rows.values()))


if __name__ == "__main__":
    main()
