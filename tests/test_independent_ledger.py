"""Synthetic private forward-ledger checks; no market, order or Telegram I/O."""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
from pathlib import Path

import pytest

from astral_stock_agent.independent_ledger import (
    IndependentLedgerError, IndependentPaperLedger,
)
from astral_stock_agent.independent_paper import preview_independent_paper
from astral_stock_agent.research_sessions import UTC_SESSION_DATE


DAYS = ('2026-01-02', '2026-01-05', '2026-01-06', '2026-01-07')


def inputs(count):
    prices = b'timestamp,open,high,low,close,volume\n' + b''.join(
        f'{day}T00:00:00Z,{op},20,1,{close},100\n'.encode()
        for day, op, close in zip(DAYS[:count], (10, 11, 12, 15), (10, 12, 13, 16))
    )
    changes = ('2', '0', '-1', '3')[:count]
    manifest = {
        'schema': 'independent_price_simulation_v1', 'strategy': 'synthetic rule',
        'version': 'v1', 'symbol': 'SPY', 'price_sha256': hashlib.sha256(prices).hexdigest(),
        'price_count': count, 'first_day': DAYS[0], 'last_day': DAYS[count - 1],
        'label_convention': UTC_SESSION_DATE, 'initial_cash': '100',
        'commission_bps': '5', 'slippage_bps': '5',
        'close_signals': [dict(session=day, units_change=change)
                          for day, change in zip(DAYS[:count], changes)],
    }
    now = datetime.fromisoformat(DAYS[count - 1] + 'T22:00:00+00:00')
    return manifest, prices, now


def simulation(count):
    manifest, prices, now = inputs(count)
    accounting, body = preview_independent_paper(manifest, prices,
        verified_us_symbols=['SPY'], now=now)
    return accounting, body, now


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.delenv('GITHUB_ACTIONS', raising=False)
    path = tmp_path / 'private-ledger'
    path.mkdir(mode=0o700)
    return IndependentPaperLedger(path), path


def test_bootstrap_then_next_session_is_hash_chained_and_idempotent(ledger):
    store, path = ledger
    previous, previous_body, previous_now = simulation(3)
    current, current_body, current_now = simulation(4)

    first = store.append(previous, previous_body, now=previous_now, bootstrap=True)
    second = store.append(current, current_body, now=current_now)
    retry = IndependentPaperLedger(path).append(current, current_body, now=current_now)

    assert first['created'] is True and first['bootstrap'] is True
    assert second['created'] is True and second['bootstrap'] is False
    assert retry == {**second, 'created': False}
    assert len(first['checkpoint_sha256']) == len(second['checkpoint_sha256']) == 64
    assert first['checkpoint_sha256'] != second['checkpoint_sha256']
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in path.iterdir())


@pytest.mark.parametrize('field,change', [
    ('daily_pnl', Decimal('0.01')),
    ('cumulative_simulated_fills', 1),
    ('cumulative_commission', Decimal('0.01')),
    ('units', Decimal('1')),
    ('daily_units_change', Decimal('1')),
])
def test_restart_rejects_transition_that_does_not_match_previous_checkpoint(ledger, field, change):
    store, _ = ledger
    previous, previous_body, previous_now = simulation(3)
    current, current_body, current_now = simulation(4)
    store.append(previous, previous_body, now=previous_now, bootstrap=True)
    broken = deepcopy(current)
    broken[field] = (broken[field] + change if type(broken[field]) is int
                     else str(Decimal(broken[field]) + change))
    # Preserve internal identities where needed so the previous-day comparison,
    # rather than the single-day arithmetic check, is what rejects the record.
    if field == 'daily_pnl':
        broken['daily_unrealized_change'] = str(
            Decimal(broken['daily_unrealized_change']) + change)
    if field == 'units':
        broken['unrealized_pnl'] = str(Decimal(broken['unrealized_pnl']) - change)
        broken['realized_pnl'] = str(Decimal(broken['realized_pnl']) + change)
    with pytest.raises(IndependentLedgerError, match='reconciliation required'):
        store.append(broken, current_body, now=current_now)


def test_previous_pending_signal_must_equal_today_units_change(ledger):
    store, _ = ledger
    previous, previous_body, previous_now = simulation(3)
    current, current_body, current_now = simulation(4)
    previous['pending_close_change'] = '-2'
    store.append(previous, previous_body, now=previous_now, bootstrap=True)
    with pytest.raises(IndependentLedgerError, match='reconciliation required'):
        store.append(current, current_body, now=current_now)


def test_changed_retry_cannot_replace_checkpoint(ledger):
    store, _ = ledger
    current, body, now = simulation(3)
    store.append(current, body, now=now, bootstrap=True)
    changed = deepcopy(current)
    changed['price_sha256'] = '0' * 64
    with pytest.raises(IndependentLedgerError, match='conflict'):
        store.append(changed, body, now=now, bootstrap=True)


def test_bootstrap_cannot_be_used_to_skip_a_day(ledger):
    store, _ = ledger
    previous, previous_body, previous_now = simulation(3)
    current, current_body, current_now = simulation(4)
    store.append(previous, previous_body, now=previous_now, bootstrap=True)
    with pytest.raises(IndependentLedgerError, match='conflict'):
        store.append(current, current_body, now=current_now, bootstrap=True)


def test_non_latest_replay_and_missing_baseline_are_blocked(ledger):
    store, _ = ledger
    old, old_body, _ = simulation(3)
    current, current_body, now = simulation(4)
    with pytest.raises(IndependentLedgerError, match='complete independent'):
        store.append(old, old_body, now=now, bootstrap=True)
    with pytest.raises(IndependentLedgerError, match='missing'):
        store.append(current, current_body, now=now)


def test_public_ci_and_unprivate_directory_block_before_writes(tmp_path, monkeypatch):
    path = tmp_path / 'ledger'
    path.mkdir(mode=0o700)
    monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    with pytest.raises(IndependentLedgerError, match='unavailable'):
        IndependentPaperLedger(path)
    monkeypatch.delenv('GITHUB_ACTIONS')
    path.chmod(0o755)
    with pytest.raises(IndependentLedgerError, match='unavailable'):
        IndependentPaperLedger(path)


def test_replaced_directory_is_detected_before_append(ledger, tmp_path):
    store, path = ledger
    moved = tmp_path / 'moved'
    path.rename(moved)
    path.mkdir(mode=0o700)
    accounting, body, now = simulation(3)
    with pytest.raises(IndependentLedgerError, match='unavailable'):
        store.append(accounting, body, now=now, bootstrap=True)
    assert not tuple(path.iterdir())


def test_report_body_is_part_of_the_immutable_checkpoint(ledger):
    store, _ = ledger
    accounting, body, now = simulation(3)
    with pytest.raises(IndependentLedgerError, match='reconciliation required'):
        store.append(accounting, body + '\nchanged', now=now, bootstrap=True)
    assert not tuple(store.directory.iterdir())


def test_private_cli_records_bootstrap_and_next_day_without_sending(tmp_path, monkeypatch, capsys):
    import json
    import astral_stock_agent.cli as cli

    ledger = tmp_path / 'ledger'
    ledger.mkdir(mode=0o700)
    manifest_path, prices_path = tmp_path / 'manifest.json', tmp_path / 'prices.csv'

    class Clock:
        current = None

        @classmethod
        def now(cls, zone):
            return cls.current

    monkeypatch.setattr(cli, 'datetime', Clock)
    monkeypatch.delenv('GITHUB_ACTIONS', raising=False)
    def forbidden(*args, **kwargs):
        raise AssertionError('recording cannot construct delivery clients')
    monkeypatch.setattr(cli, 'Telegram', forbidden)
    monkeypatch.setattr(cli, 'Outbox', forbidden)

    for count, bootstrap in ((3, True), (4, False)):
        manifest, prices, Clock.current = inputs(count)
        manifest_path.write_text(json.dumps(manifest))
        prices_path.write_bytes(prices)
        args = ['record-simulation', str(manifest_path), str(prices_path),
                '--ledger-directory', str(ledger), '--us-symbol', 'SPY']
        if bootstrap:
            args.append('--bootstrap')
        assert cli.main(args) == 0
        result = json.loads(capsys.readouterr().out)
        assert result == {'bootstrap': bootstrap, 'created': True, 'session': DAYS[count - 1]}


def test_record_cli_public_ci_blocks_before_private_inputs(monkeypatch, capsys):
    import astral_stock_agent.cli as cli
    def forbidden(*args, **kwargs):
        raise AssertionError('public CI cannot touch private inputs or ledger')
    monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    monkeypatch.setattr(Path, 'open', forbidden)
    args = ['record-simulation', 'private.json', 'private.csv',
            '--ledger-directory', 'private-ledger', '--us-symbol', 'SPY', '--bootstrap']
    assert cli.main(args) == 2
    output = capsys.readouterr().out
    assert 'blocked' in output
    assert all(value not in output for value in ('private.json', 'private.csv', 'private-ledger'))
