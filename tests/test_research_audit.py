"""Synthetic hand calculations, never private market data or actual fills."""
import copy
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal, localcontext

import pytest

from astral_stock_agent.research_audit import compare_backtest_curve
from astral_stock_agent.research_data import DailyBar, DailyHistory, ResearchDataError
from astral_stock_agent.research_execution import simulate_next_open
from astral_stock_agent.research_sessions import UTC_SESSION_DATE


def inputs():
    history = DailyHistory('SPY', tuple(
        DailyBar(datetime(2026, 11, 2+i, tzinfo=timezone.utc), Decimal(op),
                 Decimal(20), Decimal(1), Decimal(close), Decimal(100))
        for i, (op, close) in enumerate([(10, 10), (12, 14), (8, 9)])), 'synthetic')
    simulation = simulate_next_open(history, (1, -1, 0), initial_cash=100,
        commission_bps=0, slippage_bps=0, first_day=date(2026, 11, 2), last_day=date(2026, 11, 4),
        now=datetime(2026, 11, 5, tzinfo=timezone.utc), label_convention=UTC_SESSION_DATE)
    # Hand calculation: day 1 cash 100; day 2 buy at 12, mark at 14 -> 102;
    # day 3 sell at 8 -> 96. No booking of tomorrow's open into today's close.
    response = {'backtest_id': 'synthetic-run', 'strategy_id': 7,
        'backtest_status': 'succeeded', 'health_state': 'ok', 'degraded': False,
        'results': {'health_state': 'ok', 'degraded': False,
            'portfolio_value_downsampled': False,
            'portfolio_value': {str(b.timestamp): v for b, v in zip(history.bars, [100, 102, 96])},
            'backtest_period': {'bar_size': '1d', 'history_capped': False,
                'raw_data_start': str(history.bars[0].timestamp),
                'raw_data_end': str(history.bars[-1].timestamp),
                'first_eligible_trade_at': str(history.bars[0].timestamp)}}}
    return history, simulation, response


def audit(history=None, simulation=None, response=None, **kwargs):
    h, s, r = inputs()
    return compare_backtest_curve(history or h, simulation or s, response if response is not None else r,
        expected_backtest_id=kwargs.pop('expected_backtest_id', 'synthetic-run'),
        expected_strategy_id=kwargs.pop('expected_strategy_id', 7),
        first_compared_index=kwargs.pop('first_compared_index', 0), **kwargs)


def test_full_curve_equality_never_grants_parity_or_execution_authority():
    h, s, r = inputs()
    before = copy.deepcopy(r)
    a = audit(response={'structuredContent': r})
    assert a.curves_match and a.compared_points == 3 and a.mismatched_points == 0
    assert a.max_absolute_gap_usd == 0
    assert not a.deployment_approved and not a.actual_execution_verified and not a.full_strategy_parity_verified
    assert r == before and len(s.fills) == 2 and h.symbol == 'SPY'


def test_equal_final_result_cannot_hide_a_fill_booked_before_execution():
    _, _, r = inputs()
    keys = list(r['results']['portfolio_value'])
    r['results']['portfolio_value'][keys[0]] = 98  # next open=12 booked at today's close=10
    a = audit(response=r)
    assert not a.curves_match and a.mismatched_points == 1 and a.max_absolute_gap_usd == 2


def test_final_close_fallback_trade_is_detected_without_inventing_a_local_fill():
    _, _, r = inputs()
    r['results']['portfolio_value'][list(r['results']['portfolio_value'])[-1]] = '95.99'
    assert audit(response=r).mismatched_points == 1


def test_explicit_post_warmup_subset_must_retain_every_remaining_label():
    h, _, r = inputs()
    del r['results']['portfolio_value'][str(h.bars[0].timestamp)]
    r['results']['backtest_period']['first_eligible_trade_at'] = str(h.bars[1].timestamp)
    assert audit(response=r, first_compared_index=1).compared_points == 2
    with pytest.raises(ResearchDataError):
        audit(response=r)


@pytest.mark.parametrize('mutation', ['missing', 'extra', 'shift', 'duplicate_instant'])
def test_partial_shifted_or_duplicate_dates_are_rejected(mutation):
    _, _, r = inputs()
    curve = r['results']['portfolio_value']; key = list(curve)[0]
    if mutation == 'missing': del curve[key]
    elif mutation == 'extra': curve['2026-11-05T00:00:00Z'] = 96
    elif mutation == 'shift': curve['2026-11-05T00:00:00Z'] = curve.pop(key)
    else:
        del curve[list(curve)[-1]]
        curve['2026-11-02T00:00:00Z'] = 100
    with pytest.raises(ResearchDataError): audit(response=r)


@pytest.mark.parametrize('path,value', [
    (('backtest_id',), 'private-wrong-run'), (('strategy_id',), True),
    (('backtest_status',), 'running'), (('degraded',), 0), (('health_state',), 'degraded'),
    (('results_truncated',), True), (('response_truncated',), True),
    (('results', 'portfolio_value_downsampled'), 0), (('results', 'portfolio_value_downsampled'), True),
    (('results', 'degraded'), True), (('results', 'health_state'), 'unknown'),
    (('results', 'backtest_period', 'history_capped'), True),
    (('results', 'backtest_period', 'bar_size'), '15m'),
    (('results', 'backtest_period', 'raw_data_start'), '2026-11-03T00:00:00Z'),
])
def test_unhealthy_or_wrong_scope_results_fail_with_sanitized_errors(path, value):
    _, _, r = inputs(); target = r
    for part in path[:-1]: target = target[part]
    target[path[-1]] = value
    with pytest.raises(ResearchDataError) as exc: audit(response=r)
    assert str(exc.value) == 'complete matching-scope backtest curve required'


@pytest.mark.parametrize('value', [True, None, 'NaN', float('inf'), 0, -1, '1e100'])
def test_invalid_equity_cannot_match_or_poison_the_gap_calculation(value):
    _, _, r = inputs(); curve = r['results']['portfolio_value']
    curve[list(curve)[0]] = value
    with pytest.raises(ResearchDataError): audit(response=r)


def test_serialization_tolerance_does_not_depend_on_ambient_decimal_precision():
    _, _, r = inputs(); curve = r['results']['portfolio_value']; key = list(curve)[0]
    curve[key] = '100.00000005'
    with localcontext() as ctx:
        ctx.prec = 4
        assert audit(response=r).curves_match
    curve[key] = '100.0000002'
    assert not audit(response=r).curves_match


def test_reordered_local_marks_and_unverified_boundary_are_rejected():
    _, s, _ = inputs()
    with pytest.raises(ResearchDataError): audit(simulation=replace(s, daily_marks=s.daily_marks[::-1]))
    with pytest.raises(ResearchDataError): audit(first_compared_index=True)
    with pytest.raises(ResearchDataError): audit(response={'isError': True})
