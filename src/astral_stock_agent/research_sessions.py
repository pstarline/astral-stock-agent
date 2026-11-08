"""Compare research bar labels with scheduled, closed NYSE sessions.

The caller must establish the provider's label convention independently. This
checks scheduled coverage under that convention, not provider authenticity,
symbol-specific halts/listing history, corporate actions or tradable fills.
"""
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .research_data import DailyBar, DailyHistory, MAX_BARS, ResearchDataError
from .sessions import FIRST_DAY, LAST_DAY, SessionError, session_on


UTC_SESSION_DATE = 'session_date_at_utc_midnight'
NY_SESSION_DATE = 'session_date_at_new_york_midnight'


def daily_session_coverage(history, *, first_day, last_day, now, label_convention):
    """Compare an inclusive date range without discarding or filling any bar.

    UTC-midnight and New-York-midnight session-date labels are supported only
    under an explicit convention. UTC labels are NOT shifted to New York dates;
    New York labels must be midnight there, accounting for DST. Neither is
    evidence of executable midnight fills. There is no implicit convention
    default or inferred date shift. Unclosed sessions and
    unsupported calendar ranges fail, rather than silently shortening coverage.
    """
    if label_convention not in (UTC_SESSION_DATE, NY_SESSION_DATE):
        raise ResearchDataError('explicit supported daily label convention required')
    if (type(first_day) is not date or type(last_day) is not date
            or not FIRST_DAY <= first_day <= last_day <= LAST_DAY):
        raise ResearchDataError('coverage range outside verified calendar')
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ResearchDataError('timezone-aware coverage observation required')
    if (not isinstance(history, DailyHistory) or type(history.bars) is not tuple
            or not 1 <= len(history.bars) <= MAX_BARS):
        raise ResearchDataError('nonempty parsed daily history required')
    expected = set()
    try:
        for offset in range((last_day - first_day).days + 1):
            day = first_day + timedelta(days=offset)
            session = session_on(day)
            if session is not None:
                if session.closes_at > now:
                    raise ResearchDataError('coverage includes an unclosed session')
                expected.add(day)
    except SessionError:
        raise ResearchDataError('scheduled calendar unavailable') from None
    if not expected:
        raise ResearchDataError('coverage range has no scheduled sessions')
    label_zone = timezone.utc if label_convention == UTC_SESSION_DATE else ZoneInfo('America/New_York')
    observed = set()
    previous = None
    for bar in history.bars:
        if not isinstance(bar, DailyBar) or not isinstance(bar.timestamp, datetime):
            raise ResearchDataError('invalid daily bar label')
        stamp = bar.timestamp
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ResearchDataError('timezone-aware daily label required')
        stamp = stamp.astimezone(timezone.utc)
        label = stamp.astimezone(label_zone)
        if label.time() != time(0) or (previous is not None and stamp <= previous):
            raise ResearchDataError('daily labels must be ordered midnights under explicit convention')
        observed.add(label.date())
        previous = stamp
    missing = sorted(expected - observed)
    unexpected = sorted(observed - expected)
    return {
        'symbol': history.symbol,
        'label_convention': label_convention,
        'scheduled_coverage_complete': not missing and not unexpected,
        'scheduled_calendar_checked': True,
        'scheduled_session_count': len(expected),
        'missing_session_dates': [day.isoformat() for day in missing],
        'unexpected_bar_dates': [day.isoformat() for day in unexpected],
        'provider_label_semantics_verified': False,
        'corporate_actions_verified': False,
        'deployment_approved': False,
    }


def require_daily_session_coverage(history, **kwargs):
    report = daily_session_coverage(history, **kwargs)
    if not report['scheduled_coverage_complete']:
        raise ResearchDataError('daily history does not match scheduled sessions')
    return report
