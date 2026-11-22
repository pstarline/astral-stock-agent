"""Synthetic flat-reset window mechanics, never evidence of real selection."""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, Context, localcontext
import hashlib

import pytest

from astral_stock_agent.research_holdout import (
    evaluate_frozen_window, hypothesis_sha256, ERROR,
)
import astral_stock_agent.research_holdout as module
from astral_stock_agent.independent_validation import RUNNERS
from astral_stock_agent.independent_signals import preview_independent_sma
from astral_stock_agent.research_data import ResearchDataError
from test_independent_signals import inputs as sma_inputs
from test_independent_reversal import inputs as reversal_inputs

NOW = datetime(2026, 1, 15, tzinfo=timezone.utc)
FROZEN = datetime(2026, 1, 6, 22, tzinfo=timezone.utc)


def inputs():
    s, p = sma_inputs(closes=(10, 12, 14, 16, 18, 20))
    s['rule']['trade_from'] = '2026-01-07'
    return s, p


def run(spec=None, prices=None, **changes):
    s, p = inputs()
    s = s if spec is None else spec
    opts = dict(selection_end='2026-01-06', evaluation_start='2026-01-07',
        frozen_at=FROZEN, frozen_hypothesis_sha256=hypothesis_sha256(s),
        data_kind='synthetic', verified_us_symbols=['SPY'], now=NOW)
    opts.update(changes)
    return evaluate_frozen_window(s, p if prices is None else prices, **opts)


def test_prior_prices_only_warm_indicators_and_do_not_carry_profit_or_position():
    s, p = inputs(); r = run(s, p)
    assert r.status == 'insufficient'
    assert r.evaluation_start == '2026-01-07' and r.evaluation_end == '2026-01-09'
    a = r.result.scenarios[0]['metrics']
    assert Decimal(a['cumulative_pnl']) == 2
    assert Decimal(a['cash']) == 82 and Decimal(a['units']) == 1
    assert a['cumulative_simulated_fills'] == 1
    manifest, _, _ = preview_independent_sma(s, p, verified_us_symbols=['SPY'], now=NOW)
    assert all(Decimal(x['units_change']) == 0 for x in manifest['close_signals'][:3])
    assert Decimal(manifest['close_signals'][3]['units_change']) == 1
    earlier = deepcopy(s); earlier['rule']['trade_from'] = earlier['first_day']
    _, full, _ = preview_independent_sma(earlier, p, verified_us_symbols=['SPY'], now=NOW)
    assert Decimal(full['cumulative_pnl']) == 4  # not carried into this fresh window
    assert not any((r.selection_independence_verified, r.deployment_approved, r.new_orders_allowed))


def test_reversal_reuses_same_window_accounting_and_stress_with_four_calls(monkeypatch):
    s, p = reversal_inputs(); s['rule']['trade_from'] = '2026-01-09'
    calls = []
    real = RUNNERS[s['schema']]
    def count(*a, **kw): calls.append(1); return real(*a, **kw)
    monkeypatch.setitem(RUNNERS, s['schema'], count)
    r = run(s, p, selection_end='2026-01-08', evaluation_start='2026-01-09',
        frozen_at=datetime(2026, 1, 8, 22, tzinfo=timezone.utc),
        stress_costs=dict(commission_bps='5', slippage_bps='5'))
    assert r.status == 'insufficient' and len(r.result.scenarios) == 2
    assert len(calls) == 4 and not r.selection_independence_verified


@pytest.mark.parametrize('changes', [
    dict(selection_end='2026-01-07'), dict(selection_end='2026-01-05'),
    dict(selection_end='2026-01-04'), dict(evaluation_start='2026-01-08'),
    dict(evaluation_start='2026-01-09'), dict(selection_end='20260106'),
    dict(frozen_at=datetime(2026, 1, 6, 20, 59, tzinfo=timezone.utc)),
    dict(frozen_at=datetime(2026, 1, 7, 14, 30, tzinfo=timezone.utc)),
    dict(frozen_at=datetime(2026, 1, 7, 15, tzinfo=timezone.utc)),
    dict(frozen_at=FROZEN.replace(tzinfo=None)), dict(frozen_hypothesis_sha256='0'*64),
])
def test_overlap_gap_invalid_freeze_or_changed_hypothesis_blocks_before_evaluation(monkeypatch, changes):
    def forbidden(*a, **kw): raise AssertionError('no candidate evaluation')
    monkeypatch.setattr(module, 'validate_candidate', forbidden)
    with pytest.raises(ResearchDataError, match='^'+ERROR+'$'): run(**changes)


@pytest.mark.parametrize('key', ['rule', 'commission_bps', 'slippage_bps', 'last_day', 'initial_cash'])
def test_freeze_binds_rule_economics_and_predeclared_end(key):
    s, p = inputs(); digest = hypothesis_sha256(s)
    if key == 'rule': s['rule']['window'] = 2
    else: s[key] = {'commission_bps':'5','slippage_bps':'5','last_day':'2026-01-08','initial_cash':'101'}[key]
    assert hypothesis_sha256(s) != digest
    with pytest.raises(ResearchDataError): run(s, p, frozen_hypothesis_sha256=digest)


def test_freeze_does_not_require_future_prices_but_result_evidence_binds_them():
    s, p = inputs(); a = run(s, p); digest = hypothesis_sha256(s)
    changed = p.replace(b',20,20,1,20,100', b',19,20,1,19,100')
    assert changed != p
    s['price_sha256'] = hashlib.sha256(changed).hexdigest()
    assert hypothesis_sha256(s) == digest
    b = run(s, changed, frozen_hypothesis_sha256=digest)
    assert a.evidence_sha256 != b.evidence_sha256
    assert not b.selection_independence_verified


def test_same_inputs_decimal_environment_and_offsets_are_reproducible_without_mutation():
    s, p = inputs(); before = deepcopy(s); expected = run(s, p)
    hostile = Context(prec=2, Emin=-1, Emax=1)
    for key in hostile.traps: hostile.traps[key] = True
    with localcontext(hostile): assert run(s, p) == expected
    assert run(s, p, frozen_at=datetime.fromisoformat('2026-01-07T07:00:00+09:00')) == expected
    assert s == before


def test_new_york_midnight_labels_keep_the_declared_session_boundary():
    s, p = sma_inputs(closes=(10, 12, 14, 16, 18, 20), label='T00:00:00-05:00')
    s['label_convention'] = 'session_date_at_new_york_midnight'
    s['rule']['trade_from'] = '2026-01-07'
    r = run(s, p)
    assert r.status == 'insufficient' and r.evaluation_start == '2026-01-07'
    assert Decimal(r.result.scenarios[0]['metrics']['cumulative_pnl']) == 2


def test_weekend_is_not_a_missing_session_between_selection_and_evaluation():
    s, p = inputs(); s['rule']['trade_from'] = '2026-01-05'
    r = run(s, p, selection_end='2026-01-02', evaluation_start='2026-01-05',
            frozen_at=datetime(2026, 1, 4, 12, tzinfo=timezone.utc))
    assert r.status == 'insufficient' and not r.selection_independence_verified


def test_unclosed_or_corrupt_prices_and_invalid_rule_do_not_make_a_result():
    s, p = inputs()
    with pytest.raises(ResearchDataError): run(s, p+b'broken')
    with pytest.raises(ResearchDataError): run(now=datetime(2026, 1, 9, 18, tzinfo=timezone.utc))
    s['rule']['window'] = 1
    r = run(s, p)
    assert r.status == 'held' and r.reason == 'local_validation_blocked'
    assert r.evidence_sha256 is None


def test_failed_stress_is_held_not_successful_baseline_promotion():
    s, p = inputs(); s['initial_cash'] = '18'
    r = run(s, p, stress_costs=dict(commission_bps='5', slippage_bps='5'))
    assert r.status == 'held' and not r.deployment_approved


def test_historical_declaration_and_freeze_time_do_not_authenticate_selection():
    r = run(data_kind='historical')
    assert r.status == 'insufficient' and not r.selection_independence_verified
    assert not r.result.source_equivalence_verified and not r.result.actual_execution_verified


def test_errors_do_not_echo_private_hypothesis_or_prices(capsys):
    s, p = inputs(); s['rule']['trade_from'] = 'SYNTHETIC_PRIVATE'
    with pytest.raises(ResearchDataError) as err: run(s, p)
    assert str(err.value) == ERROR and 'PRIVATE' not in str(err.value)
    assert capsys.readouterr() == ('', '')


def test_no_file_network_ledger_or_delivery_authority(monkeypatch):
    import builtins
    import socket
    from astral_stock_agent.independent_ledger import IndependentPaperLedger
    from astral_stock_agent.delivery import Telegram, Outbox
    assert run().result.scenarios  # initialize existing calendar before I/O denial
    def forbidden(*a, **kw): raise AssertionError('outside research scope')
    monkeypatch.setattr(builtins, 'open', forbidden)
    monkeypatch.setattr(socket, 'socket', forbidden)
    monkeypatch.setattr(IndependentPaperLedger, 'append', forbidden)
    monkeypatch.setattr(Telegram, 'send', forbidden)
    monkeypatch.setattr(Outbox, 'enqueue', forbidden)
    assert run().status == 'insufficient'


def test_local_hypothesis_does_not_use_external_execution_comparison(monkeypatch):
    from astral_stock_agent import research_trend
    def forbidden(*a, **kw): raise AssertionError('external execution comparison forbidden')
    monkeypatch.setattr(research_trend, 'require_matching_execution', forbidden)
    assert run().status == 'insufficient'
