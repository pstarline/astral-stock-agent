"""Read failures must not outlive a failed collection in a persistent loop."""
import asyncio
import copy
import json
from datetime import datetime
from pathlib import Path

import pytest

from astral_stock_agent.activity import collect_activity
from astral_stock_agent.preflight import collect_preflight
from astral_stock_agent.report import InvalidSnapshot
from test_preflight import NOW, bundle


@pytest.fixture(params=['preflight', 'activity_scope', 'activity_history'])
def collection(request):
    kind = request.param
    if kind == 'preflight':
        data = bundle()
        def invoke(callback):
            return collect_preflight(callback, account_ref=data['account_ref'],
                strategy_ref=None, verified_us_symbols=['SPY'], clock=lambda: NOW)
        names = ['accounts', 'deployments']
        message = 'Paper prerequisite read failed'
    else:
        data = json.loads((Path(__file__).parents[1] / 'examples/astral_activity.json').read_text())
        now = max(datetime.fromisoformat(row['received_at']) for row in data['responses'].values())
        def invoke(callback):
            return collect_activity(callback, account_ref=data['account_ref'],
                strategy_ref=data['strategy_ref'], verified_us_symbols=['SPY'],
                start=datetime.fromisoformat(data['start']), end=datetime.fromisoformat(data['end']),
                clock=lambda: now)
        names = ['accounts', 'deployments'] if kind == 'activity_scope' else ['orders', 'trades']
        message = 'Astral read failed'
    targets = [data['responses'][name]['tool'] for name in names]
    expected = (targets if kind != 'activity_history'
                else [row['tool'] for row in data['responses'].values()])
    return data, invoke, targets, message, expected


@pytest.mark.parametrize('failure_index', [0, 1])
@pytest.mark.parametrize('cleanup_fails', [False, True])
def test_read_failure_settles_sibling_and_does_not_start_next_stage(collection, failure_index, cleanup_fails):
    data, invoke, targets, message, expected = collection
    async def scenario():
        started, closed = asyncio.Event(), asyncio.Event()
        children, calls = [], []
        async def callback(tool, arguments):
            calls.append(tool)
            if tool not in targets:
                return copy.deepcopy(next(row['result'] for row in data['responses'].values()
                                          if row['tool'] == tool))
            children.append(asyncio.current_task())
            if tool == targets[failure_index]:
                await started.wait()
                raise RuntimeError('PRIVATE_READ_ERROR')
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                closed.set()
                if cleanup_fails:
                    raise RuntimeError('PRIVATE_CLEANUP_ERROR')
        try:
            with pytest.raises(InvalidSnapshot, match='^' + message + '$'):
                await asyncio.wait_for(invoke(callback), timeout=1)
            assert closed.is_set(), 'collection returned with a live sibling read'
            assert all(child.done() for child in children)
            assert sorted(calls) == sorted(expected)
        finally:
            for child in children:
                if not child.done():
                    child.cancel()
            await asyncio.gather(*children, return_exceptions=True)
    asyncio.run(scenario())


def test_caller_cancellation_settles_each_current_read(collection):
    data, invoke, targets, _, _ = collection
    async def scenario():
        started = asyncio.Event()
        children, closed = [], []
        async def callback(tool, arguments):
            if tool not in targets:
                return copy.deepcopy(next(row['result'] for row in data['responses'].values()
                                          if row['tool'] == tool))
            children.append(asyncio.current_task())
            if len(children) == 2:
                started.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                closed.append(tool)
        task = asyncio.create_task(invoke(callback))
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert sorted(closed) == sorted(targets)
        assert all(child.done() for child in children)
    asyncio.run(scenario())
