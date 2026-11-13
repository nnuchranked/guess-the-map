"""When does the next daily map appear?"""
import time
from datetime import UTC, datetime, timedelta

import config


def next_reset(now=None):
    """Unix time of the next daily reset."""
    now = time.time() if now is None else now
    dt = datetime.fromtimestamp(now, UTC)
    reset = dt.replace(hour=config.DAILY_RESET_HOUR_UTC, minute=0, second=0, microsecond=0)
    if reset <= dt:
        reset += timedelta(days=1)
    return int(reset.timestamp())