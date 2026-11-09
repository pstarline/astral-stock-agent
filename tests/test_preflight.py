"""Synthetic prerequisites; no private IDs, balances, credentials or delivery."""
import asyncio
import copy
import json
from datetime import datetime, timedelta, timezone

import pytest

from astral_stock_agent.preflight import TOOLS, collect_preflight, normalize_preflight, render_preflight
from astral_stock_agent.report import InvalidSnapshot
from astral_stock_agent.cli import main


NOW = datetime(2026, 11, 6, 22, tzinfo=timezone.utc)


def bundle():
    accounts = {'count': 1, 'broker_accounts': [{'account_id': 'private-account-sentinel',
        'account_type': 'paper', 'provider': 'astral_paper', 'account_status': 'active',
        'connection_status': 'active', 'verified': True, 'currency': 'USD',
        'supported_asset_types': ['stock'], 'available_for_strategy_deployment': True}]}
    results = {'accounts': accounts, 'deployments': {'status': 'ok', 'active_strategies': []}}
    return {'version': 1, 'source': 'fixture', 'account_ref': 'private-account-sentinel',
            'strategy_ref': None, 'responses': {
                name: {'tool': tool, 'arguments': {}, 'received_at': NOW.isoformat(), 'result': results[name]}
                for name, tool in TOOLS.items()}}


def check(b):
    return normalize_preflight(b, verified_us_symbols=['SPY'], now=NOW)


def active_bundle(*, crypto=False):
    b = bundle()
    b['strategy_ref'] = 11
    a = b['responses']['accounts']['result']['broker_accounts'][0]
    a['active_deployment'] = {'strategy_id': 11, 'active_strategy_id': 22}
    a['available_for_strategy_deployment'] = False
    b['responses']['deployments']['result']['active_strategies'] = [{
        'id': 22, 'strategy_id': 11, 'broker_account_id': 'private-account-sentinel',
        'execution_mode': 'astral_paper', 'has_crypto': crypto, 'lifecycle_state': 'active',
        'runtime_health': 'ok', 'ready_to_trade': True, 'assets': ['BTCUSD'] if crypto else ['SPY']}]
    return b


def test_idle_account_is_not_zero_trading_or_daily_report():
    b = bundle()
    before = copy.deepcopy(b)
    r = check(b)
    assert r['blocking_reasons'] == ['no_active_deployment']
    assert r['account_verified'] and not r['us_paper_deployment_verified']
    for field in ['new_orders_allowed', 'ledger_reconciled', 'complete_daily_snapshot', 'telegram_ready']:
        assert r[field] is False
    assert not any(key in r for key in ['orders', 'fills', 'pnl', 'cash'])
    assert 'private-account-sentinel' not in json.dumps(r)
    assert 'private-account-sentinel' not in render_preflight(r)
    assert b == before


def test_healthy_deployment_still_does_not_grant_orders_or_reporting():
    r = check(active_bundle())
    assert r['us_paper_deployment_verified']
    assert r['blocking_reasons'] == ['daily_ledger_and_delivery_unverified']
    assert not r['new_orders_allowed'] and not r['telegram_ready']


def test_existing_crypto_not_replaced_or_accepted_as_us_equity():
    b = active_bundle(crypto=True)
    before = copy.deepcopy(b)
    assert check(b)['blocking_reasons'] == ['intended_us_paper_deployment_unverified']
    assert b == before


def test_never_auto_select_existing_strategy():
    b = active_bundle()
    b['strategy_ref'] = None
    assert check(b)['blocking_reasons'] == ['configured_strategy_missing']


@pytest.mark.parametrize('field,value', [('account_type', 'live'), ('provider', 'other'),
    ('account_status', 'unknown'), ('connection_status', 'inactive'), ('verified', 1),
    ('currency', 'KRW'), ('supported_asset_types', ['crypto'])])
def test_unverified_accounts_fail_closed(field, value):
    b = bundle()
    b['responses']['accounts']['result']['broker_accounts'][0][field] = value
    with pytest.raises(InvalidSnapshot):
        check(b)


@pytest.mark.parametrize('offset', [-301, 1])
def test_stale_or_future_receipts(offset):
    b = bundle()
    b['responses']['accounts']['received_at'] = (NOW + timedelta(seconds=offset)).isoformat()
    with pytest.raises(InvalidSnapshot):
        check(b)


def test_skewed_receipts_rejected():
    b = bundle()
    b['responses']['accounts']['received_at'] = (NOW - timedelta(seconds=61)).isoformat()
    with pytest.raises(InvalidSnapshot):
        check(b)


@pytest.mark.parametrize('field,value', [('has_more', True), ('rows_omitted_from_model', 1),
                                       ('delivery', 'download'), ('status', 'needs_correction')])
def test_incomplete_or_unsuccessful_responses_are_not_idle_account(field, value):
    b = bundle()
    b['responses']['accounts']['result'][field] = value
    with pytest.raises(InvalidSnapshot):
        check(b)


@pytest.mark.parametrize('rows', [None, [{}], [{'broker_account_id': None}]])
def test_malformed_deployments_are_not_an_empty_complete_list(rows):
    b = bundle()
    b['responses']['deployments']['result']['active_strategies'] = rows
    with pytest.raises(InvalidSnapshot):
        check(b)


def test_wrong_tool_or_arguments_rejected():
    for field, value in [('tool', 'astral_order_submit'), ('arguments', {'limit': 200})]:
        b = bundle()
        b['responses']['accounts'][field] = value
        with pytest.raises(InvalidSnapshot):
            check(b)


def test_collector_only_reads_accounts_and_deployments():
    b = bundle()
    calls = []
    async def actual_callback_name_contract(tool, args):
        calls.append((tool, args))
        return {'structuredContent': next(row['result'] for row in b['responses'].values() if row['tool'] == tool)}
    r = asyncio.run(collect_preflight(actual_callback_name_contract, account_ref=b['account_ref'],
        strategy_ref=None, verified_us_symbols=['SPY'], clock=lambda: NOW))
    assert set(tool for tool, _ in calls) == set(TOOLS.values()) and len(calls) == 2
    assert all(args == {} for _, args in calls)
    assert r['source'] == 'astral_mcp' and r['blocking_reasons'] == ['no_active_deployment']


def test_collector_error_omits_private_exception():
    async def fail(tool, args):
        raise RuntimeError('private-token-sentinel')
    with pytest.raises(InvalidSnapshot) as exc:
        asyncio.run(collect_preflight(fail, account_ref='private-account-sentinel', strategy_ref=None,
                                     verified_us_symbols=[], clock=lambda: NOW))
    assert str(exc.value) == 'Paper prerequisite read failed'


@pytest.mark.parametrize('extra', [[], ['--send'], ['--activity'], ['--reconcile']])
def test_cli_preview_and_delivery_boundary(tmp_path, capsys, extra):
    path = tmp_path / 'bundle.json'
    path.write_text(json.dumps(bundle()))
    code = main([str(path), '--preflight', '--us-symbol', 'SPY', *extra])
    output = capsys.readouterr().out
    assert code == (2 if extra else 0)
    assert 'private-account-sentinel' not in output
    if not extra:
        assert '실제 거래 기록 아님' in output and '0이라는 뜻이 아닙니다' in output
    assert not (tmp_path / 'reports.sqlite').exists()


def test_unavailable_account_not_called_ready():
    b = bundle()
    b['responses']['accounts']['result']['broker_accounts'][0]['available_for_strategy_deployment'] = False
    assert check(b)['blocking_reasons'] == ['account_availability_unverified']


def test_account_count_mismatch_and_duplicate_identity_rejected():
    b = bundle()
    b['responses']['accounts']['result']['count'] = 2
    with pytest.raises(InvalidSnapshot):
        check(b)
    a = b['responses']['accounts']['result']
    a['broker_accounts'].append(copy.deepcopy(a['broker_accounts'][0]))
    with pytest.raises(InvalidSnapshot):
        check(b)
