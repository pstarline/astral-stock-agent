"""Synthetic account alerts with fake delivery only; no real account data."""
import copy
import json
from datetime import datetime, timedelta

import pytest

import astral_stock_agent.cli as cli
from astral_stock_agent.account_status import render_account_status
from astral_stock_agent.delivery import DeliveryError, Outbox
from test_account_observation import bundle, NOW


@pytest.fixture
def environment(tmp_path, monkeypatch):
    value = bundle()
    # Operational-shaped synthetic data; fake transport installed before use.
    value['source'] = 'astral_mcp'
    class FixedClock(datetime):
        @classmethod
        def now(cls, tz=None): return NOW
    class Sender:
        def __init__(self): self.calls, self.constructed, self.failure = [], 0, None
        def send(self, body):
            self.calls.append(body)
            if self.failure: raise self.failure
            return '42'
    sender = Sender()
    def factory(*args):
        sender.constructed += 1
        return sender
    monkeypatch.setattr(cli, 'Telegram', factory)
    monkeypatch.setattr(cli, 'datetime', FixedClock)
    monkeypatch.delenv('GITHUB_ACTIONS', raising=False)
    path, db = tmp_path/'input.json', tmp_path/'outbox.sqlite'
    Outbox(db, create=True)
    return value, path, db, sender


def invoke(env, *, send=True, extra=()):
    value, path, db, sender = env
    path.write_text(json.dumps(value))
    args = [str(path), '--account-status', '--us-symbol', 'SPY', *extra]
    if send: args += ['--send', '--database', str(db)]
    return cli.main(args)


def test_report_has_known_cash_but_no_invented_accounting_or_ids():
    value = bundle(); before = copy.deepcopy(value)
    key, body = render_account_status(value, verified_us_symbols=['SPY'], now=NOW)
    assert '900.25 USD' in body and '매매 성과 보고 아님' in body
    assert '활성 배포 없음' in body and '손익 (실현/미실현): 미확인' in body
    assert '주문·체결·보유 종목: 미확인' in body
    assert '거래일 아님' in body and '신규 주문 권한 없음' in body
    assert 'synthetic-private-reference' not in body and len(key) == 64
    assert value == before


def test_fake_delivery_restarts_without_duplicate_and_does_not_touch_books(environment, monkeypatch):
    def forbidden(*args, **kwargs): raise AssertionError('Status alert cannot access closing books')
    monkeypatch.setattr(cli, 'ClosingBooks', forbidden)
    monkeypatch.setattr(cli, 'reconcile_daily', forbidden)
    value, path, db, sender = environment
    assert invoke(environment) == 0 and len(sender.calls) == 1
    assert invoke(environment) == 0 and len(sender.calls) == 1
    assert Outbox(db).status_counts()['sent'] == 1


def test_timeout_remains_unknown_and_never_retries(environment):
    value, path, db, sender = environment
    sender.failure = DeliveryError('synthetic timeout')
    assert invoke(environment) == 2 and Outbox(db).status_counts()['unknown'] == 1
    sender.failure = None
    assert invoke(environment) == 2 and len(sender.calls) == 1


def test_changed_same_day_observation_is_not_overwritten_or_resent(environment):
    value, path, db, sender = environment
    assert invoke(environment) == 0
    value['responses']['accounts']['result']['broker_accounts'][0]['cash'] = '901.25'
    assert invoke(environment) == 2 and len(sender.calls) == 1
    assert Outbox(db).status_counts()['sent'] == 1


@pytest.mark.parametrize('problem', ['fixture', 'stale', 'live', 'wrong_account', 'bad_cash'])
def test_untrusted_or_invalid_status_never_constructs_sender_or_enqueues(environment, problem):
    value, path, db, sender = environment
    account = value['responses']['accounts']['result']['broker_accounts'][0]
    if problem == 'fixture': value['source'] = 'fixture'
    elif problem == 'stale': value['responses']['accounts']['received_at'] = (NOW-timedelta(minutes=6)).isoformat()
    elif problem == 'live': account['account_type'] = 'live'
    elif problem == 'wrong_account': value['account_ref'] = 'wrong-private-reference'
    else: account['cash'] = 'NaN'
    assert invoke(environment) == 2 and sender.constructed == 0 and not sender.calls
    assert not any(Outbox(db).status_counts().values())


@pytest.mark.parametrize('extra', [['--activity'], ['--preflight'], ['--account-observation'],
                                  ['--reconcile'], ['--book-directory', 'PRIVATE_PATH']])
def test_mixed_modes_cannot_turn_partial_data_into_performance_report(environment, extra):
    value, path, db, sender = environment
    assert invoke(environment, extra=extra) == 2 and sender.constructed == 0
    assert not any(Outbox(db).status_counts().values())


def test_public_ci_cannot_send_or_print_private_amounts(environment, monkeypatch, capsys):
    value, path, db, sender = environment
    monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    assert invoke(environment) == 2 and sender.constructed == 0
    assert '900.25' not in capsys.readouterr().out
    assert invoke(environment, send=False) == 2
    assert '900.25' not in capsys.readouterr().out


def test_fixture_can_be_previewed_without_sender(environment, capsys):
    value, path, db, sender = environment
    value['source'] = 'fixture'
    assert invoke(environment, send=False) == 0
    assert '검증용 예제' in capsys.readouterr().out and sender.constructed == 0


def test_keys_use_korean_notification_day_and_distinct_mode_namespace():
    value = bundle()
    first, _ = render_account_status(value, verified_us_symbols=['SPY'], now=NOW)
    later = NOW + timedelta(days=1)
    for row in value['responses'].values(): row['received_at'] = later.isoformat()
    second, _ = render_account_status(value, verified_us_symbols=['SPY'], now=later)
    assert first != second
    # Existing performance key has source/account/strategy/trading-day inputs,
    # while this mode has its explicit notification namespace and no strategy.
    import hashlib
    old = hashlib.sha256(json.dumps(['fixture', value['account_ref'], 'SPY', '2026-11-09']).encode()).hexdigest()
    assert first != old


def test_duplicate_json_is_rejected_before_render_or_delivery(environment, monkeypatch):
    value, path, db, sender = environment
    path.write_text('{"source":"fixture","source":"astral_mcp"}')
    def forbidden(*args, **kwargs): raise AssertionError('Ambiguous input cannot render')
    monkeypatch.setattr(cli, 'render_account_status', forbidden)
    assert cli.main([str(path), '--account-status', '--send', '--database', str(db)]) == 2
    assert sender.constructed == 0
