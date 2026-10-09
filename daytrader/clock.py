"""US market time helpers. All timestamps are Unix seconds; the exchange clock is New York time."""
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")  # needs the tzdata package on Windows
OPEN = dtime(9, 30)
CLOSE = dtime(16, 0)


def parse_hhmm(s):
    h, m = str(s).strip().split(":")
    return dtime(int(h), int(m))


def at(day, hhmm):
    """Unix time of a New York wall-clock time on `day` (date or 'YYYY-MM-DD')."""
    if isinstance(day, str):
        day = date.fromisoformat(day)
    t = hhmm if isinstance(hhmm, dtime) else parse_hhmm(hhmm)
    return int(datetime.combine(day, t, ET).timestamp())


def to_et(ts):
    return datetime.fromtimestamp(ts, ET)


def day_of(ts):
    return to_et(ts).date().isoformat()


def hhmm(ts):
    return to_et(ts).strftime("%H:%M")


def now_ts():
    return int(datetime.now(ET).timestamp())


def today_et():
    return datetime.now(ET).date().isoformat()


def weekday_name(day):
    return date.fromisoformat(day).strftime("%A")


def prev_weekday(day, n=1):
    d = date.fromisoformat(day)
    while n:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d.isoformat()
