"""Scheduled NYSE core cash-equity sessions, verified for 2026-2028.

Calendar times do not establish real-time exchange availability, symbol halts,
fresh market data, complete accounting, or finality of fills.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

import exchange_calendars


FIRST_DAY = date(2026, 1, 1)
LAST_DAY = date(2028, 12, 31)


class SessionError(ValueError):
    pass


@dataclass(frozen=True)
class Session:
    day: date
    opens_at: datetime
    closes_at: datetime


@lru_cache(maxsize=1)
def _calendar():
    # Padding keeps Jan 1 / Dec 31 queries inside the library's session bounds.
    return exchange_calendars.get_calendar("XNYS", start="2025-12-01", end="2029-01-31")


def session_on(day):
    """Return a scheduled core session, None for holiday/weekend; fail outside range."""
    if type(day) is not date or not FIRST_DAY <= day <= LAST_DAY:
        raise SessionError("session date outside verified 2026-2028 range")
    try:
        calendar = _calendar()
        if not calendar.is_session(day.isoformat()):
            return None
        return Session(day, calendar.session_open(day.isoformat()).to_pydatetime(),
                       calendar.session_close(day.isoformat()).to_pydatetime())
    except Exception:
        raise SessionError("NYSE calendar unavailable") from None


def require_closed_session(day, now):
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise SessionError("timezone-aware observation required")
    session = session_on(day)
    if session is None:
        raise SessionError("NYSE holiday or weekend")
    if now < session.closes_at:
        raise SessionError("NYSE session has not closed")
    return session


def latest_closed_session(now):
    """Select by New York date, including weekends, holidays and close boundaries."""
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise SessionError("timezone-aware observation required")
    day = now.astimezone(ZoneInfo("America/New_York")).date()
    if not FIRST_DAY <= day <= LAST_DAY:
        raise SessionError("observation date outside verified range")
    for offset in range(15):
        candidate = day - timedelta(days=offset)
        if candidate < FIRST_DAY:
            break
        session = session_on(candidate)
        if session and session.closes_at <= now:
            return session
    raise SessionError("no closed session in verified lookback")
