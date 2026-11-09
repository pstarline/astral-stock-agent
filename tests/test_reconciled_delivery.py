"""Synthetic operational-shaped evidence with fake sender; no real transmission."""
import copy
import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

import astral_stock_agent.cli as cli
from astral_stock_agent.delivery import Outbox


@pytest.fixture
def environment(tmp_path, monkeypatch):
    evidence = json.loads((Path(__file__).parents[1] / 'examples/daily_reconciliation.json').read_text())
    # Only these isolated tests use an operational source label. The checked-in
    # example stays fixture, and Telegram is replaced BEFORE any CLI execution.
    for section in ('previous', 'current', 'accounting'):
        evidence[section]['source'] = 'astral_mcp'
    instant = datetime.fromisoformat(evidence['current']['collected_at'].replace('Z', '+00:00'))

    class FixedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant

    class FakeSender:
        def __init__(self):
            self.calls = []
            self.constructed = 0
            self.failure = None

        def send(self, body):
            self.calls.append(body)
            if self.failure:
                raise self.failure
            return '42'

    sender = FakeSender()
    def factory(*args):
        sender.constructed += 1
        return sender

    monkeypatch.setattr(cli, 'Telegram', factory)
    monkeypatch.setattr(cli, 'datetime', FixedClock)
    monkeypatch.delenv('GITHUB_ACTIONS', raising=False)
    path = tmp_path / 'evidence.json'
    database = tmp_path / 'reports.sqlite'
    Outbox(database, create=True)
    return evidence, path, database, sender


def invoke(environment, *, reconcile=True):
    evidence, path, database, sender = environment
    path.write_text(json.dumps(evidence))
    args = [str(path), '--send', '--us-symbol', 'SPY', '--database', str(database)]
    if reconcile:
        args.append('--reconcile')
    return cli.main(args)


def rows(database):
    with sqlite3.connect(database) as db:
        return db.execute('SELECT * FROM reports ORDER BY report_key').fetchall()


def test_complete_evidence_reconciles_then_fake_delivery_and_restart_deduplicates(environment):
    evidence, path, database, sender = environment
    before = copy.deepcopy(evidence)
    assert invoke(environment) == 0
    assert len(sender.calls) == 1
    assert '당일 총손익 (자금 유출입 제외): $27.00' in sender.calls[0]
    assert '당일 실현 손익' in sender.calls[0]
    assert rows(database)[0][2:] == ('sent', '42')
    assert invoke(environment) == 0  # new Outbox instance in the CLI
    assert len(sender.calls) == 1
    assert evidence == before and json.loads(path.read_text()) == before


@pytest.mark.parametrize('section,field,value', [
    ('accounting', 'execution_fees_usd', None),
    ('accounting', 'net_capital_flow_usd', None),
    ('accounting', 'complete', False),
    ('accounting', 'source', 'fixture'),
    ('previous', 'source', 'fixture'),
    ('current', 'source', 'fixture'),
    ('current', 'cash_usd', '908'),
    ('current', 'daily_total_pnl_usd', '999'),
    ('current', 'collected_at', '2026-11-06T20:00:00Z'),
    ('current', 'run_ref', 'unmatched-run'),
])
def test_missing_or_conflicting_evidence_never_touches_sender_or_outbox(environment, section, field, value):
    evidence, path, database, sender = environment
    Outbox(database).enqueue('preserved', 'existing pending report')
    before = rows(database)
    evidence[section][field] = value
    assert invoke(environment) == 2
    assert sender.constructed == 0 and not sender.calls
    assert rows(database) == before


def test_all_fixture_evidence_cannot_send_even_with_clock_matching_fixture(environment):
    evidence, path, database, sender = environment
    for section in ('previous', 'current', 'accounting'):
        evidence[section]['source'] = 'fixture'
    assert invoke(environment) == 2
    assert not sender.calls and rows(database) == []


def test_raw_snapshot_send_cannot_bypass_reconciliation(environment):
    evidence, path, database, sender = environment
    environment = (evidence['current'], path, database, sender)
    assert invoke(environment, reconcile=False) == 2
    assert sender.constructed == 0 and rows(database) == []


def test_public_ci_blocks_complete_operational_shape_before_sender(environment, monkeypatch):
    monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    assert invoke(environment) == 2
    assert environment[3].constructed == 0 and rows(environment[2]) == []


def test_reconciliation_failure_does_not_relabel_existing_sent_report(environment):
    assert invoke(environment) == 0
    evidence, path, database, sender = environment
    before = rows(database)
    evidence['accounting']['execution_fees_usd'] = None
    assert invoke(environment) == 2
    assert len(sender.calls) == 1 and rows(database) == before


@pytest.mark.parametrize('state', ['sending', 'unknown'])
def test_ambiguous_or_interrupted_delivery_not_retried(environment, state):
    sender = environment[3]
    sender.failure = TimeoutError('private-token-sentinel') if state == 'unknown' else SystemExit('interrupted')
    if state == 'sending':
        with pytest.raises(SystemExit):
            invoke(environment)
    else:
        assert invoke(environment) == 2
    assert rows(environment[2])[0][2] == state
    sender.failure = None
    assert invoke(environment) == 2
    assert len(sender.calls) == 1 and rows(environment[2])[0][2] == state


def test_changed_valid_report_body_still_requires_operator_reconciliation(environment):
    assert invoke(environment) == 0
    before = rows(environment[2])
    environment[0]['current']['strategy_name'] = 'changed synthetic display name'
    assert invoke(environment) == 2
    assert len(environment[3].calls) == 1 and rows(environment[2]) == before


def test_missing_ledger_is_not_initialized_by_reconciled_send(environment):
    environment[2].unlink()
    assert invoke(environment) == 2
    assert not environment[2].exists() and not environment[3].calls
