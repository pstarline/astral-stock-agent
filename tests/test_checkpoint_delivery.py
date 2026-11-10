"""Synthetic checkpoint-to-report integration; Telegram is always replaced."""
import copy
import json
from datetime import datetime

import pytest

import astral_stock_agent.cli as cli
from astral_stock_agent.closing_books import ClosingBooks
from astral_stock_agent.delivery import DeliveryError
from test_reconciled_delivery import environment, rows


@pytest.fixture
def checkpoint(environment):
    evidence, path, database, sender = environment
    directory = path.parent / 'books'
    directory.mkdir(mode=0o700)
    ClosingBooks(directory).save(evidence['previous'],
        now=datetime.fromisoformat(evidence['previous']['collected_at'].replace('Z', '+00:00')))
    payload = {key: copy.deepcopy(evidence[key]) for key in ('current', 'accounting')}
    return payload, path, database, sender, directory


def invoke(value, *, send=True, extra=()):
    payload, path, database, sender, directory = value
    path.write_text(json.dumps(payload))
    args = [str(path), '--reconcile', '--book-directory', str(directory), '--us-symbol', 'SPY', *extra]
    if send:
        args += ['--send', '--database', str(database)]
    return cli.main(args)


def test_saved_baseline_reconciles_persists_and_fake_sends_once_across_restart(checkpoint):
    payload, path, database, sender, directory = checkpoint
    before = copy.deepcopy(payload)
    assert invoke(checkpoint) == 0
    assert len(list(directory.iterdir())) == 2
    assert rows(database)[0][2:] == ('sent', '42')
    assert len(sender.calls) == 1 and '$27.00' in sender.calls[0]
    assert invoke(checkpoint) == 0 and len(sender.calls) == 1
    assert payload == before and json.loads(path.read_text()) == before


def test_delivery_timeout_keeps_valid_book_and_blocks_retransmission(checkpoint):
    payload, path, database, sender, directory = checkpoint
    sender.failure = DeliveryError('synthetic ambiguous timeout')
    assert invoke(checkpoint) == 2
    assert len(list(directory.iterdir())) == 2 and rows(database)[0][2] == 'unknown'
    sender.failure = None
    assert invoke(checkpoint) == 2 and len(sender.calls) == 1
    assert rows(database)[0][2] == 'unknown'


@pytest.mark.parametrize('problem', ['missing', 'corrupt', 'scope', 'fees', 'inline_previous', 'fixture_send'])
def test_invalid_baseline_or_accounting_never_saves_or_sends(checkpoint, problem):
    payload, path, database, sender, directory = checkpoint
    baseline = next(directory.iterdir())
    if problem == 'missing': baseline.unlink()
    elif problem == 'corrupt': baseline.write_bytes(b'')
    elif problem == 'scope': payload['current']['run_ref'] = 'different-run'
    elif problem == 'fees': payload['accounting']['execution_fees_usd'] = None
    elif problem == 'inline_previous': payload['previous'] = None
    else:
        for part in payload.values(): part['source'] = 'fixture'
    before = {p.name: p.read_bytes() for p in directory.iterdir()}
    assert invoke(checkpoint) == 2
    assert not sender.calls and sender.constructed == 0 and rows(database) == []
    assert before == {p.name: p.read_bytes() for p in directory.iterdir()}


def test_explicit_storage_preview_persists_book_but_does_not_construct_sender(checkpoint):
    payload, path, database, sender, directory = checkpoint
    assert invoke(checkpoint, send=False) == 0
    assert len(list(directory.iterdir())) == 2 and sender.constructed == 0
    assert rows(database) == []


def test_checkpoint_conflict_blocks_even_an_otherwise_valid_report(checkpoint):
    payload, path, database, sender, directory = checkpoint
    assert invoke(checkpoint, send=False) == 0
    payload['current']['health'] = 'synthetic changed observation'
    before = {p.name: p.read_bytes() for p in directory.iterdir()}
    assert invoke(checkpoint) == 2 and sender.constructed == 0
    assert rows(database) == [] and before == {p.name: p.read_bytes() for p in directory.iterdir()}


def test_public_ci_blocks_before_input_or_checkpoint_access(checkpoint, monkeypatch):
    payload, path, database, sender, directory = checkpoint
    monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    def forbidden(*args, **kwargs):
        raise AssertionError('No private file access in public CI')
    monkeypatch.setattr(type(path), 'stat', forbidden)
    assert invoke(checkpoint) == 2 and sender.constructed == 0


@pytest.mark.parametrize('mode', [[], ['--activity'], ['--preflight'], ['--account-observation']])
def test_storage_is_not_silently_accepted_in_other_modes(checkpoint, monkeypatch, mode):
    payload, path, database, sender, directory = checkpoint
    def forbidden(*args, **kwargs):
        raise AssertionError('Invalid mode cannot open checkpoint store')
    monkeypatch.setattr(cli, 'ClosingBooks', forbidden)
    args = [str(path), '--book-directory', str(directory), *mode]
    assert cli.main(args) == 2 and sender.constructed == 0
