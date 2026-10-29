# whova-notification-router

A small prototype of the part of an event app that decides when a phone should buzz. I wrote it after
reading the App Store reviews of the Whova app ([whova-review-study](https://github.com/muhammad-musa-ml/whova-review-study)):
of the 38 reviews rated one to three stars among the 500 newest, 14 are about notifications. In seven
of those the reviewer says the messages came from Whova itself (tips on using the app, promotion of the
app), two say they wanted the conference's notifications and not Whova's, and one got alerts at 5 AM and
on weekends. Whova's FAQ says attendees can already turn each notification type for the event, the
community board and topics on or off; this prototype is about what that leaves out: messages from the
platform, the time of day, and bursts. I am not affiliated with Whova, and nothing here touches its
systems.

## What it decides

Every notification still lands in the attendee's in-app inbox. The router only decides whether the
phone buzzes now, later, or not at all. The first rule that matches wins:

| rule | outcome |
|---|---|
| urgent, and only the organizer can mark one (a room change, a cancellation) | push now, even at night |
| from a source the attendee turned off (Whova, sponsors or other attendees; never the organizer) | inbox only |
| during quiet hours, 22:00 to 07:00 on the event's clock by default | hold until 07:00 |
| a tip on using the app, an app promotion or a sponsor offer | hold for the next digest, 12:30 or 18:30 |
| a chat thread that buzzed less than 10 minutes ago | hold until the 10 minutes are up |
| six pushes already in the last hour | hold until the hour has room |
| anything else | push now |

Held notifications go out together as one push when they fall due ("3 updates: 2 from organizer,
1 from whova"). The code is [notify_router/router.py](notify_router/router.py); the times and limits are
settings in [notify_router/model.py](notify_router/model.py).

## A simulated conference

`scripts/simulate.py` replays a made-up two-day conference (500 attendees, Los Angeles, 10-11 November
2026) twice: once pushing every notification the moment it is created, once through the router. The
stream is my assumption, written at the top of the script: 8 organizer announcements a day, 2 room
changes over the two days, 6 session reminders per attendee per day, 4 Whova messages between 05:00 and 22:30, 3 sponsor
offers, and up to 3 chat conversations of 1 to 6 messages each. So the numbers below describe those
assumptions, not Whova's traffic; what carries over is what gets held and for how long.

| per attendee | push everything | router | router, Whova turned off |
|---|---|---|---|
| pushes per day, mean | 27.2 | 20.5 | 19.9 |
| pushes per day, most for one attendee | 40 | 28 | 27 |
| pushes in the busiest hour | 14 | 8 | 8 |
| pushes between 22:00 and 07:00 (all attendees) | 947 | 0 | 0 |
| room-change delay, worst | 0 min | 0 min | 0 min |
| announcement delay, median / 90th percentile | 0 / 0 min | 0 / 0 min | 0 / 0 min |
| chat message delay, median / 90th percentile | 0 / 0 min | 4 / 9 min | 4 / 9 min |

The busiest hour can pass the limit of six because urgent pushes and digests do not count against it.
Turning Whova's messages off saves little more once they already wait for the digest; it matters for
people who want none at all. The run fails if any notification is not pushed, sent in a digest or left
in the inbox, or if anything is still on hold at the end.

## Design notes

- **Quiet hours are wall-clock times in an IANA zone,** so 07:00 moves with daylight saving. The tests
  pin both 2026 changes in Los Angeles, including a wall time that does not exist on the spring-forward
  night.
- **State lives in Redis** so several router processes can share it: per attendee, a sorted set of push
  times, a hash of the last push in each thread, and a sorted set of held notifications keyed by release
  time. Scores are integer microseconds since the epoch, which a Redis double holds exactly. Releasing a
  digest reads and removes the due items in one MULTI/EXEC, so two processes cannot send the same item.
- **The same tests run against an in-memory store and against Redis** (fakeredis locally; a real Redis 7
  service in CI).
- Not built: delivery to APNs or FCM, a per-attendee zone for virtual events (the zone is one setting),
  and the in-app inbox itself.

## How to run it

```
python -m venv .venv
.venv/Scripts/activate        # on macOS or Linux: source .venv/bin/activate
pip install -r requirements.txt
python -m pytest
python scripts/simulate.py    # writes results/simulation.json
python scripts/simulate.py --redis redis://localhost:6379/15   # same run on a scratch Redis database (it is emptied)
```
