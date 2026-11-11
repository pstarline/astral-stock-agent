"""Operator inspection must never claim, send, repair or expose report content."""
import json
import sqlite3

import pytest

from astral_stock_agent import cli
from astral_stock_agent.delivery import DeliveryError, Outbox


def seed(path):
    Outbox(path, create=True)
    with sqlite3.connect(path) as db:
        for state in ('pending', 'sending', 'unknown', 'sent'):
            db.execute('INSERT INTO reports VALUES (?, ?, ?, ?)',
                       ('private-key-' + state, 'private-body', state, 'private-message-id'))


def test_readonly_summary_preserves_bytes_and_hides_private_values(tmp_path):
    path = tmp_path / 'reports.sqlite'
    seed(path)
    before = path.read_bytes()
    counts = Outbox(path, read_only=True).status_counts()
    assert counts == dict.fromkeys(('pending', 'sending', 'unknown', 'sent'), 1)
    counts['sent'] = 999  # Returned values cannot edit the ledger.
    assert Outbox(path, read_only=True).status_counts()['sent'] == 1
    assert path.read_bytes() == before
    assert not list(tmp_path.glob('*-journal'))


def test_readonly_sqlite_connection_refuses_writes(tmp_path):
    path = tmp_path / 'reports.sqlite'
    seed(path)
    box = Outbox(path, read_only=True)
    with pytest.raises(sqlite3.OperationalError):
        box.enqueue('another', 'new body')
    assert box.state('another') is None


@pytest.mark.parametrize('read_only', [True, 1, 'yes'])
def test_creation_cannot_be_combined_with_readonly(tmp_path, read_only):
    path = tmp_path / 'missing.sqlite'
    with pytest.raises(DeliveryError):
        Outbox(path, create=True, read_only=read_only)
    assert not path.exists()


@pytest.mark.parametrize('state,expected_code', [('pending', 2), ('sent', 0), ('sending', 2), ('unknown', 2)])
def test_cli_counts_and_manual_reconciliation_signal(tmp_path, monkeypatch, capsys, state, expected_code):
    monkeypatch.delenv('GITHUB_ACTIONS', raising=False)
    path = tmp_path / 'reports.sqlite'
    seed(path)
    with sqlite3.connect(path) as db:
        db.execute('DELETE FROM reports WHERE state != ?', (state,))
    before = path.read_bytes()
    def forbidden(*a, **kw):
        pytest.fail('inspection attempted delivery')
    monkeypatch.setattr(cli, 'Telegram', forbidden)
    monkeypatch.setattr(Outbox, 'deliver', forbidden)
    assert cli.main(['inspect-outbox', '--database', str(path)]) == expected_code
    result = json.loads(capsys.readouterr().out)
    assert result['counts'][state] == 1
    assert sum(result['counts'].values()) == 1
    assert result['operator_reconciliation_required'] == (expected_code == 2)
    assert 'private-' not in json.dumps(result)
    assert path.read_bytes() == before


@pytest.mark.parametrize('state', ['pending', 'sending', 'unknown', 'sent'])
def test_inspection_agrees_with_restart_that_skips_existing_daily_report(
        tmp_path, monkeypatch, capsys, state):
    import asyncio
    from astral_stock_agent.account_status import account_status_key
    from astral_stock_agent.status_runner import run_account_status
    from test_account_observation import NOW, bundle

    monkeypatch.delenv('GITHUB_ACTIONS', raising=False)
    path = tmp_path / 'reports.sqlite'
    box = Outbox(path, create=True)
    account = bundle()['account_ref']
    report_key = account_status_key('astral_mcp', account, NOW)
    # Model a crash after enqueue, or a previous attempt whose state is known.
    box.enqueue(report_key, 'synthetic private body')
    with sqlite3.connect(path) as db:
        db.execute('UPDATE reports SET state=?', (state,))
    before = path.read_bytes()
    async def forbidden_read(*args):
        pytest.fail('restart retried collection')
    class ForbiddenSender:
        def send(self, body):
            pytest.fail('restart retried delivery')
    monkeypatch.setattr(cli, 'Telegram', lambda *args: pytest.fail('inspection opened sender'))

    result = asyncio.run(run_account_status(forbidden_read, account_ref=account,
        strategy_ref=None, verified_us_symbols=['SPY'], database=path,
        sender=ForbiddenSender(), clock=lambda: NOW))
    assert result['new_attempt'] is False
    assert result['operator_reconciliation_required'] is (state != 'sent')
    code = cli.main(['inspect-outbox', '--database', str(path)])
    inspection = json.loads(capsys.readouterr().out)
    assert inspection['operator_reconciliation_required'] == result['operator_reconciliation_required']
    assert code == (0 if state == 'sent' else 2)
    assert inspection['counts'][state] == 1
    assert account not in json.dumps(inspection) and 'synthetic private body' not in json.dumps(inspection)
    assert path.read_bytes() == before


def test_empty_initialized_ledger_is_not_readiness_proof(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv('GITHUB_ACTIONS', raising=False)
    path = tmp_path / 'reports.sqlite'
    Outbox(path, create=True)
    assert cli.main(['inspect-outbox', '--database', str(path)]) == 0
    assert sum(json.loads(capsys.readouterr().out)['counts'].values()) == 0


@pytest.mark.parametrize('kind', ['missing', 'empty', 'corrupt', 'permissions', 'symlink'])
def test_cli_rejects_unavailable_private_storage_without_disclosing_it(tmp_path, monkeypatch, capsys, kind):
    monkeypatch.delenv('GITHUB_ACTIONS', raising=False)
    path = tmp_path / 'private-path.sqlite'
    if kind in ('empty', 'corrupt'):
        path.write_text('private-body' if kind == 'corrupt' else '')
        path.chmod(0o600)
    elif kind in ('permissions', 'symlink'):
        seed(path)
        if kind == 'permissions':
            path.chmod(0o644)
        else:
            target = tmp_path / 'target.sqlite'
            path.rename(target)
            path.symlink_to(target)
    before = path.read_bytes() if path.exists() else None
    assert cli.main(['inspect-outbox', '--database', str(path)]) == 2
    assert capsys.readouterr().out == 'Outbox inspection blocked: private initialized storage required\n'
    if before is None:
        assert not path.exists()
    else:
        assert path.read_bytes() == before


def test_ci_blocked_before_storage_access(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    def forbidden(*a, **kw):
        pytest.fail('CI accessed operational storage')
    monkeypatch.setattr(cli, 'Outbox', forbidden)
    assert cli.main(['inspect-outbox', '--database', str(tmp_path / 'private.sqlite')]) == 2
    assert 'blocked' in capsys.readouterr().out


def test_invalid_legacy_state_is_not_summarized_as_healthy(tmp_path):
    path = tmp_path / 'reports.sqlite'
    seed(path)
    with sqlite3.connect(path) as db:
        db.execute('PRAGMA ignore_check_constraints=ON')
        db.execute("UPDATE reports SET state='unexpected'")
    with pytest.raises(DeliveryError, match='invalid state'):
        Outbox(path, read_only=True).status_counts()


def test_readonly_delivery_cannot_claim_or_send(tmp_path):
    path = tmp_path / 'reports.sqlite'
    seed(path)
    class ForbiddenSender:
        def send(self, body):
            pytest.fail('read-only inspection sent a report')
    with pytest.raises(sqlite3.OperationalError):
        Outbox(path, read_only=True).deliver('private-key-pending', ForbiddenSender())
    assert Outbox(path, read_only=True).state('private-key-pending') == 'pending'
