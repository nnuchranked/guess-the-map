"""When does the next map appear?"""
import time
from datetime import datetime, timezone

import config

UTC = timezone.utc

# The schedule is a fixed grid: MAP_INTERVAL_HOURS counted from this UTC anchor.
_ANCHOR_DAY = datetime(2026, 1, 1, tzinfo=UTC)


def next_reset(now=None):
    """Return the Unix timestamp of the next scheduled map change."""
    now = time.time() if now is None else now
    anchor = _ANCHOR_DAY.replace(hour=config.DAILY_RESET_HOUR_UTC).timestamp()
    step = round(config.MAP_INTERVAL_HOURS * 3600)
    n = int((now - anchor) // step) + 1
    return int(anchor + n * step)
