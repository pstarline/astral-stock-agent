"""Private synthetic checkpoints, never official account records."""
import copy
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import pytest

from astral_stock_agent.closing_books import ClosingBookError, ClosingBooks
from astral_stock_agent.reconciliation import ReconciliationError


def capture(book):
    return datetime.fromisoformat(book['collected_at'].replace('Z', '+00:00'))


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.delenv('GITHUB_ACTIONS', raising=False)
    directory = tmp_path / 'private'
    directory.mkdir(mode=0o700)
    evidence = json.loads((Path(__file__).parents[1] / 'examples/daily_reconciliation.json').read_text())
    return directory, evidence


def test_restart_loads_exact_previous_day_then_reconciles_without_changing_sources(setup):
    directory, e = setup
    before = copy.deepcopy(e)
    assert ClosingBooks(directory).save(e['previous'], now=capture(e['previous']))
    store = ClosingBooks(directory)
    assert store.previous_for(e['current'], now=capture(e['current'])) == e['previous']
    result = store.reconcile_and_save(e['current'], e['accounting'], verified_us_symbols=['SPY'], now=capture(e['current']))
    assert result['daily_total_pnl_usd'] == '27' and e == before
    assert len(list(directory.iterdir())) == 2
    assert not store.save(e['current'], now=capture(e['current']))
    for p in directory.iterdir():
        assert p.stat().st_mode & 0o777 == 0o600
        assert 'example' not in p.name


def test_changed_book_cannot_replace_an_existing_checkpoint(setup):
    directory, e = setup
    store = ClosingBooks(directory)
    store.save(e['previous'], now=capture(e['previous']))
    path = next(directory.iterdir()); before = path.read_bytes()
    changed = dict(e['previous'], health='different observation')
    with pytest.raises(ClosingBookError):
        store.save(changed, now=capture(changed))
    assert path.read_bytes() == before


@pytest.mark.parametrize('field', ['source', 'account_ref', 'strategy_ref', 'deployment_ref', 'run_ref'])
def test_other_scope_never_supplies_the_previous_baseline(setup, field):
    directory, e = setup
    store = ClosingBooks(directory)
    store.save(e['previous'], now=capture(e['previous']))
    e['current'][field] = 'astral_mcp' if field == 'source' else 'unrelated-scope'
    with pytest.raises(ClosingBookError):
        store.previous_for(e['current'], now=capture(e['current']))
    assert len(list(directory.iterdir())) == 1


def test_failed_accounting_cannot_create_a_current_checkpoint(setup):
    directory, e = setup
    store = ClosingBooks(directory)
    store.save(e['previous'], now=capture(e['previous']))
    e['accounting']['execution_fees_usd'] = '0'
    with pytest.raises(ReconciliationError):
        store.reconcile_and_save(e['current'], e['accounting'], verified_us_symbols=['SPY'], now=capture(e['current']))
    assert len(list(directory.iterdir())) == 1


@pytest.mark.parametrize('damage', ['missing', 'empty', 'duplicate_json', 'wrong_day', 'checksum', 'shared', 'symlink', 'hardlink'])
def test_damaged_or_missing_baseline_is_never_recreated_or_repaired(setup, damage):
    directory, e = setup
    store = ClosingBooks(directory)
    store.save(e['previous'], now=capture(e['previous']))
    p = next(directory.iterdir())
    if damage == 'missing': p.unlink()
    elif damage == 'empty': p.write_bytes(b'')
    elif damage == 'duplicate_json': p.write_text('{"private_key":1,"private_key":2}')
    elif damage == 'wrong_day':
        book = dict(e['previous'], trading_day='2026-11-04',
                    session_closed_at='2026-11-04T21:00:00Z',
                    valuation_at='2026-11-04T21:00:00Z', collected_at='2026-11-04T21:05:00Z')
        store.save(book, now=capture(book))
        other = next(x for x in directory.iterdir() if x != p)
        p.write_bytes(other.read_bytes())  # Valid checksum, wrong session.
    elif damage == 'checksum':
        value = json.loads(p.read_text())
        value['book']['health'] = 'synthetic changed valid text'
        p.write_text(json.dumps(value))
    elif damage == 'shared': p.chmod(0o644)
    elif damage == 'symlink':
        original = directory / 'original'
        p.rename(original); p.symlink_to(original)
    else: os.link(p, directory / 'link')
    before = sorted(x.name for x in directory.iterdir())
    with pytest.raises(ClosingBookError) as exc:
        store.previous_for(e['current'], now=capture(e['current']))
    assert str(exc.value) == 'matching previous closing book unavailable'
    assert sorted(x.name for x in directory.iterdir()) == before


def test_near_close_mark_is_not_an_exact_checkpoint(setup):
    directory, e = setup
    e['previous']['valuation_at'] = '2026-11-05T20:59:00Z'
    with pytest.raises(ClosingBookError):
        ClosingBooks(directory).save(e['previous'], now=capture(e['previous']))
    assert not list(directory.iterdir())


def test_identical_concurrent_writes_do_not_overwrite_or_duplicate(setup):
    directory, e = setup
    def save(_):
        try:
            return ClosingBooks(directory).save(e['previous'], now=capture(e['previous']))
        except ClosingBookError:
            # A reader may observe an in-progress write and fail closed.
            return 'blocked'
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(save, range(4)))
    assert results.count(True) == 1 and len(list(directory.iterdir())) == 1
    assert not ClosingBooks(directory).save(e['previous'], now=capture(e['previous']))


def test_public_ci_blocks_before_storage_access(monkeypatch):
    monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    def forbidden(*args, **kwargs):
        raise AssertionError('No private storage access in public CI')
    monkeypatch.setattr(Path, 'lstat', forbidden)
    with pytest.raises(ClosingBookError):
        ClosingBooks('/private/book-store')


def test_conflicting_concurrent_books_preserve_the_single_winner(setup):
    directory, e = setup
    books = [dict(e['previous'], health='synthetic-version-'+str(i)) for i in range(4)]
    def save(book):
        try:
            return ClosingBooks(directory).save(book, now=capture(book))
        except ClosingBookError:
            return 'blocked'
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(save, books))
    assert results.count(True) == 1 and results.count('blocked') == 3
    stored = json.loads(next(directory.iterdir()).read_text())['book']
    assert stored == books[results.index(True)]


def test_monday_requires_friday_not_the_last_arbitrary_saved_day(setup):
    directory, e = setup
    friday = copy.deepcopy(e['previous'])
    monday = copy.deepcopy(e['previous'])
    for book, day in ((friday, '2026-11-06'), (monday, '2026-11-09')):
        book.update(trading_day=day, session_closed_at=day+'T21:00:00Z',
                    valuation_at=day+'T21:00:00Z', collected_at=day+'T21:05:00Z')
    store = ClosingBooks(directory)
    store.save(e['previous'], now=capture(e['previous']))  # Thursday is insufficient.
    with pytest.raises(ClosingBookError):
        store.previous_for(monday, now=capture(monday))
    store.save(friday, now=capture(friday))
    assert store.previous_for(monday, now=capture(monday)) == friday


def test_identical_retry_repeats_sync_after_uncertain_write(setup, monkeypatch):
    directory, e = setup
    store = ClosingBooks(directory)
    real_sync = os.fsync
    def failed(fd):
        raise OSError('PRIVATE_STORAGE_DETAIL')
    monkeypatch.setattr(os, 'fsync', failed)
    with pytest.raises(ClosingBookError) as exc:
        store.save(e['previous'], now=capture(e['previous']))
    assert 'PRIVATE_STORAGE_DETAIL' not in str(exc.value)
    calls = []
    def checked(fd):
        calls.append(fd)
        real_sync(fd)
    monkeypatch.setattr(os, 'fsync', checked)
    assert not store.save(e['previous'], now=capture(e['previous']))
    assert len(calls) == 2
