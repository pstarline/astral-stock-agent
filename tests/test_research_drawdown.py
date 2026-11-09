"""Hand-calculated synthetic paths; no market data or actual execution claims."""
from dataclasses import FrozenInstanceError
from datetime import date, datetime, timezone
from decimal import Decimal, localcontext

import pytest

from astral_stock_agent.research_data import DailyBar, DailyHistory
from astral_stock_agent.research_execution import simulate_next_open
from astral_stock_agent.research_sessions import UTC_SESSION_DATE


def simulate(closes, changes, *, cash=100, units_cost=0, slip=0):
    history = DailyHistory('SPY', tuple(
        DailyBar(datetime(2026, 11, 2+i, tzinfo=timezone.utc), Decimal(10),
                 Decimal(20), Decimal(1), Decimal(c), Decimal(100))
        for i, c in enumerate(closes)), 'synthetic')
    result = simulate_next_open(history, changes, initial_cash=cash,
        commission_bps=units_cost, slippage_bps=slip,
        first_day=date(2026, 11, 2), last_day=date(2026, 11, 1+len(closes)),
        now=datetime(2026, 11, 7, tzinfo=timezone.utc), label_convention=UTC_SESSION_DATE)
    return history, result


def test_profitable_finish_does_not_hide_half_equity_drawdown():
    _, r = simulate([10, 12, 6, 13], (10, 0, 0, 0))
    assert [m.equity for m in r.daily_marks] == [100, 120, 60, 130]
    assert [m.close_drawdown_fraction for m in r.daily_marks] == [0, 0, Decimal('.5'), 0]
    assert r.max_close_drawdown_fraction == Decimal('.5')
    assert r.net_price_pnl == 30 and r.final_marked_equity == 130
    assert len(r.fills) == 1 and r.units == 10  # no fabricated final sale
    assert not r.deployment_approved and not r.actual_execution_verified


def test_initial_cash_is_peak_and_entry_costs_reduce_equity_once():
    _, r = simulate([10, 10, 10], (1, 0, 0), units_cost=100, slip=100)
    assert r.cash == Decimal('89.799') and r.commissions == Decimal('.101')
    assert [m.equity for m in r.daily_marks] == [100, Decimal('99.799'), Decimal('99.799')]
    assert r.max_close_drawdown_fraction == Decimal('.00201')
    assert r.net_price_pnl == Decimal('-.201')


def test_sale_and_subsequent_no_trade_marks_use_cash_without_future_exposure():
    _, r = simulate([10, 12, 6, 1], (5, -5, 0, 0))
    assert [m.equity for m in r.daily_marks] == [100, 110, 100, 100]
    assert [m.units for m in r.daily_marks] == [0, 5, 0, 0]
    assert r.daily_marks[-1].equity == r.cash == r.final_marked_equity
    assert r.max_close_drawdown_fraction == Decimal(10)/110


def test_no_trades_marks_every_session_without_inventing_fills():
    h, r = simulate([10, 12, 6, 13], (0, 0, 0, 0))
    assert [m.bar_index for m in r.daily_marks] == list(range(len(h.bars)))
    assert all(m.equity == 100 and m.close_drawdown_fraction == 0 for m in r.daily_marks)
    assert r.max_close_drawdown_fraction == 0 and not r.fills


def test_close_only_metric_does_not_claim_intraday_extremes_are_drawdown():
    # Every synthetic low=1, but the mark is close=10. No intraday risk claim.
    _, r = simulate([10, 10, 10], (10, 0, 0))
    assert r.max_close_drawdown_fraction == 0
    assert not r.corporate_actions_verified and not r.signal_causality_verified


def test_future_recovery_does_not_rewrite_earlier_marks_or_drawdowns():
    _, a = simulate([10, 12, 6, 13], (5, 0, 0, 0))
    _, b = simulate([10, 12, 6, 7], (5, 0, 0, 0))
    assert a.daily_marks[:3] == b.daily_marks[:3]
    assert a.max_close_drawdown_fraction == b.max_close_drawdown_fraction


def test_drawdowns_are_independent_of_ambient_decimal_precision():
    _, expected = simulate([10, 12, 7, 13], (3, 0, 0, 0))
    with localcontext() as ctx:
        ctx.prec = 4
        _, actual = simulate([10, 12, 7, 13], (3, 0, 0, 0))
    assert actual == expected


def test_marks_are_immutable_and_price_inputs_are_preserved():
    h, r = simulate([10, 12, 6, 13], (5, 0, 0, 0))
    assert [bar.close for bar in h.bars] == [10, 12, 6, 13]
    with pytest.raises(FrozenInstanceError):
        r.daily_marks[0].equity = Decimal(0)
