import random

from notify_router.model import Settings
from scripts import simulate


def test_every_notification_is_accounted_for_and_urgent_is_never_late():
    stream = [n for n in simulate.build_stream(random.Random(1)) if n.attendee_id in {"a000", "a001", "a002"}]
    settings = Settings(zone=simulate.ZONE)
    result = simulate.run_router(stream, settings, simulate.MemoryStore())
    row = simulate.summarize("router", stream, settings, result)  # raises if anything is lost or still held
    assert row["urgent_delay_minutes_max"] == 0.0
    assert row["pushes_in_quiet_hours"] == 0
