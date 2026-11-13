"""When does the next map appear?"""
import time
from datetime import UTC, datetime

import config

# The schedule is a fixed grid: every MAP_INTERVAL_HOURS, counted from this day at DAILY_RESET_HOUR_UTC.
_ANCHOR_DAY = datetime(2026, 1, 1, tzinfo=UTC)


def next_reset(now=None):
    """Unix time of the next map change."""
    now = time.time() if now is None else now
    anchor = _ANCHOR_DAY.replace(hour=config.DAILY_RESET_HOUR_UTC).timestamp()
    step = round(config.MAP_INTERVAL_HOURS * 3600)
    n = int((now - anchor) // step) + 1
    return int(anchor + n * step)