"""Synthetic compiler envelopes only: no private source or identifiers."""
import copy
from dataclasses import replace

import pytest

from astral_stock_agent.research_contracts import (
    LOCAL_DAILY_EXECUTION, ExecutionContract, compare_execution_conditions,
    inspect_astral_execution, require_matching_execution,
)
from astral_stock_agent.research_data import ResearchDataError


def response():
    return {'strategy': {'agent_strategy_state': {'strategy': {
        't': 'run_strategy', 'v': 2, 'mode': 'backtest',
        'engine': {'bar_size': '1d', 'fill_price_mode': 'on_close', 'timezone': 'UTC',
                   'events': ['bar_close'], 'follow_live': False},
    }}}}


def test_close_fill_cannot_be_labeled_next_open_reproduction():
    observed = inspect_astral_execution(response())
    parity = compare_execution_conditions(observed)
    assert parity.mismatched_fields == ('fill_timing',)
    assert not parity.matches
    assert not parity.full_strategy_parity_verified and not parity.deployment_approved
    with pytest.raises(ResearchDataError, match='comparison blocked'):
        require_matching_execution(observed)


def test_multiple_differences_are_all_reported_without_source_values():
    r = response()
    engine = r['strategy']['agent_strategy_state']['strategy']['engine']
    engine.update(bar_size='15m', follow_live=True)
    observed = inspect_astral_execution(r)
    assert compare_execution_conditions(observed).mismatched_fields == ('bar_size', 'fill_timing', 'follow_live')
    # A configured follow_live flag is not promoted to a live order assertion.
    assert observed.follow_live is True


def test_equal_execution_conditions_are_not_full_strategy_or_deploy_approval():
    actual = inspect_astral_execution(response())
    r = require_matching_execution(actual, actual)
    assert r.matches and r.mismatched_fields == ()
    assert r.full_strategy_parity_verified is False and r.deployment_approved is False
    r = require_matching_execution(LOCAL_DAILY_EXECUTION)
    assert r.matches and not r.deployment_approved


@pytest.mark.parametrize('key,value', [
    ('bar_size', '2d'), ('timezone', 'secret-timezone'), ('events', []),
    ('events', 'bar_close'), ('events', ['bar_close', 'bar_close']),
    ('events', ['unknown-secret-event']), ('follow_live', 1), ('follow_live', None),
    ('fill_price_mode', 'next_open'), ('fill_price_mode', None),
])
def test_unknown_engine_conditions_not_mapped_to_local_defaults(key, value):
    r = response()
    r['strategy']['agent_strategy_state']['strategy']['engine'][key] = value
    with pytest.raises(ResearchDataError) as exc:
        inspect_astral_execution(r)
    assert str(exc.value) == 'unsupported or incomplete Astral execution conditions'


@pytest.mark.parametrize('key,value', [('t', 'unknown'), ('v', True), ('v', 3),
                                       ('mode', 'live'), ('mode', 'paper')])
def test_run_version_and_mode_are_explicit(key, value):
    r = response()
    r['strategy']['agent_strategy_state']['strategy'][key] = value
    with pytest.raises(ResearchDataError):
        inspect_astral_execution(r)


@pytest.mark.parametrize('path', [
    ('strategy',), ('strategy', 'agent_strategy_state'),
    ('strategy', 'agent_strategy_state', 'strategy'),
    ('strategy', 'agent_strategy_state', 'strategy', 'engine'),
    ('strategy', 'agent_strategy_state', 'strategy', 'engine', 'follow_live'),
    ('strategy', 'agent_strategy_state', 'strategy', 'engine', 'fill_price_mode'),
])
def test_missing_fields_fail_closed(path):
    r = response()
    target = r
    for part in path[:-1]:
        target = target[part]
    del target[path[-1]]
    with pytest.raises(ResearchDataError):
        inspect_astral_execution(r)


@pytest.mark.parametrize('r', [None, [], {'strategy': None}])
def test_malformed_envelope_is_sanitized(r):
    with pytest.raises(ResearchDataError):
        inspect_astral_execution(r)


def test_raw_strategy_and_identifiers_never_retained_or_mutated():
    r = response()
    r['strategy']['strategy_code'] = 'PRIVATE_SOURCE_SENTINEL'
    r['strategy']['user_id'] = 'PRIVATE_ID_SENTINEL'
    before = copy.deepcopy(r)
    observed = inspect_astral_execution(r)
    assert r == before
    assert 'PRIVATE' not in repr(observed)
    assert 'PRIVATE' not in repr(compare_execution_conditions(observed))


@pytest.mark.parametrize('field,value', [
    ('bar_size', '15m'), ('fill_timing', 'same_bar_close'),
    ('timezone', 'America/New_York'), ('decision_events', ('bar_open',)), ('follow_live', True),
])
def test_each_condition_can_independently_block_comparison(field, value):
    changed = replace(LOCAL_DAILY_EXECUTION, **{field: value})
    assert compare_execution_conditions(changed).mismatched_fields == (field,)


def test_untyped_contracts_rejected():
    with pytest.raises(ResearchDataError):
        compare_execution_conditions({})
    with pytest.raises(ResearchDataError):
        ExecutionContract('1d', 'unknown', 'UTC', ('bar_close',), False)


@pytest.fixture
def comparison_kwargs():
    from datetime import date, datetime, timezone
    from decimal import Decimal
    from astral_stock_agent.research_data import DailyBar, DailyHistory
    from astral_stock_agent.research_sessions import UTC_SESSION_DATE
    bars = tuple(DailyBar(datetime(2026, 1, d, tzinfo=timezone.utc), Decimal(10),
                          Decimal(12), Decimal(8), Decimal(11), Decimal(100)) for d in (2, 5, 6))
    return dict(history=DailyHistory('SPY', bars, 'synthetic'), window=2, units='1',
                trade_from=date(2026, 1, 2), initial_cash=100, commission_bps=0, slippage_bps=0,
                first_day=date(2026, 1, 2), last_day=date(2026, 1, 6),
                now=datetime(2026, 1, 7, tzinfo=timezone.utc), label_convention=UTC_SESSION_DATE)


def test_mismatched_external_execution_blocks_before_simulation(comparison_kwargs, monkeypatch):
    import astral_stock_agent.research_trend as module
    def unexpected_simulation(*args, **kwargs):
        pytest.fail('execution comparison must block before simulation')
    monkeypatch.setattr(module, 'simulate_next_open', unexpected_simulation)
    with pytest.raises(ResearchDataError, match='comparison blocked'):
        module.compare_sma_candidate(**comparison_kwargs, external_execution=inspect_astral_execution(response()))


def test_matching_subset_recorded_without_strategy_approval(comparison_kwargs):
    from astral_stock_agent.research_trend import compare_sma_candidate
    result = compare_sma_candidate(**comparison_kwargs, external_execution=LOCAL_DAILY_EXECUTION)
    assert result.execution_conditions.matches
    assert not result.execution_conditions.full_strategy_parity_verified
    assert not result.execution_conditions.deployment_approved and not result.deployment_approved


def test_no_external_contract_cannot_claim_external_validation(comparison_kwargs):
    from astral_stock_agent.research_trend import compare_sma_candidate
    assert compare_sma_candidate(**comparison_kwargs).execution_conditions is None
