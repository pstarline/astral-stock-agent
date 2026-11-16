"""Private host bootstrap/recovery tests: synthetic disk only, no providers."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import stat

import pytest

from astral_stock_agent import host_storage as host
from astral_stock_agent.delivery import Outbox
from astral_stock_agent.independent_ledger import IndependentPaperLedger


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.delenv('GITHUB_ACTIONS', raising=False)
    path = tmp_path / 'private'
    path.mkdir(mode=0o700)
    return path


def snapshot(root):
    return {str(p.relative_to(root)): (p.stat().st_mode, p.read_bytes())
            for p in root.rglob('*') if p.is_file()}


def test_first_install_and_read_only_inspection(root):
    assert host.initialize(root) == dict(status='initialized', worker_enabled=False)
    for name in host.DIRECTORIES:
        assert stat.S_IMODE((root/name).stat().st_mode) == 0o700
    before = snapshot(root)
    result = host.inspect(root)
    assert result['status'] == 'inspected'
    assert result['accounting_verified'] is False
    assert result['recipient_receipt_verified'] is False
    assert result['delivery_reconciliation_required'] is False
    assert snapshot(root) == before
    with pytest.raises(host.HostStorageError):
        host.initialize(root)
    assert snapshot(root) == before


def test_two_initializers_only_one_can_create(root):
    def attempt(_):
        try:
            return host.initialize(root)['status']
        except host.HostStorageError:
            return 'blocked'
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(attempt, range(2))) == ['blocked', 'initialized']
    assert host.inspect(root)['status'] == 'inspected'


def test_partial_initialization_never_repaired(root, monkeypatch):
    actual = host.Outbox
    def fail(*args, **kwargs):
        raise RuntimeError('synthetic private details')
    monkeypatch.setattr(host, 'Outbox', fail)
    with pytest.raises(host.HostStorageError, match='^'+host.ERROR+'$'):
        host.initialize(root)
    before = snapshot(root)
    monkeypatch.setattr(host, 'Outbox', actual)
    for action in (host.initialize, host.inspect):
        with pytest.raises(host.HostStorageError):
            action(root)
    assert snapshot(root) == before
    assert not (root/'initialized.json').exists()


@pytest.mark.parametrize('kind', ['nonempty', 'permissions', 'symlink', 'relative', 'missing'])
def test_unsafe_or_existing_roots_rejected_without_changes(root, kind, monkeypatch):
    supplied = root
    if kind == 'nonempty':
        (root/'operator-data').write_text('synthetic')
    elif kind == 'permissions':
        root.chmod(0o755)
    elif kind == 'symlink':
        supplied = root.parent/'link'
        supplied.symlink_to(root, target_is_directory=True)
    elif kind == 'relative':
        monkeypatch.chdir(root.parent)
        supplied = Path('private')
    else:
        supplied = root/'missing'
    before = snapshot(root)
    with pytest.raises(host.HostStorageError):
        host.initialize(supplied)
    assert snapshot(root) == before


def test_public_ci_blocks_before_filesystem_access(root, monkeypatch):
    monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    for action in (host.initialize, host.inspect):
        with pytest.raises(host.HostStorageError):
            action(root)
    assert list(root.iterdir()) == []


@pytest.mark.parametrize('state', ['pending', 'sending', 'unknown', 'sent'])
def test_inspection_never_changes_delivery_state(root, state):
    host.initialize(root)
    box = Outbox(root/'outbox'/'reports.sqlite')
    box.enqueue('synthetic', 'synthetic report')
    with box._connect() as db:
        db.execute('UPDATE reports SET state=?', (state,))
    before = snapshot(root)
    result = host.inspect(root)
    assert result['outbox'][state] == 1
    assert result['delivery_reconciliation_required'] == (state != 'sent')
    assert snapshot(root) == before


def test_incomplete_worker_attempt_blocks_inspection_without_repair(root):
    host.initialize(root)
    control = IndependentPaperLedger(root/'control')
    control._write_once(control._path('attempt:2026-11-16'),
                        b'{"kind":"attempt","session":"2026-11-16"}', 'conflict')
    before = snapshot(root)
    with pytest.raises(host.HostStorageError):
        host.inspect(root)
    assert snapshot(root) == before


def test_cli_hides_private_arguments(root, capsys):
    assert host.main(['initialize-empty', '--directory', str(root)]) == 0
    assert host.main(['inspect', '--directory', str(root)]) == 0
    assert host.main(['inspect', '--directory', '/SYNTHETIC_PRIVATE/missing']) == 2
    assert host.main(['--SYNTHETIC_PRIVATE']) == 2
    assert 'SYNTHETIC_PRIVATE' not in capsys.readouterr().out
