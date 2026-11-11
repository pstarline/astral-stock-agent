import asyncio
import copy
import json
from datetime import datetime, timedelta, timezone

import pytest

from astral_stock_agent.account_observation import (
    collect_account_observation, normalize_account_observation, render_account_observation,
)
from astral_stock_agent.cli import main
from astral_stock_agent.preflight import TOOLS
from astral_stock_agent.report import InvalidSnapshot


NOW = datetime(2026, 11, 9, 12, tzinfo=timezone.utc)


def bundle():
    account = {'account_id': 'synthetic-private-reference', 'account_type': 'paper',
               'provider': 'astral_paper', 'account_status': 'active', 'connection_status': 'active',
               'verified': True, 'currency': 'USD', 'supported_asset_types': ['stock'],
               'available_for_strategy_deployment': True, 'cash': '900.25',
               'unassigned_cash': '800.25', 'reserved_cash': '100'}
    values = {'accounts': {'count': 1, 'broker_accounts': [account]},
              'deployments': {'status': 'ok', 'active_strategies': []}}
    return {'version': 1, 'source': 'fixture', 'account_ref': account['account_id'],
            'strategy_ref': None, 'responses': {
                key: {'tool': tool, 'arguments': {}, 'received_at': NOW.isoformat(), 'result': values[key]}
                for key, tool in TOOLS.items()}}


def normalize(value):
    return normalize_account_observation(value, verified_us_symbols=['SPY'], now=NOW)


def test_known_cash_does_not_become_equity_pnl_fills_or_fresh_valuation():
    value = bundle()
    before = copy.deepcopy(value)
    result = normalize(value)
    assert result['observed_cash_usd'] == '900.25'
    assert result['unassigned_cash_usd'] == '800.25'
    assert result['reserved_cash_usd'] == '100'
    for field in ['balance_as_of', 'valuation_as_of', 'trading_day', 'equity_usd',
                  'daily_realized_pnl_usd', 'daily_unrealized_pnl_usd', 'cumulative_pnl_usd',
                  'orders', 'fills', 'positions']:
        assert result[field] is None
    for field in ['ledger_reconciled', 'complete_daily_snapshot', 'new_orders_allowed', 'telegram_ready']:
        assert result[field] is False
    text = render_account_observation(value, verified_us_symbols=['SPY'], now=NOW)
    assert '900.25 USD' in text and '거래 성과 보고 아님' in text
    assert '미확인' in text and 'synthetic-private-reference' not in text
    assert 'synthetic-private-reference' not in json.dumps(result)
    assert value == before


def test_missing_values_remain_unknown_without_zero_fill_or_substitution():
    value = bundle()
    account = value['responses']['accounts']['result']['broker_accounts'][0]
    del account['cash']
    account['reserved_cash'] = None
    result = normalize(value)
    assert result['observed_cash_usd'] is None and result['reserved_cash_usd'] is None
    assert result['unassigned_cash_usd'] == '800.25'


@pytest.mark.parametrize('bad', [True, [], {}, '-1', 'NaN', float('inf'), '1e999', 'PRIVATE_TOKEN'])
def test_invalid_cash_does_not_escape_or_become_zero(bad):
    value = bundle()
    value['responses']['accounts']['result']['broker_accounts'][0]['cash'] = bad
    with pytest.raises(InvalidSnapshot, match='^official paper account observation unavailable$'):
        normalize(value)


@pytest.mark.parametrize('field,value', [('account_type', 'live'), ('currency', 'KRW'), ('verified', 1)])
def test_account_boundary_reused(field, value):
    b = bundle()
    b['responses']['accounts']['result']['broker_accounts'][0][field] = value
    with pytest.raises(InvalidSnapshot):
        normalize(b)


def test_old_receipt_still_rejected():
    value = bundle()
    value['responses']['accounts']['received_at'] = (NOW - timedelta(minutes=6)).isoformat()
    with pytest.raises(InvalidSnapshot):
        normalize(value)


def test_collector_uses_only_official_account_and_deployment_reads():
    b = bundle()
    calls = []
    async def callback(tool, arguments):
        calls.append((tool, arguments))
        return {'structuredContent': next(row['result'] for row in b['responses'].values() if row['tool'] == tool)}
    result = asyncio.run(collect_account_observation(callback, account_ref=b['account_ref'],
        strategy_ref=None, verified_us_symbols=['SPY'], clock=lambda: NOW))
    assert set(tool for tool, _ in calls) == set(TOOLS.values()) and len(calls) == 2
    assert all(arguments == {} for _, arguments in calls)
    assert result['source'] == 'astral_mcp' and result['observed_cash_usd'] == '900.25'


@pytest.mark.parametrize('failed_tool', list(TOOLS.values()))
@pytest.mark.parametrize('cleanup_fails', [False, True])
def test_failed_read_settles_other_read_before_returning(failed_tool, cleanup_fails):
    async def scenario():
        started = asyncio.Event()
        closed = asyncio.Event()
        children, calls = [], []

        async def callback(tool, arguments):
            children.append(asyncio.current_task())
            calls.append((tool, arguments))
            if tool == failed_tool:
                await started.wait()
                raise RuntimeError('PRIVATE_PROVIDER_ERROR')
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                # Model an async transport closing its connection on cancel.
                await asyncio.sleep(0)
                closed.set()
                if cleanup_fails:
                    raise RuntimeError('PRIVATE_CLEANUP_ERROR')

        try:
            with pytest.raises(InvalidSnapshot, match='^official paper account read failed$'):
                await asyncio.wait_for(collect_account_observation(callback,
                    account_ref=bundle()['account_ref'], strategy_ref=None,
                    verified_us_symbols=['SPY'], clock=lambda: NOW), timeout=1)
            assert closed.is_set(), 'failed collection returned with a live sibling read'
            assert all(task.done() for task in children)
            assert sorted(calls) == sorted((tool, {}) for tool in TOOLS.values())
        finally:
            # Clean up even when running this regression against the old code.
            for child in children:
                if not child.done():
                    child.cancel()
            await asyncio.gather(*children, return_exceptions=True)
    asyncio.run(scenario())


def test_caller_cancellation_settles_both_reads_and_propagates():
    async def scenario():
        started = asyncio.Event()
        children, closed = [], []
        async def callback(tool, arguments):
            children.append(asyncio.current_task())
            if len(children) == 2:
                started.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                closed.append(tool)
        operation = asyncio.create_task(collect_account_observation(callback,
            account_ref=bundle()['account_ref'], strategy_ref=None,
            verified_us_symbols=['SPY'], clock=lambda: NOW))
        await asyncio.wait_for(started.wait(), timeout=1)
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation
        assert sorted(closed) == sorted(TOOLS.values())
        assert all(child.done() for child in children)
    asyncio.run(scenario())


@pytest.mark.parametrize('extra', [[], ['--send'], ['--send', '--reconcile'], ['--preflight'], ['--activity'], ['--reconcile']])
def test_cli_is_preview_only_and_never_uses_sender_or_outbox(tmp_path, capsys, monkeypatch, extra):
    from astral_stock_agent import cli
    def forbidden(*args, **kwargs):
        raise AssertionError('No delivery or outbox in observation preview')
    monkeypatch.setattr(cli, 'Telegram', forbidden)
    monkeypatch.setattr(cli, 'Outbox', forbidden)
    path = tmp_path / 'input.json'
    path.write_text(json.dumps(bundle()))
    assert main([str(path), '--account-observation', *extra]) == (2 if extra else 0)
    output = capsys.readouterr().out
    assert 'synthetic-private-reference' not in output
    assert not (tmp_path / 'reports.sqlite').exists()


def test_public_ci_rejects_real_source_before_printing_amounts(tmp_path, monkeypatch, capsys):
    value = bundle()
    value['source'] = 'astral_mcp'
    path = tmp_path / 'input.json'
    path.write_text(json.dumps(value))
    monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    assert main([str(path), '--account-observation']) == 2
    assert '900.25' not in capsys.readouterr().out
