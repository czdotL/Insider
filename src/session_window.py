import os
from datetime import datetime, time as dtime

STATE_DIR = os.getenv("BOT_STATE_DIR", "state")
PAUSE_FLAG = os.path.join(STATE_DIR, "paused")

# Weekly broker session: Monday 14:30 -> Friday 22:30 (local time).
# Nights in between stay connected; the weekend is left free for the mobile app.
WEEK_OPEN = dtime(14, 30)
WEEK_CLOSE = dtime(22, 30)


def is_paused() -> bool:
    return os.path.exists(PAUSE_FLAG)


def set_paused(paused: bool) -> None:
    if paused:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(PAUSE_FLAG, "w") as f:
            f.write(datetime.now().isoformat())
    elif os.path.exists(PAUSE_FLAG):
        os.remove(PAUSE_FLAG)


def in_weekly_window(now: datetime = None) -> bool:
    now = now or datetime.now()
    day, t = now.weekday(), now.time()
    if day >= 5:
        return False
    if day == 0:
        return t >= WEEK_OPEN
    if day == 4:
        return t <= WEEK_CLOSE
    return True


def is_attended_hours(now: datetime = None) -> bool:
    """Weekday afternoon/evening when the user is awake to approve a 2FA push."""
    now = now or datetime.now()
    return now.weekday() < 5 and WEEK_OPEN <= now.time() <= WEEK_CLOSE


def should_be_connected(now: datetime = None) -> bool:
    return in_weekly_window(now) and not is_paused()
