"""Fixed synthetic counterexample; no original code, data or performance claims."""
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Context, Decimal, localcontext

import pytest

import astral_stock_agent.research_strict_sma as module
from astral_stock_agent.research_data import DailyBar, DailyHistory, ResearchDataError
from astral_stock_agent.research_sessions import UTC_SESSION_DATE
from astral_stock_agent.sessions import session_on
from astral_stock_agent.independent_validation import RUNNERS


def history(closes=None):
    closes = [1]*14+[15, 2, 2] if closes is None else closes
    days, day = [], date(2026, 1, 2)
    while len(days) < len(closes):
        if session_on(day) is not None:
            days.append(day)
        day += timedelta(days=1)
    bars = tuple(DailyBar(datetime.combine(d, datetime.min.time(), timezone.utc),
        Decimal(8 if i == 16 else 7 if i == 17 else 10), Decimal(20), Decimal(1),
        Decimal(c), Decimal(100)) for i, (d, c) in enumerate(zip(days, closes)))
    return DailyHistory('SPY', bars, 'synthetic-declaration-not-trusted')


def decisions(h=None, **kw):
    return module.strict_sma15_decisions(h or history(), trade_from=kw.get('trade_from', date(2026,1,2)),
        label_convention=kw.get('label_convention', UTC_SESSION_DATE))


def compare(h=None, **kw):
    h = h or history()
    args = dict(trade_from=date(2026,1,2), first_day=h.bars[0].timestamp.date(),
        last_day=h.bars[-1].timestamp.date(), now=datetime(2026,2,10,tzinfo=timezone.utc),
        label_convention=UTC_SESSION_DATE, data_kind='synthetic')
    args.update(kw)
    return module.compare_strict_sma15(h, **args)


def test_counterexample_equality_keeps_target_then_strict_downward_exit():
    result = compare()
    assert result.strict_decisions.changes == (0,)*14+(1,0,-1)
    assert result.else_flat_decisions.changes == (0,)*14+(1,-1,0)
    assert result.strict_decisions.pending_final_change == -1
    assert result.strict_decisions.executable_changes == (0,)*14+(1,0,0)
    assert [(f.signal_index,f.execution_index) for f in result.strict.fills] == [(14,15)]
    assert [(f.signal_index,f.execution_index) for f in result.else_flat.fills] == [(14,15),(15,16)]
    assert result.strict.units == 1 and result.else_flat.units == 0
    assert result.strict.final_marked_equity == Decimal('9991.9899975')
    assert result.strict.fills[0].price == Decimal('10.005')
    assert result.strict.fills[0].commission == Decimal('0.0050025')


def test_executed_exit_uses_later_open_gap_and_costs_without_new_engine():
    r = compare(history([1]*14+[15,2,2,1]))
    assert [(f.signal_index,f.execution_index) for f in r.strict.fills] == [(14,15),(16,17)]
    assert r.strict.fills[1].units_change == -1
    assert r.strict.fills[1].price == Decimal('6.9965')
    assert r.strict.cash == Decimal('9996.98299925')
    assert r.strict.units == 0
    assert all(m.units in (0,1) for m in r.strict.daily_marks)


def test_flat_equality_warmup_boundary_and_terminal_entry_are_not_fills():
    assert decisions(history([1]*17)).changes == (0,)*17
    d = decisions(trade_from=history().bars[15].timestamp.date())
    assert d.first_eligible_index == 15 and d.changes == (0,)*17
    r = compare(history([1]*15+[2]))
    assert r.strict_decisions.pending_final_change == 1
    assert not r.strict.fills and r.strict.units == 0 and r.strict.cash == 10000


def test_prefix_and_future_changes_preserve_past_decisions():
    h = history([1]*14+[15,2,2,1]); d = decisions(h)
    for n in (16,17):
        assert decisions(replace(h,bars=h.bars[:n])).changes == d.changes[:n]
    changed = replace(h,bars=h.bars[:-1]+(replace(h.bars[-1],close=Decimal(20)),))
    assert decisions(changed).changes[:-1] == d.changes[:-1]
    assert decisions(changed).changes[-1] != d.changes[-1]


def test_replay_context_input_hash_and_approval_boundaries(monkeypatch):
    h=history(); before=deepcopy(h); r=compare(h)
    hostile=Context(prec=2,Emin=-1,Emax=1)
    for trap in hostile.traps: hostile.traps[trap]=True
    with localcontext(hostile): assert compare(h)==r
    assert h==before and compare(h)==r
    changed=replace(h,bars=h.bars[:-1]+(replace(h.bars[-1],volume=Decimal(101)),))
    assert compare(changed).input_sha256 != r.input_sha256
    assert compare(replace(h,sha256='arbitrary')).input_sha256 == r.input_sha256
    assert compare(h,data_kind='historical').status == r.status == 'insufficient'
    assert not any((r.source_equivalence_verified,r.selection_independence_verified,
                    r.deployment_approved,r.new_orders_allowed,r.strict.actual_execution_verified))
    assert module.RULE_ID not in RUNNERS and len(RUNNERS)==2
    calls=[]; real=module.simulate_next_open
    def count(*a,**kw): calls.append(kw); return real(*a,**kw)
    monkeypatch.setattr(module,'simulate_next_open',count)
    assert compare()==r and len(calls)==2
    assert all((c['initial_cash'],c['commission_bps'],c['slippage_bps'])==('10000','5','5') for c in calls)


@pytest.mark.parametrize('change', ['symbol','gap','future','bad_ohlc','no_cash','kind','date','convention','short','oversize'])
def test_invalid_inputs_fail_closed_with_fixed_private_error(change,monkeypatch):
    h=history(); options={}
    if change=='symbol':h=replace(h,symbol='ORCL')
    elif change=='gap':h=replace(h,bars=h.bars[:5]+h.bars[6:])
    elif change=='future':options['now']=datetime(2026,1,5,tzinfo=timezone.utc)
    elif change=='bad_ohlc':h=replace(h,bars=(replace(h.bars[0],low=Decimal(11)),)+h.bars[1:])
    elif change=='no_cash':h=replace(h,bars=h.bars[:15]+(replace(h.bars[15],open=Decimal(20000),high=Decimal(20000)),)+h.bars[16:])
    elif change=='kind':options['data_kind']='forward'
    elif change=='date':options['trade_from']=h.bars[-1].timestamp.date()
    elif change=='convention':options['label_convention']='infer'
    elif change=='short':h=replace(h,bars=h.bars[:15])
    else:h=replace(h,bars=h.bars*121)
    with pytest.raises(ResearchDataError,match='^'+module.ERROR+'$'):
        compare(h,**options)
