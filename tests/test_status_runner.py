"""Private orchestration with synthetic official-shaped reads and fake sends."""
import asyncio
import copy
from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

from astral_stock_agent.account_status import account_status_key
from astral_stock_agent.delivery import DeliveryError, Outbox
from astral_stock_agent.status_runner import run_account_status
from test_account_observation import NOW, bundle


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.delenv('GITHUB_ACTIONS', raising=False)
    data = bundle()
    path = tmp_path / 'outbox.sqlite'
    Outbox(path, create=True)
    calls, sends = [], []

    async def read(tool, arguments):
        assert arguments == {}
        calls.append(tool)
        return {'structuredContent': copy.deepcopy(next(
            row['result'] for row in data['responses'].values() if row['tool'] == tool))}

    class Sender:
        def send(self, body):
            sends.append(body)
            return '7'

    return data, path, calls, sends, read, Sender()


def invoke(runtime, *, now=NOW, clock=None, read=None, sender=None, account_ref=None):
    data, path, calls, sends, callback, default_sender = runtime
    return asyncio.run(run_account_status(
        read or callback, account_ref=account_ref or data['account_ref'], strategy_ref=None,
        verified_us_symbols=['SPY'], database=path, sender=sender or default_sender,
        clock=clock or (lambda: now)))


def key(runtime, now=NOW):
    return account_status_key('astral_mcp', runtime[0]['account_ref'], now)


def test_official_reads_flow_into_private_daily_report_without_raw_output(runtime, capsys):
    data, path, calls, sends, _, _ = runtime
    result = invoke(runtime)
    assert sorted(calls) == ['astral_broker_accounts_list', 'astral_deployments_list']
    assert len(sends) == 1 and '900.25 USD' in sends[0]
    assert '매매 성과 보고 아님' in sends[0] and '체결·보유 종목: 미확인' in sends[0]
    assert data['account_ref'] not in sends[0]
    assert result == {'state': 'sent', 'new_attempt': True, 'operator_reconciliation_required': False,
                      'recipient_receipt_verified': False, 'new_orders_allowed': False}
    assert Outbox(path).state(key(runtime)) == 'sent'
    assert '900.25' not in str(result) and data['account_ref'] not in str(result)
    assert capsys.readouterr() == ('', '')


def test_restart_skips_all_new_collection_and_delivery_even_if_clock_and_cash_changed(runtime):
    assert invoke(runtime)['new_attempt'] is True
    runtime[0]['responses']['accounts']['result']['broker_accounts'][0]['cash'] = '999.25'
    assert invoke(runtime, now=NOW + timedelta(minutes=10))['new_attempt'] is False
    assert len(runtime[2]) == 2 and len(runtime[3]) == 1


@pytest.mark.parametrize('state', ['pending', 'sending', 'unknown', 'sent'])
def test_any_existing_daily_row_is_immutable_and_not_automatically_retried(runtime, state):
    box = Outbox(runtime[1]); report_key = key(runtime)
    box.enqueue(report_key, 'private prior body')
    with sqlite3.connect(box.path) as db:
        db.execute('UPDATE reports SET state=?', (state,))
    result = invoke(runtime)
    assert result['state'] == state and result['new_attempt'] is False
    assert result['operator_reconciliation_required'] is (state != 'sent')
    assert not runtime[2] and not runtime[3]
    with sqlite3.connect(box.path) as db:
        assert db.execute('SELECT body,state FROM reports').fetchone() == ('private prior body', state)


def test_timeout_persists_unknown_then_restart_never_reads_or_resends(runtime):
    class TimeoutSender:
        def send(self, body):
            runtime[3].append(body)
            raise TimeoutError('PRIVATE_TOKEN')
    with pytest.raises(DeliveryError, match='^private account status run blocked or delivery unconfirmed$'):
        invoke(runtime, sender=TimeoutSender())
    assert Outbox(runtime[1]).state(key(runtime)) == 'unknown'
    result = invoke(runtime)
    assert result['state'] == 'unknown' and result['operator_reconciliation_required']
    assert len(runtime[2]) == 2 and len(runtime[3]) == 1


def test_process_interruption_leaves_sending_and_restart_skips(runtime):
    class Interrupted(BaseException):
        pass
    class CrashSender:
        def send(self, body):
            runtime[3].append(body)
            raise Interrupted()
    with pytest.raises(Interrupted):
        invoke(runtime, sender=CrashSender())
    assert Outbox(runtime[1]).state(key(runtime)) == 'sending'
    assert invoke(runtime)['state'] == 'sending'
    assert len(runtime[2]) == 2 and len(runtime[3]) == 1


@pytest.mark.parametrize('problem', ['live', 'unverified', 'count', 'truncated', 'error', 'cash'])
def test_invalid_official_result_never_enqueues_or_sends(runtime, problem):
    value = runtime[0]['responses']['accounts']['result']
    if problem == 'live': value['broker_accounts'][0]['account_type'] = 'live'
    elif problem == 'unverified': value['broker_accounts'][0]['verified'] = False
    elif problem == 'count': value['count'] = 2
    elif problem == 'truncated': value['truncated'] = True
    elif problem == 'error': value['error'] = 'PRIVATE_TOKEN'
    else: value['broker_accounts'][0]['cash'] = 'PRIVATE_TOKEN'
    with pytest.raises(DeliveryError, match='^private account status run blocked or delivery unconfirmed$'):
        invoke(runtime)
    assert not runtime[3] and not any(Outbox(runtime[1]).status_counts().values())


def test_callback_error_is_sanitized_and_never_retried(runtime):
    async def failed(tool, arguments):
        runtime[2].append(tool)
        raise RuntimeError('https://private.invalid/PRIVATE_TOKEN')
    with pytest.raises(DeliveryError) as error:
        invoke(runtime, read=failed)
    assert 'PRIVATE' not in str(error.value) and 'https://' not in str(error.value)
    assert len(runtime[2]) == 2 and not runtime[3]
    assert not any(Outbox(runtime[1]).status_counts().values())


def test_public_ci_blocks_before_clock_storage_reads_or_network(runtime, monkeypatch):
    import astral_stock_agent.status_runner as runner
    def forbidden(*args):
        raise AssertionError('Public CI must stop before operational access')
    monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    monkeypatch.setattr(runner, 'Outbox', forbidden)
    with pytest.raises(DeliveryError):
        invoke(runtime, clock=forbidden)
    assert not runtime[2] and not runtime[3]


def test_missing_private_ledger_never_bootstraps_or_reads(runtime):
    runtime[1].unlink()
    with pytest.raises(DeliveryError):
        invoke(runtime)
    assert not runtime[1].exists() and not runtime[2] and not runtime[3]


@pytest.mark.parametrize('clock', [lambda: datetime(2026, 11, 9, 12), lambda: 'PRIVATE_TOKEN'])
def test_invalid_clock_stops_before_collection(runtime, clock):
    with pytest.raises(DeliveryError):
        invoke(runtime, clock=clock)
    assert not runtime[2] and not runtime[3]


@pytest.mark.parametrize('last', [NOW - timedelta(seconds=1), NOW + timedelta(minutes=6)])
def test_collection_clock_regression_or_elapsed_limit_never_writes(runtime, last):
    times = iter([NOW, NOW, NOW, NOW, last])
    with pytest.raises(DeliveryError):
        invoke(runtime, clock=lambda: next(times))
    assert not runtime[3] and not any(Outbox(runtime[1]).status_counts().values())


def test_korean_midnight_crossing_requires_a_separate_fresh_run(runtime):
    before = datetime(2026, 11, 9, 14, 59, 59, tzinfo=timezone.utc)
    after = before + timedelta(seconds=2)
    times = iter([before, before, before, before, after])
    with pytest.raises(DeliveryError):
        invoke(runtime, clock=lambda: next(times))
    assert not runtime[3] and not any(Outbox(runtime[1]).status_counts().values())
    assert invoke(runtime, now=after)['new_attempt']


def test_following_korean_day_uses_new_key_without_replaying_previous_body(runtime):
    assert invoke(runtime)['new_attempt']
    assert invoke(runtime, now=NOW + timedelta(days=1))['new_attempt']
    assert len(runtime[2]) == 4 and len(runtime[3]) == 2
    assert '2026-11-09' in runtime[3][0] and '2026-11-10' in runtime[3][1]
    assert Outbox(runtime[1]).status_counts()['sent'] == 2


@pytest.mark.parametrize("different", [False, True])
def test_competing_initial_workers_send_at_most_once(runtime, different):
    data, path, calls, sends, read, sender = runtime
    async def competing():
        entered, account_reads = 0, 0
        ready = asyncio.Event()
        async def barrier(tool, arguments):
            nonlocal entered, account_reads
            entered += 1
            if tool == "astral_broker_accounts_list":
                account_reads += 1
                variant = account_reads
            else:
                variant = 0
            if entered == 4: ready.set()
            await ready.wait()
            result = await read(tool, arguments)
            if different and variant:
                result['structuredContent']['broker_accounts'][0]['cash'] = str(900 + variant)
            return result
        def once():
            return run_account_status(barrier, account_ref=data['account_ref'], strategy_ref=None,
                verified_us_symbols=['SPY'], database=path, sender=sender, clock=lambda: NOW)
        return await asyncio.gather(once(), once(), return_exceptions=True)
    results = asyncio.run(competing())
    assert sum(r['new_attempt'] for r in results if isinstance(r, dict)) == 1
    if different:
        assert sum(isinstance(r, DeliveryError) for r in results) == 1
    assert len(calls) == 4 and len(sends) == 1
    assert Outbox(path).status_counts()['sent'] == 1
