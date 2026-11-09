"""Hand-calculated synthetic scenarios, never actual orders/fills."""
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal, localcontext

import pytest

from astral_stock_agent.research_data import DailyBar, DailyHistory, ResearchDataError
from astral_stock_agent.research_sessions import UTC_SESSION_DATE
from astral_stock_agent.research_execution import simulate_next_open


def history():
    # Nov 2 close=10; next open=12; following open=8; final close=9.
    bars = []
    for day, op, close in [(2, '10', '10'), (3, '12', '14'), (4, '8', '9')]:
        bars.append(DailyBar(datetime(2026, 11, day, tzinfo=timezone.utc),
                             Decimal(op), Decimal('20'), Decimal('1'), Decimal(close), Decimal('100')))
    return DailyHistory('SPY', tuple(bars), 'synthetic')


def run(changes=(1, -1, 0), **kwargs):
    args = dict(initial_cash='100', commission_bps='100', slippage_bps='100',
                first_day=date(2026, 11, 2), last_day=date(2026, 11, 4),
                now=datetime(2026, 11, 5, tzinfo=timezone.utc), label_convention=UTC_SESSION_DATE)
    args.update(kwargs)
    return simulate_next_open(args.pop('history', history()), changes, **args)


def test_roundtrip_uses_next_open_and_charges_both_sides():
    r = run()
    # Buy 12*1.01=12.12, fee=.1212; sell 8*.99=7.92, fee=.0792.
    assert r.cash == Decimal('95.5996')
    assert r.net_price_pnl == Decimal('-4.4004')
    assert r.commissions == Decimal('.2004')
    assert r.units == 0 and r.final_marked_equity == r.cash
    assert [(f.signal_index, f.execution_index) for f in r.fills] == [(0, 1), (1, 2)]
    assert [f.price for f in r.fills] == [Decimal('12.12'), Decimal('7.92')]
    assert r.kind == 'hypothetical_price_only'
    assert not any([r.actual_execution_verified, r.signal_causality_verified,
                    r.corporate_actions_verified, r.deployment_approved])


def test_zero_cost_comparison_is_exact_and_not_paper_profit():
    r = run(commission_bps=0, slippage_bps=0)
    assert r.cash == 96 and r.net_price_pnl == -4
    assert r.commissions == 0
    assert run().net_price_pnl < r.net_price_pnl


def test_terminal_holdings_are_marked_without_fabricated_liquidation():
    r = run((1, 0, 0))
    assert r.cash == Decimal('87.7588')
    assert r.units == 1 and r.final_marked_equity == Decimal('96.7588')
    assert len(r.fills) == 1


def test_no_trade_preserves_cash():
    r = run((0, 0, 0))
    assert r.cash == 100 and r.units == 0 and r.net_price_pnl == 0 and not r.fills


def test_later_price_cannot_change_already_simulated_fill():
    h = history()
    changed = replace(h, bars=h.bars[:-1] + (replace(h.bars[-1], open=Decimal('7'), close=Decimal('6')),))
    assert run(history=h).fills[0] == run(history=changed).fills[0]
    # Caller-provided signals are still not certified as causal.
    assert run(history=changed).signal_causality_verified is False


def test_roundtrip_fee_not_counted_twice():
    r = run(slippage_bps=0)
    assert r.cash == Decimal('95.80')  # -4 price loss and .12+.08 commissions
    assert r.net_price_pnl == Decimal('-4.20')


@pytest.mark.parametrize('changes,cash', [((100, 0, 0), '100'), ((-1, 0, 0), '100'),
                                           ((1, -2, 0), '100'), ((1, 0, 0), '12.12')])
def test_insufficient_cash_or_short_rejected_without_partial_fill(changes, cash):
    with pytest.raises(ResearchDataError, match='insufficient'):
        run(changes, initial_cash=cash)


@pytest.mark.parametrize('changes', [(0, 0, 1), (0, 0, -1)])
def test_no_same_close_or_missing_next_open_execution(changes):
    with pytest.raises(ResearchDataError, match='no next session'):
        run(changes)


@pytest.mark.parametrize('changes', [(), (1, 0), [1, -1, 0], (True, -1, 0), (1.0, -1, 0), ('NaN', 0, 0)])
def test_decision_shape_and_numbers_rejected(changes):
    with pytest.raises(ResearchDataError):
        run(changes)


@pytest.mark.parametrize('kwargs', [dict(initial_cash=0), dict(initial_cash=-1),
    dict(initial_cash=True), dict(commission_bps=-1), dict(slippage_bps=-1),
    dict(commission_bps=10000), dict(slippage_bps=10000),
    dict(commission_bps='NaN'), dict(slippage_bps='Infinity'), dict(initial_cash='1e100'),
    dict(initial_cash=100.0)])
def test_cost_assumptions_must_be_explicit_valid_numbers(kwargs):
    with pytest.raises(ResearchDataError):
        run(**kwargs)


def test_missing_session_cannot_be_bridged_by_next_observed_bar():
    h = history()
    with pytest.raises(ResearchDataError, match='scheduled sessions'):
        run((1, 0), history=replace(h, bars=(h.bars[0], h.bars[-1])))


def test_ambient_decimal_precision_does_not_change_results():
    expected = run()
    with localcontext() as ctx:
        ctx.prec = 4
        assert run() == expected


def test_inputs_are_unchanged():
    h = history()
    original = tuple(h.bars)
    run(history=h)
    assert h.bars == original


@pytest.mark.parametrize('field,value', [('open', '0'), ('close', 'NaN'), ('high', '2'), ('volume', '-1')])
def test_invalid_manually_constructed_bars_fail_closed(field, value):
    h = history()
    with pytest.raises(ResearchDataError):
        run(history=replace(h, bars=(replace(h.bars[0], **{field: Decimal(value)}),) + h.bars[1:]))
