"""Generic timing semantics and fresh import isolation; no provider calls."""
from dataclasses import asdict, FrozenInstanceError, replace
import os
from pathlib import Path
import subprocess
import sys

import pytest

from astral_stock_agent.execution_contracts import (
    ExecutionContract, ExecutionParity, LOCAL_DAILY_EXECUTION,
    compare_execution_conditions, require_matching_execution,
)
from astral_stock_agent.research_data import ResearchDataError


def test_generic_contract_equality_never_approves_strategy_or_deployment():
    result = require_matching_execution(LOCAL_DAILY_EXECUTION)
    assert asdict(result) == dict(matches=True, mismatched_fields=(),
        full_strategy_parity_verified=False, deployment_approved=False)
    with pytest.raises(FrozenInstanceError):
        result.deployment_approved = True


@pytest.mark.parametrize('field,value', [
    ('bar_size', '15m'), ('fill_timing', 'same_bar_close'),
    ('timezone', 'America/New_York'), ('decision_events', ('bar_open',)),
    ('follow_live', True),
])
def test_each_generic_mismatch_retains_existing_rejection(field, value):
    observed = replace(LOCAL_DAILY_EXECUTION, **{field: value})
    result = compare_execution_conditions(observed)
    assert not result.matches and result.mismatched_fields == (field,)
    assert not result.full_strategy_parity_verified and not result.deployment_approved
    with pytest.raises(ResearchDataError, match='execution conditions differ'):
        require_matching_execution(observed)


def test_historical_public_imports_resolve_to_same_types_and_functions():
    from astral_stock_agent import research_contracts as historical
    for name in ('ExecutionContract', 'ExecutionParity', 'LOCAL_DAILY_EXECUTION',
                 'compare_execution_conditions', 'require_matching_execution'):
        assert getattr(historical, name) is globals()[name]
    assert isinstance(LOCAL_DAILY_EXECUTION, historical.ExecutionContract)
    assert isinstance(compare_execution_conditions(LOCAL_DAILY_EXECUTION), historical.ExecutionParity)


@pytest.mark.parametrize('module', [
    'research_trend', 'independent_signals', 'independent_reversal',
    'independent_validation', 'research_holdout', 'paper_worker', 'host_storage',
])
def test_independent_imports_cannot_load_historical_interpreter_or_service(module):
    # A new interpreter per entrypoint; a previously imported module cannot hide
    # a dependency. Do not forward operating secrets or enable any runtime.
    script = '''
import importlib, importlib.abc, sys
blocked = {'research_contracts', 'astral', 'public_discovery', 'authoring',
           'preflight', 'activity', 'status_runner', 'account_observation'}
class BlockHistorical(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith('astral_stock_agent.') and fullname.split('.')[1] in blocked:
            raise AssertionError('historical module import blocked')
sys.meta_path.insert(0, BlockHistorical())
def deny_network(event, args):
    if event.startswith('socket.'):
        raise AssertionError('network forbidden')
sys.addaudithook(deny_network)
importlib.import_module('astral_stock_agent.' + sys.argv[1])
assert all('astral_stock_agent.' + name not in sys.modules for name in blocked)
'''
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, '-c', script, module],
        env=dict(PATH=os.defpath, PYTHONPATH=str(root/'src'), PYTHONDONTWRITEBYTECODE='1'),
        capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, 'independent import boundary failed'
    assert result.stdout == result.stderr == ''
