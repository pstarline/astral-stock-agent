"""Synthetic signal scenarios and prefix invariance, no actual strategy returns."""
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal, localcontext

import pytest

from astral_stock_agent.research_data import DailyBar, DailyHistory, ResearchDataError
from astral_stock_agent.research_trend import sma_close_decisions, compare_sma_candidate
from astral_stock_agent.research_sessions import UTC_SESSION_DATE


def history():
    bars = tuple(DailyBar(datetime(2026, 1, d, tzinfo=timezone.utc),
                          Decimal(c), Decimal(20), Decimal(1), Decimal(c), Decimal(100))
                 for d, c in [(2, 10), (5, 12), (6, 14), (7, 8), (8, 7), (9, 10)])
    return DailyHistory('SPY', bars, 'synthetic')


def decisions(h=None, **overrides):
    args = dict(window=3, units='1', trade_from=date(2026, 1, 2), label_convention=UTC_SESSION_DATE)
    args.update(overrides)
    return sma_close_decisions(h or history(), **args)


def compare(**overrides):
    args = dict(window=3, units='1', trade_from=date(2026, 1, 2), initial_cash='100',
                commission_bps=0, slippage_bps=0, first_day=date(2026, 1, 2),
                last_day=date(2026, 1, 9), now=datetime(2026, 1, 10, tzinfo=timezone.utc),
                label_convention=UTC_SESSION_DATE)
    args.update(overrides)
    return compare_sma_candidate(args.pop('history', history()), **args)


def test_hand_computed_signals_and_final_unexecuted_decision():
    d = decisions()
    assert d.changes == (0, 0, 1, -1, 0, 1)
    assert d.pending_final_change == 1
    assert d.executable_changes == (0, 0, 1, -1, 0, 0)
    assert d.reference_changes == (0, 0, 1, 0, 0, 0)


def test_candidate_and_reference_have_hand_computed_price_results():
    r = compare()
    assert r.candidate.net_price_pnl == -1  # buy next open=8, sell next open=7
    assert r.reference.net_price_pnl == 2  # buy at8, terminal mark10; no sell
    assert len(r.candidate.fills) == 2 and len(r.reference.fills) == 1
    assert not r.deployment_approved
    assert not r.candidate.signal_causality_verified


def test_explicit_costs_lower_both_results():
    zero, cost = compare(), compare(commission_bps=5, slippage_bps=5)
    assert cost.candidate.net_price_pnl < zero.candidate.net_price_pnl
    assert cost.reference.net_price_pnl < zero.reference.net_price_pnl
    assert cost.candidate.commissions > 0


def test_future_price_changes_do_not_change_prefix_decisions():
    h = history()
    changed = replace(h, bars=h.bars[:-1] + (replace(h.bars[-1], close=Decimal(2)),))
    assert decisions(h).changes[:-1] == decisions(changed).changes[:-1]
    assert decisions(h).changes[-1] != decisions(changed).changes[-1]
    for n in (4, 5):
        assert decisions(replace(h, bars=h.bars[:n])).changes == decisions(h).changes[:n]


def test_holdout_boundary_uses_earlier_warmup_but_no_earlier_positions():
    d = decisions(trade_from=date(2026, 1, 7))
    assert d.first_eligible_index == 3
    assert d.changes[:3] == (0, 0, 0)
    assert d.reference_changes == (0, 0, 0, 1, 0, 0)


def test_equality_is_flat_and_never_forces_trade():
    h = history()
    h = replace(h, bars=tuple(replace(b, close=Decimal(10)) for b in h.bars))
    assert decisions(h).changes == (0,) * 6


@pytest.mark.parametrize('window', [True, 1, 253, 3.0, '3'])
def test_invalid_windows(window):
    with pytest.raises(ResearchDataError):
        decisions(window=window)


@pytest.mark.parametrize('units', [0, -1, True, 1.0, 'NaN'])
def test_invalid_units(units):
    with pytest.raises(ResearchDataError):
        decisions(units=units)


@pytest.mark.parametrize('trade_from', [date(2025, 12, 31), date(2026, 1, 10), date(2026, 1, 9), '2026-01-02'])
def test_invalid_or_unexecutable_evaluation_dates(trade_from):
    with pytest.raises(ResearchDataError):
        decisions(trade_from=trade_from)


def test_no_implicit_parameter_search_or_cash_adjustment():
    with pytest.raises(ResearchDataError, match='insufficient'):
        compare(initial_cash='1')


def test_incomplete_history_rejected_before_signals_execute():
    h = history()
    with pytest.raises(ResearchDataError, match='scheduled sessions'):
        compare(history=replace(h, bars=h.bars[:2] + h.bars[3:]))


def test_ambient_decimal_precision_and_input_immutability():
    h = history()
    expected = decisions(h)
    with localcontext() as ctx:
        ctx.prec = 3
        assert decisions(h) == expected
    assert h == history()
