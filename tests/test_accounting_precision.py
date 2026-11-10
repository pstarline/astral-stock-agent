"""Accounting invariants must survive unrelated Decimal context changes."""
import copy
import json
from datetime import datetime
from decimal import Context, Inexact, ROUND_DOWN, ROUND_UP, localcontext
from pathlib import Path

import pytest

from astral_stock_agent.reconciliation import ReconciliationError, reconcile_daily
from astral_stock_agent.report import InvalidSnapshot, render


@pytest.fixture
def evidence():
    return json.loads((Path(__file__).parents[1] / 'examples/daily_reconciliation.json').read_text())


def now(e):
    return datetime.fromisoformat(e['current']['collected_at'].replace('Z', '+00:00'))


def reconcile(e):
    return reconcile_daily(e['previous'], e['current'], e['accounting'],
                           verified_us_symbols=['SPY'], now=now(e))


def cash_only(e, fee):
    for book in (e['previous'], e['current']):
        book.update(equity_usd='100000', cash_usd='100000', available_cash_usd='100000',
                    cumulative_realized_pnl_usd='0', unrealized_pnl_usd='0',
                    positions=[], orders=[], fills=[])
    e['accounting']['execution_fees_usd'] = fee


@pytest.mark.parametrize('precision', [2, 3, 6, 28])
def test_fee_mismatch_cannot_disappear_under_low_precision(evidence, precision):
    # No trades or capital flow. A $0.02 fee with unchanged cash is inconsistent.
    # Previously precision=3 silently rounded 100000 - .02 back to 100000.
    cash_only(evidence, '0.02')
    original = copy.deepcopy(evidence)
    with localcontext() as context:
        context.prec = precision
        with pytest.raises(ReconciliationError, match='^daily reconciliation blocked:'):
            reconcile(evidence)
    assert evidence == original


@pytest.mark.parametrize('rounding', [ROUND_UP, ROUND_DOWN])
def test_valid_cash_shares_pnl_and_report_are_independent_of_all_context_settings(evidence, rounding):
    expected = reconcile(evidence)
    expected_report = render(expected, now=now(evidence))
    hostile = Context(prec=2, rounding=rounding, Emin=-1, Emax=1)
    for signal in hostile.traps:
        hostile.traps[signal] = True
    hostile.flags[Inexact] = True
    with localcontext(hostile) as context:
        before = (context.prec, context.rounding, context.Emin, context.Emax,
                  dict(context.traps), dict(context.flags))
        result = reconcile(evidence)
        assert result == expected and result['daily_total_pnl_usd'] == '27'
        assert render(result, now=now(evidence)) == expected_report
        assert before == (context.prec, context.rounding, context.Emin, context.Emax,
                          dict(context.traps), dict(context.flags))


@pytest.mark.parametrize('fee,blocked', [('0.01', False), ('0.0100000001', True)])
def test_existing_cent_tolerance_is_not_relaxed(evidence, fee, blocked):
    cash_only(evidence, fee)
    with localcontext() as context:
        context.prec = 3
        if blocked:
            with pytest.raises(ReconciliationError):
                reconcile(evidence)
        else:
            assert reconcile(evidence)['daily_total_pnl_usd'] == '0'


def test_subcent_formatting_does_not_require_inexact_ledger_arithmetic(evidence):
    # Display rounding is intentional; underlying equal equity/cash is exact.
    cash_only(evidence, '0')
    for book in (evidence['previous'], evidence['current']):
        for field in ('cash_usd', 'equity_usd', 'available_cash_usd'):
            book[field] = '100000.12345'
    result = reconcile(evidence)
    assert '$100,000.12' in render(result, now=now(evidence))[1]


def test_tiny_extra_fill_cannot_round_away_to_a_false_quantity_match(evidence):
    current = evidence['current']
    order = copy.deepcopy(current['orders'][0])
    order.update(quantity='1', filled_quantity='1')
    fill = copy.deepcopy(current['fills'][0])
    fill['quantity'] = '1'
    extra = copy.deepcopy(fill)
    extra.update(fill_ref='synthetic-extra', quantity='1e-1000')
    current.update(orders=[order], fills=[fill, extra])
    with pytest.raises(InvalidSnapshot, match='^missing or invalid snapshot fields$'):
        render(current, now=now(evidence))


def test_extreme_income_cannot_be_silently_rounded_out_of_accounting(evidence):
    cash_only(evidence, '0')
    evidence['accounting']['net_other_income_usd'] = '1e-1000'
    with pytest.raises(ReconciliationError):
        reconcile(evidence)
