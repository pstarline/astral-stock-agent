"""Report corrections must not silently reuse an older Telegram receipt."""
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from astral_stock_agent.delivery import DeliveryError, Outbox
from astral_stock_agent.cli import main


@pytest.mark.parametrize('state', ['pending', 'sending', 'sent', 'unknown'])
def test_changed_content_blocked_without_mutating_record(tmp_path, state):
    path = tmp_path / 'reports.sqlite'
    box = Outbox(path, create=True)
    box.enqueue('private-key', 'original-private-body')
    with sqlite3.connect(path) as db:
        db.execute('UPDATE reports SET state=?, message_id=?', (state, '42' if state == 'sent' else None))
        before = db.execute('SELECT * FROM reports').fetchall()
    # Reopen to exercise persisted conflicts, not in-memory tracking.
    with pytest.raises(DeliveryError) as exc:
        Outbox(path, create=True).enqueue('private-key', 'new-private-body')
    assert str(exc.value) == 'report content conflict; operator reconciliation required'
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT * FROM reports').fetchall() == before
    Outbox(path, create=True).enqueue('private-key', 'original-private-body')
    assert box.state('private-key') == state


def test_two_workers_cannot_accept_conflicting_content(tmp_path):
    path = tmp_path / 'reports.sqlite'
    Outbox(path, create=True)
    gate = Barrier(2)

    def enqueue(body):
        box = Outbox(path, create=True)
        gate.wait(timeout=10)
        try:
            box.enqueue('same-day', body)
        except DeliveryError:
            return False, body
        return True, body

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(enqueue, ['first', 'second']))
    accepted = [body for ok, body in results if ok]
    assert len(accepted) == 1
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT body, state, message_id FROM reports').fetchall() == [(accepted[0], 'pending', None)]


@pytest.mark.parametrize('state', ['pending', 'sending', 'sent', 'unknown'])
def test_cli_conflict_never_calls_sender_or_claims_success(tmp_path, monkeypatch, capsys, state):
    import astral_stock_agent.cli as cli
    path = tmp_path / 'reports.sqlite'
    box = Outbox(path, create=True)
    box.enqueue('same-day', 'old-report')
    with sqlite3.connect(path) as db:
        db.execute('UPDATE reports SET state=?', (state,))
    snapshot = tmp_path / 'snapshot.json'
    snapshot.write_text(json.dumps({'source': 'astral_mcp'}))
    monkeypatch.delenv('GITHUB_ACTIONS', raising=False)
    monkeypatch.setattr(cli, 'render', lambda *a, **kw: ('same-day', 'corrected-report'))
    calls = []

    class Sender:
        def __init__(self, *args):
            pass

        def send(self, body):
            calls.append(body)
            return '99'

    monkeypatch.setattr(cli, 'Telegram', Sender)
    assert main([str(snapshot), '--send', '--database', str(path)]) == 2
    assert capsys.readouterr().out == 'Report blocked: invalid input, configuration, or unconfirmed delivery\n'
    assert not calls
    assert box.state('same-day') == state


def test_distinct_keys_do_not_conflict(tmp_path):
    box = Outbox(tmp_path / 'reports.sqlite', create=True)
    box.enqueue('first-day', 'first-report')
    box.enqueue('second-day', 'second-report')
    assert box.state('first-day') == box.state('second-day') == 'pending'
