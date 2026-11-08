"""Synthetic observations: no real prices, account IDs, or operational calls."""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from decimal import Decimal

import pytest

from astral_stock_agent.research_data import DailyBar, DailyHistory, ResearchDataError, alignment_report
from astral_stock_agent.research_sessions import (
    UTC_SESSION_DATE, NY_SESSION_DATE, daily_session_coverage, require_daily_session_coverage,
)
from astral_stock_agent.sessions import SessionError


def history(*days):
    bars = tuple(DailyBar(datetime.fromisoformat(d + 'T00:00:00+00:00'),
                          *(Decimal(x) for x in ('10', '12', '9', '11', '100')))
                 for d in days)
    return DailyHistory('SPY', bars, 'synthetic-checksum')


def coverage(h, first='2026-11-02', last='2026-11-06', **overrides):
    args = dict(first_day=date.fromisoformat(first), last_day=date.fromisoformat(last),
                now=datetime(2026, 12, 1, tzinfo=timezone.utc), label_convention=UTC_SESSION_DATE)
    args.update(overrides)
    return daily_session_coverage(h, **args)


FULL = ['2026-11-02', '2026-11-03', '2026-11-04', '2026-11-05', '2026-11-06']


def test_utc_midnight_session_labels_are_not_shifted_to_previous_ny_day():
    result = coverage(history(*FULL))
    assert result['scheduled_coverage_complete']
    assert result['scheduled_session_count'] == 5
    assert not result['missing_session_dates'] and not result['unexpected_bar_dates']
    for key in ['provider_label_semantics_verified', 'corporate_actions_verified', 'deployment_approved']:
        assert result[key] is False


def test_shared_missing_day_is_found_even_when_cross_symbol_alignment_passes():
    first = history(*[d for d in FULL if d != '2026-11-04'])
    second = replace(first, symbol='IEF')
    assert alignment_report([first, second])['aligned']
    for h in [first, second]:
        r = coverage(h)
        assert not r['scheduled_coverage_complete']
        assert r['missing_session_dates'] == ['2026-11-04']


@pytest.mark.parametrize('extra', ['2026-11-01', '2026-11-07', '2026-11-09'])
def test_weekends_and_outside_range_not_dropped(extra):
    h = history(*sorted(FULL + [extra]))
    r = coverage(h)
    assert r['unexpected_bar_dates'] == [extra]
    assert not r['scheduled_coverage_complete']
    assert len(h.bars) == 6


def test_thanksgiving_and_early_close_exact_boundary():
    h = history('2026-11-25', '2026-11-27')
    r = coverage(h, first='2026-11-25', last='2026-11-29',
                 now=datetime(2026, 11, 27, 18, tzinfo=timezone.utc))
    assert r['scheduled_coverage_complete'] and r['scheduled_session_count'] == 2
    with pytest.raises(ResearchDataError, match='unclosed'):
        coverage(h, first='2026-11-25', last='2026-11-27',
                 now=datetime(2026, 11, 27, 17, 59, 59, tzinfo=timezone.utc))
    r = coverage(history('2026-11-25', '2026-11-26', '2026-11-27'),
                 first='2026-11-25', last='2026-11-27')
    assert r['unexpected_bar_dates'] == ['2026-11-26']


@pytest.mark.parametrize('days', [
    ['2026-03-06', '2026-03-09'], ['2026-10-30', '2026-11-02'],
])
def test_dst_changes_do_not_shift_labels(days):
    assert coverage(history(*days), first=days[0], last=days[-1])['scheduled_coverage_complete']


@pytest.mark.parametrize('shift', [timedelta(hours=5), timedelta(microseconds=1)])
def test_ambiguous_non_midnight_labels_rejected(shift):
    h = history(*FULL)
    h = replace(h, bars=(replace(h.bars[0], timestamp=h.bars[0].timestamp + shift),) + h.bars[1:])
    with pytest.raises(ResearchDataError, match='midnights'):
        coverage(h)


@pytest.mark.parametrize('convention', [None, 'new_york_date', '', 0])
def test_no_inferred_label_convention(convention):
    with pytest.raises(ResearchDataError, match='convention'):
        coverage(history(*FULL), label_convention=convention)


@pytest.mark.parametrize('first,last', [('2025-12-31', '2026-01-02'),
                                       ('2028-12-29', '2029-01-01'),
                                       ('2026-11-06', '2026-11-02')])
def test_invalid_or_unverified_ranges(first, last):
    with pytest.raises(ResearchDataError, match='range'):
        coverage(history(*FULL), first, last)


def test_all_weekend_range_rejected_instead_of_vacuous_success():
    with pytest.raises(ResearchDataError, match='no scheduled sessions'):
        coverage(history('2026-11-07'), first='2026-11-07', last='2026-11-08')


def test_calendar_failure_never_becomes_weekday_fallback(monkeypatch):
    import astral_stock_agent.research_sessions as module
    def fail(day):
        raise SessionError('private calendar error')
    monkeypatch.setattr(module, 'session_on', fail)
    with pytest.raises(ResearchDataError) as exc:
        coverage(history(*FULL))
    assert str(exc.value) == 'scheduled calendar unavailable'


@pytest.mark.parametrize('h', [None, DailyHistory('SPY', (), 'synthetic')])
def test_missing_history_rejected(h):
    with pytest.raises(ResearchDataError):
        coverage(h)


def test_naive_observation_and_naive_labels_rejected():
    h = history(*FULL)
    with pytest.raises(ResearchDataError):
        coverage(h, now=datetime(2026, 12, 1))
    h = replace(h, bars=(replace(h.bars[0], timestamp=h.bars[0].timestamp.replace(tzinfo=None)),))
    with pytest.raises(ResearchDataError):
        coverage(h)


@pytest.mark.parametrize('reorder', [lambda b: b[::-1], lambda b: (b[0],) + b])
def test_order_and_duplicate_labels_rechecked(reorder):
    h = history(*FULL)
    with pytest.raises(ResearchDataError):
        coverage(replace(h, bars=reorder(h.bars)))


def test_require_wrapper_blocks_missing_sessions():
    kwargs = dict(first_day=date(2026, 11, 2), last_day=date(2026, 11, 6),
                  now=datetime(2026, 12, 1, tzinfo=timezone.utc), label_convention=UTC_SESSION_DATE)
    assert require_daily_session_coverage(history(*FULL), **kwargs)['scheduled_coverage_complete']
    with pytest.raises(ResearchDataError, match='does not match'):
        require_daily_session_coverage(history(FULL[0]), **kwargs)


@pytest.mark.parametrize('days,utc_hours', [
    (['2026-03-06', '2026-03-09'], [5, 4]),
    (['2026-10-30', '2026-11-02'], [4, 5]),
])
def test_new_york_midnight_convention_handles_dst(days, utc_hours):
    h = history(*days)
    bars = tuple(replace(b, timestamp=b.timestamp.replace(tzinfo=ZoneInfo('America/New_York')).astimezone(timezone.utc))
                 for b in h.bars)
    assert [b.timestamp.hour for b in bars] == utc_hours
    h = replace(h, bars=bars)
    assert coverage(h, first=days[0], last=days[-1], label_convention=NY_SESSION_DATE)['scheduled_coverage_complete']
    with pytest.raises(ResearchDataError):
        coverage(h, first=days[0], last=days[-1], label_convention=UTC_SESSION_DATE)


def test_utc_midnight_cannot_be_silently_reinterpreted_as_new_york_midnight():
    with pytest.raises(ResearchDataError):
        coverage(history(*FULL), label_convention=NY_SESSION_DATE)
