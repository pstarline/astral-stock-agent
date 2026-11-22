"""Shared runner: synthetic accounting, causal boundaries and failed evidence."""
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Context, Decimal, localcontext
import json

import pytest

import astral_stock_agent.independent_validation as validation
from astral_stock_agent.research_data import ResearchDataError
from test_independent_signals import inputs as sma_inputs
from test_independent_reversal import inputs as reversal_inputs


NOW = datetime(2026, 11, 21, tzinfo=timezone.utc)


def run(spec=None, prices=None, **kwargs):
    s, p = sma_inputs()
    args = dict(origin='self', data_kind='synthetic', verified_us_symbols=['SPY'], now=NOW)
    args.update(kwargs)
    return validation.validate_candidate(s if spec is None else spec,
        p if prices is None else prices, **args)


def checks(result):
    return {c.name: c for c in result.checks}


def source(*, timing='next_session_open', timeframe='1d', assets=None):
    # Minimal synthetic historical handoff, not a provider projection or capability.
    # Unknown hashes stay unknown; this is not an executable public-source adapter.
    return dict(schema='public_strategy_review_candidate_v1', origin='public',
                assets=['SPY'] if assets is None else list(assets), timeframe=timeframe,
                fill_price_mode=timing, definition_sha256=None, evidence_sha256=None,
                needs_source_refresh=False)


@pytest.mark.parametrize('make,expected', [(sma_inputs, '-1'), (reversal_inputs, '2')])
def test_existing_rules_produce_hand_checked_accounting_and_replay_evidence(make, expected):
    s, p = make()
    result = run(s, p, stress_costs=dict(commission_bps='5', slippage_bps='5'))
    assert result.status == 'insufficient'
    assert all(c.status == 'passed' for c in result.checks)
    baseline, stress = result.scenarios
    assert Decimal(baseline['metrics']['cumulative_pnl']) == Decimal(expected)
    assert Decimal(baseline['metrics']['realized_pnl']) == Decimal(expected)
    assert Decimal(baseline['metrics']['unrealized_pnl']) == 0
    assert baseline['metrics']['cumulative_simulated_fills'] == 2
    assert baseline['completed_round_trips'] is None  # 2 order legs are not 2 round trips
    assert baseline['fill_count_definition'] == 'executed_order_legs_not_completed_round_trips'
    assert baseline['return_denominator'] == 'entire_initial_hypothetical_cash'
    assert baseline['calculation_scope']['first_day'] == s['first_day']
    assert baseline['calculation_scope']['last_day'] == s['last_day']
    assert baseline['signal_start_day'] == s['rule']['trade_from']
    assert baseline['drawdown_definition'] == 'close_to_running_peak_fraction_including_initial_cash'
    assert Decimal(baseline['account_return_fraction']) == Decimal(expected) / 100
    assert all(baseline[k] is None for k in ('sharpe', 'win_rate', 'annualized_return'))
    assert Decimal(stress['metrics']['cumulative_commission']) > 0
    assert Decimal(stress['metrics']['cumulative_pnl']) < Decimal(expected)
    assert 'actual_forward_paper' in result.unresolved
    assert 'synthetic_results_not_market_performance' in result.unresolved
    assert not any((result.deployment_approved, result.new_orders_allowed,
                    result.actual_execution_verified, result.source_equivalence_verified))


def test_same_input_and_ambient_decimal_context_reproduce_without_mutation():
    s, p = reversal_inputs()
    s.update(commission_bps='5', slippage_bps='5')
    c = source()
    costs = dict(commission_bps='10', slippage_bps='10')
    before = deepcopy((s, c, costs))
    result = run(s, p, origin='public', public_candidate=c, stress_costs=costs)
    hostile = Context(prec=2, Emin=-1, Emax=1)
    for key in hostile.traps:
        hostile.traps[key] = True
    with localcontext(hostile):
        assert run(s, p, origin='public', public_candidate=c, stress_costs=costs) == result
    assert (s, c, costs) == before
    assert checks(result)['source_execution'].status == 'insufficient'
    assert checks(result)['exact_replay'].status == 'passed'


@pytest.mark.parametrize('origin', ['self', 'public'])
def test_both_origins_share_engine_costs_metrics_and_missing_evidence(origin):
    result = run(origin=origin, public_candidate=source() if origin == 'public' else None)
    assert result.scenarios == run().scenarios
    assert checks(result)['cost_sensitivity'].status == 'insufficient'
    assert checks(result)['daily_next_open_accounting'].status == 'passed'
    assert set(validation.UNVERIFIED) <= set(result.unresolved)
    assert result.status == 'insufficient'


@pytest.mark.parametrize('data_kind', ['synthetic', 'historical'])
def test_historical_recalculation_is_never_forward_or_source_verification(data_kind):
    r = run(data_kind=data_kind)
    assert r.data_kind == data_kind and r.status == 'insufficient'
    assert 'actual_forward_paper' in r.unresolved
    assert not r.actual_execution_verified


@pytest.mark.parametrize('timing,timeframe,assets', [
    ('on_close', '1d', None), ('on_close', '15m', None),
    ('next_session_open', '15m', None), ('next_session_open', '1d', ['SPY', 'QQQ']),
    ('next_session_open', '1d', ['QQQ']),
])
def test_source_execution_or_scope_mismatch_blocks_before_rule_execution(monkeypatch, timing, timeframe, assets):
    def forbidden(*a, **kw):
        raise AssertionError('must not generate signals')
    monkeypatch.setitem(validation.RUNNERS, 'independent_sma_candidate_v1', forbidden)
    result = run(origin='public', public_candidate=source(timing=timing, timeframe=timeframe, assets=assets))
    assert result.status == 'held' and result.scenarios == ()
    assert result.checks[-1].reason in ('source_daily_next_open_conditions_differ', 'source_asset_scope_differs')


@pytest.mark.parametrize('field', ['timeframe', 'fill_price_mode'])
def test_missing_public_execution_does_not_inherit_local_defaults(field):
    c = source(); c[field] = None
    r = run(origin='public', public_candidate=c)
    assert r.status == 'insufficient' and not r.scenarios


@pytest.mark.parametrize('origin', ['self', 'public'])
def test_missing_local_rules_are_insufficient_without_fake_metrics(origin):
    r = validation.validate_candidate(None, None, origin=origin, data_kind='historical',
        verified_us_symbols=['SPY'], now=NOW, public_candidate=source() if origin == 'public' else None)
    assert r.status == 'insufficient' and not r.scenarios and r.input_sha256 is None
    assert checks(r)['executable_input'].status == 'insufficient'


def test_final_signal_pending_and_single_buy_is_not_a_closed_trade():
    s, p = sma_inputs(closes=(10, 12, 14, 8), days=('2026-01-02', '2026-01-05', '2026-01-06', '2026-01-07'))
    r = run(s, p)
    a = r.scenarios[0]['metrics']
    assert a['cumulative_simulated_fills'] == 1
    assert Decimal(a['units']) == 1 and Decimal(a['pending_close_change']) == -1
    assert Decimal(a['realized_pnl']) == 0
    assert r.scenarios[0]['completed_round_trips'] is None


@pytest.mark.parametrize('case', ['checksum', 'gap', 'unclosed', 'cash', 'unknown_rule', 'missing_cost', 'rows'])
def test_failed_data_and_accounting_are_held_with_no_invented_results(case):
    s, p = sma_inputs()
    if case == 'checksum': s['price_sha256'] = '0' * 64
    if case == 'gap':
        s, p = sma_inputs(closes=(10, 12, 8, 7, 10), days=('2026-01-02', '2026-01-05', '2026-01-07', '2026-01-08', '2026-01-09'))
    if case == 'cash': s['initial_cash'] = '1'
    if case == 'unknown_rule': s['rule']['id'] = 'execute_external_source'
    if case == 'missing_cost': del s['slippage_bps']
    if case == 'rows': s['price_count'] = 2049
    now = datetime(2026, 1, 9, 18, tzinfo=timezone.utc) if case == 'unclosed' else NOW
    r = run(s, p, now=now)
    assert r.status == 'held' and not r.scenarios
    assert not r.new_orders_allowed


@pytest.mark.parametrize('costs', [
    {}, {'commission_bps': '10'}, {'commission_bps': 'NaN', 'slippage_bps': '5'},
    {'commission_bps': '-1', 'slippage_bps': '5'}, {'commission_bps': '10000', 'slippage_bps': '5'},
    {'commission_bps': '0', 'slippage_bps': '0'}, {'commission_bps': True, 'slippage_bps': '5'},
    {'commission_bps': '5', 'slippage_bps': '5', 'retry': True},
    [{'commission_bps': '5', 'slippage_bps': '5'}],
])
def test_stress_costs_never_default_or_create_unbounded_parameter_search(costs):
    r = run(stress_costs=costs)
    assert r.status == 'held' and not r.scenarios


def test_stress_cannot_lower_one_cost_while_raising_another():
    s, p = sma_inputs(); s.update(commission_bps='5', slippage_bps='5')
    assert run(s, p, stress_costs=dict(commission_bps='4', slippage_bps='10')).status == 'held'


def test_stress_failure_retains_baseline_but_does_not_report_sensitivity_pass():
    s, p = sma_inputs(); s['initial_cash'] = '8'
    r = run(s, p, stress_costs=dict(commission_bps='5', slippage_bps='5'))
    assert r.status == 'held' and len(r.scenarios) == 1
    assert checks(r)['cost_sensitivity'].status == 'held'
    assert checks(r)['daily_next_open_accounting'].status == 'passed'


def test_stale_public_handoff_requires_source_refresh_without_execution():
    c = source(); c['needs_source_refresh'] = True
    r = run(origin='public', public_candidate=c)
    assert r.status == 'insufficient' and not r.scenarios
    assert checks(r)['source_freshness'].reason == 'source_refresh_required'


def test_replay_mismatch_stops_without_retry_or_stress(monkeypatch):
    real = validation.RUNNERS['independent_sma_candidate_v1']
    calls = []
    def changed(*a, **kw):
        calls.append(1)
        m, acc, body = real(*a, **kw)
        if len(calls) == 2:
            acc['cumulative_simulated_fills'] += 1
        return m, acc, body
    monkeypatch.setitem(validation.RUNNERS, 'independent_sma_candidate_v1', changed)
    r = run(stress_costs=dict(commission_bps='5', slippage_bps='5'))
    assert r.status == 'held' and len(calls) == 2
    assert checks(r)['exact_replay'].reason == 'same_input_result_differs'


def test_prefix_future_dependence_is_held(monkeypatch):
    real = validation.RUNNERS['independent_sma_candidate_v1']
    def changed(*a, **kw):
        m, acc, body = real(*a, **kw)
        if kw.get('allow_pending_only'):
            m['close_signals'][0]['units_change'] = '1'
        return m, acc, body
    monkeypatch.setitem(validation.RUNNERS, 'independent_sma_candidate_v1', changed)
    r = run()
    assert r.status == 'held' and checks(r)['terminal_prefix'].reason == 'earlier_decisions_changed'


def test_terminal_only_baseline_is_held_before_claiming_causal_evidence():
    s, p = sma_inputs(); s['rule']['trade_from'] = s['last_day']
    r = run(s, p)
    assert r.status == 'held' and not r.scenarios
    assert checks(r)['daily_next_open_accounting'].status == 'held'
    assert not r.source_equivalence_verified


def test_unexpected_prefix_failure_is_held_and_error_is_private(monkeypatch, capsys):
    real = validation.RUNNERS['independent_sma_candidate_v1']
    def changed(*a, **kw):
        if kw.get('allow_pending_only'):
            raise ResearchDataError('SYNTHETIC_PRIVATE_VALUE https://private.invalid/?sig=SECRET')
        return real(*a, **kw)
    monkeypatch.setitem(validation.RUNNERS, 'independent_sma_candidate_v1', changed)
    r = run()
    assert r.status == 'held' and checks(r)['terminal_prefix'].status == 'held'
    assert 'PRIVATE_VALUE' not in json.dumps(asdict(r))
    assert not capsys.readouterr().out


def test_bounded_four_calls_and_no_candidate_or_price_input_mutation(monkeypatch):
    real = validation.RUNNERS['independent_daily_reversal_v1']
    calls = []
    def count(*a, **kw):
        calls.append(1)
        return real(*a, **kw)
    monkeypatch.setitem(validation.RUNNERS, 'independent_daily_reversal_v1', count)
    s, p = reversal_inputs()
    r = run(s, p, stress_costs=dict(commission_bps='5', slippage_bps='5'))
    assert len(calls) == 4 and len(r.scenarios) == 2


@pytest.mark.parametrize('kw', [dict(origin='SYNTHETIC_PRIVATE_VALUE'),
    dict(data_kind='SYNTHETIC_PRIVATE_VALUE'), dict(origin='public'),
    dict(verified_us_symbols=[]), dict(verified_us_symbols=['SPY', 'https://private.invalid']),
    dict(now=NOW.replace(tzinfo=None))])
def test_invalid_metadata_cannot_be_reflected_into_results_or_errors(kw, capsys):
    r = run(**kw)
    assert r.status == 'held' and not r.scenarios
    assert 'SYNTHETIC_PRIVATE_VALUE' not in json.dumps(asdict(r))
    assert not capsys.readouterr().out


def test_no_raw_prices_source_reports_or_private_ids_in_results(capsys):
    c = source()
    c.update(entry_rules_summary='SYNTHETIC_SECRET_SOURCE', account_id='SYNTHETIC_PRIVATE_ACCOUNT',
             source_code='raise RuntimeError("never execute")', signed_url='https://private.invalid/?sig=SECRET')
    r = run(origin='public', public_candidate=c)
    output = json.dumps(asdict(r)) + repr(r)
    assert r.scenarios and checks(r)['exact_replay'].status == 'passed'
    for text in ('SECRET_SOURCE', 'PRIVATE_ACCOUNT', 'private.invalid', 'source_code', 'timestamp,open',
                 'close_signals', 'report_sha256'):
        assert text not in output
    assert not capsys.readouterr().out


def test_input_evidence_binds_data_origin_costs_rule_and_observation():
    base = run()
    s, p = sma_inputs(); s['rule']['window'] = 2
    assert run(s, p).input_sha256 != base.input_sha256
    assert run(data_kind='historical').input_sha256 != base.input_sha256
    assert run(stress_costs=dict(commission_bps='5', slippage_bps='5')).input_sha256 != base.input_sha256
    assert run(origin='public', public_candidate=source()).input_sha256 != base.input_sha256
    assert run(now=datetime(2026, 11, 22, tzinfo=timezone.utc)).input_sha256 != base.input_sha256


def test_offline_runner_has_no_file_network_ledger_or_delivery_authority(monkeypatch):
    import builtins
    from pathlib import Path
    import socket
    from astral_stock_agent.independent_ledger import IndependentPaperLedger
    from astral_stock_agent.price_archive import PriceArchive
    from astral_stock_agent.delivery import Outbox, Telegram
    # Warm the existing exchange calendar before forbidding all external I/O.
    assert run().scenarios
    def forbidden(*a, **kw):
        raise AssertionError('operation is outside research scope')
    monkeypatch.setattr(builtins, 'open', forbidden)
    monkeypatch.setattr(Path, 'open', forbidden)
    monkeypatch.setattr(socket, 'socket', forbidden)
    monkeypatch.setattr(IndependentPaperLedger, 'append', forbidden)
    monkeypatch.setattr(PriceArchive, 'append', forbidden)
    monkeypatch.setattr(Outbox, 'enqueue', forbidden)
    monkeypatch.setattr(Telegram, 'send', forbidden)
    result = run(stress_costs=dict(commission_bps='5', slippage_bps='5'))
    assert all(c.status == 'passed' for c in result.checks)

def test_common_validation_fixture_and_runner_do_not_require_retired_modules():
    import os
    from pathlib import Path
    import subprocess
    import sys
    root = Path(__file__).resolve().parents[1]
    script = '''
import importlib.abc, sys
blocked = {'astral_stock_agent.' + name for name in (
    'cli', 'research_contracts', 'astral', 'public_discovery', 'authoring',
    'preflight', 'activity', 'status_runner', 'account_observation')}
blocked.add('test_public_discovery')
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in blocked:
            raise AssertionError('retired module dependency')
sys.meta_path.insert(0, Guard())
def deny_network(event, args):
    if event.startswith('socket.'):
        raise AssertionError('network forbidden')
sys.addaudithook(deny_network)
import test_independent_validation as tests
candidate = tests.source()
assert candidate['definition_sha256'] is candidate['evidence_sha256'] is None
assert tests.source(assets=[])['assets'] == []
result = tests.run()
assert result.status == 'insufficient' and result.scenarios
assert tests.checks(result)['exact_replay'].status == 'passed'
assert tests.checks(result)['terminal_prefix'].status == 'passed'
assert not result.source_equivalence_verified and not result.actual_execution_verified
assert not result.deployment_approved and not result.new_orders_allowed
assert not blocked.intersection(sys.modules)
'''
    result = subprocess.run([sys.executable, '-c', script],
        env=dict(PATH=os.defpath, PYTHONPATH=os.pathsep.join(
            (str(root/'src'), str(root/'tests'))), PYTHONDONTWRITEBYTECODE='1'),
        capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, 'common validation fixture or runner requires retired modules'
    assert result.stdout == result.stderr == ''
