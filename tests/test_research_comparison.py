"""Paired local research only; synthetic prices, no provider/source intake."""
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal, Context, localcontext
import json

import pytest

from astral_stock_agent.research_comparison import compare_local_candidates
import astral_stock_agent.research_comparison as module
from astral_stock_agent.independent_validation import RUNNERS
from test_independent_reversal import inputs as reversal_inputs


NOW = datetime(2026, 11, 21, tzinfo=timezone.utc)


def inputs():
    # Use a reversal fixture's common history; derive another LOCAL rule spec.
    reversal, prices = reversal_inputs()
    sma = deepcopy(reversal)
    sma['schema'] = 'independent_sma_candidate_v1'
    sma['rule'] = dict(id='close_gt_trailing_sma_fixed_units_v1',
                       window=3, units='1', trade_from=sma['first_day'])
    return [sma, reversal], prices


def run(specs=None, prices=None, **changes):
    ss, pp = inputs()
    options = dict(data_kind='synthetic', verified_us_symbols=['SPY'], now=NOW)
    options.update(changes)
    return compare_local_candidates(ss if specs is None else specs,
        pp if prices is None else prices, **options)


def test_hand_checked_reversal_and_sma_differences_are_ordered_not_selection():
    r = run(stress_costs=dict(commission_bps='5', slippage_bps='5'))
    assert r.status == 'insufficient' and r.reason == 'same_conditions_arithmetic_only'
    assert len(r.candidates) == len(r.differences) == 2
    left, right = r.candidates
    assert Decimal(right.scenarios[0]['metrics']['cumulative_pnl']) == 2
    for i, d in enumerate(r.differences):
        assert Decimal(d['cumulative_pnl']) == (
            Decimal(right.scenarios[i]['metrics']['cumulative_pnl'])
            - Decimal(left.scenarios[i]['metrics']['cumulative_pnl']))
        assert d['definition'] == 'right_minus_left_not_ranking'
    assert not r.selection_approved and not r.deployment_approved and not r.new_orders_allowed
    assert 'no_independent_holdout_evidence' in r.limitations
    assert all(c.status == 'insufficient' for c in r.candidates)


def test_reverse_order_inverts_differences_and_changes_evidence():
    s, p = inputs(); a = run(s, p); b = run(s[::-1], p)
    assert a.evidence_sha256 != b.evidence_sha256
    for key in ('cumulative_pnl', 'account_return_fraction', 'max_close_drawdown_fraction'):
        assert Decimal(a.differences[0][key]) == -Decimal(b.differences[0][key])


def test_repeat_and_hostile_decimal_context_preserve_inputs_and_results():
    s, p = inputs(); costs = dict(commission_bps='5', slippage_bps='5')
    before = deepcopy((s, costs)); expected = run(s, p, stress_costs=costs)
    hostile = Context(prec=2, Emin=-1, Emax=1)
    for k in hostile.traps: hostile.traps[k] = True
    with localcontext(hostile):
        assert run(s, p, stress_costs=costs) == expected
    assert (s, costs) == before


@pytest.mark.parametrize('key,value', [
    ('symbol', 'QQQ'), ('first_day', '2026-01-05'), ('last_day', '2026-01-13'),
    ('price_sha256', '0'*64), ('price_count', 8), ('label_convention', 'other'),
    ('initial_cash', '101'), ('commission_bps', '5'), ('slippage_bps', '5'),
    ('trade_from', '2026-01-05'),
])
def test_scope_differences_block_before_either_candidate_runs(monkeypatch, key, value):
    s, p = inputs()
    if key == 'trade_from': s[1]['rule'][key] = value
    else: s[1][key] = value
    def forbidden(*a, **kw): raise AssertionError('must not run')
    monkeypatch.setattr(module, 'validate_candidate', forbidden)
    r = run(s, p)
    assert r.status == 'held' and r.candidates == r.differences == ()
    assert r.reason == 'different_explicit_comparison_conditions'


def test_numeric_representations_do_not_change_comparison_conditions():
    s, p = inputs()
    s[1].update(initial_cash='100.00', commission_bps='0.00', slippage_bps='0.00')
    assert run(s, p).reason == 'same_conditions_arithmetic_only'


@pytest.mark.parametrize('specs', [None, [], [{}], [{}, {}, {}], 'execute', [None, None]])
def test_arbitrary_or_unbounded_candidates_fail_without_reflecting_data(specs):
    r = compare_local_candidates(specs, b'PRIVATE_SYNTHETIC', data_kind='synthetic',
        verified_us_symbols=['SPY'], now=NOW)
    assert r.status == 'held' and not r.candidates and not r.differences
    assert 'PRIVATE_SYNTHETIC' not in repr(r)


def test_bad_common_data_or_one_invalid_rule_never_produces_comparison():
    s, p = inputs(); s[1]['rule']['id'] = 'SYNTHETIC_PRIVATE_RULE'
    r = run(s, p)
    assert r.status == 'held' and r.differences == ()
    assert 'SYNTHETIC_PRIVATE_RULE' not in json.dumps(asdict(r))
    assert run(prices=p+b'broken').status == 'held'


def test_stress_failure_does_not_compare_only_successful_baselines():
    s, p = inputs()
    for x in s: x['initial_cash'] = '8'
    r = run(s, p, stress_costs=dict(commission_bps='5', slippage_bps='5'))
    assert r.status == 'held' and not r.differences


def test_at_most_two_candidates_and_eight_existing_evaluations(monkeypatch):
    counts = []
    for key, real in list(RUNNERS.items()):
        def wrapped(*a, _real=real, **kw):
            counts.append(1); return _real(*a, **kw)
        monkeypatch.setitem(RUNNERS, key, wrapped)
    assert run(stress_costs=dict(commission_bps='5', slippage_bps='5')).differences
    assert len(counts) == 8


def test_different_rule_units_remain_exposure_limitation_not_equal_risk():
    s, p = inputs(); s[0]['rule']['units'] = '2'
    r = run(s, p)
    assert r.differences and 'different_rules_may_have_different_exposure' in r.limitations


def test_historical_declaration_does_not_certify_source_or_forward():
    r = run(data_kind='historical')
    assert r.status == 'insufficient' and not r.selection_approved
    assert all(not c.source_equivalence_verified and not c.actual_execution_verified
               for c in r.candidates)


def test_offline_no_legacy_intake_or_io_authority(monkeypatch):
    import builtins
    import socket
    from astral_stock_agent.independent_ledger import IndependentPaperLedger
    from astral_stock_agent.delivery import Telegram, Outbox
    assert run().differences  # calendar warmup, before forbidding external I/O
    def forbidden(*a, **kw): raise AssertionError('outside scope')
    monkeypatch.setattr(builtins, 'open', forbidden)
    monkeypatch.setattr(socket, 'socket', forbidden)
    monkeypatch.setattr(IndependentPaperLedger, 'append', forbidden)
    monkeypatch.setattr(Telegram, 'send', forbidden)
    monkeypatch.setattr(Outbox, 'enqueue', forbidden)
    assert run().reason == 'same_conditions_arithmetic_only'
