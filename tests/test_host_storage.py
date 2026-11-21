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


def test_fingerprint_requires_stopped_writers_before_any_access(root, monkeypatch):
    def forbidden(*args, **kwargs): raise AssertionError('filesystem touched')
    monkeypatch.setattr(host, '_root', forbidden)
    for value in (False, None, 1, 'true'):
        with pytest.raises(host.HostStorageError): host.fingerprint(root, worker_stopped=value)


@pytest.mark.parametrize('state', ['pending', 'sending', 'unknown', 'sent'])
def test_offline_restore_fingerprint_preserves_bytes_permissions_and_delivery(root, tmp_path, state):
    import shutil
    host.initialize(root)
    box = Outbox(root/'outbox'/'reports.sqlite')
    box.enqueue('synthetic', 'synthetic private body')
    with box._connect() as db: db.execute('UPDATE reports SET state=?', (state,))
    paper = IndependentPaperLedger(root/'paper')
    paper._write_once(paper._path('synthetic'), b'{"synthetic":"unchanged"}', 'conflict')
    before = snapshot(root)
    result = host.fingerprint(root, worker_stopped=True)
    assert result['outbox'][state] == 1
    assert not result['accounting_verified'] and not result['restore_verified']
    assert not result['writer_exclusion_verified']
    restored = tmp_path/'offline-restored'
    shutil.copytree(root, restored)
    copy_before = snapshot(restored)
    assert host.fingerprint(restored, worker_stopped=True) == result
    assert snapshot(root) == before and snapshot(restored) == copy_before
    target = next((restored/'paper').iterdir())
    target.write_bytes(b'{"synthetic":"changed"}')
    changed = host.fingerprint(restored, worker_stopped=True)
    assert changed['content_sha256'] != result['content_sha256']
    target.unlink()
    assert host.fingerprint(restored, worker_stopped=True)['content_sha256'] != result['content_sha256']


@pytest.mark.parametrize('kind', ['wal', 'journal', 'shm', 'symlink', 'hardlink', 'permissions',
                                  'oversize', 'incomplete_control'])
def test_fingerprint_blocks_unsafe_partial_or_hot_copies_without_repair(root, kind):
    host.initialize(root)
    target = root/'paper'/'synthetic.json'
    if kind in ('wal','journal','shm'):
        target = root/'outbox'/('reports.sqlite-'+kind)
        target.write_bytes(b'synthetic unfinished state'); target.chmod(0o600)
    elif kind == 'symlink': target.symlink_to(root/'outbox'/'reports.sqlite')
    elif kind == 'hardlink': target.hardlink_to(root/'outbox'/'reports.sqlite')
    elif kind == 'permissions': target.write_bytes(b'private'); target.chmod(0o644)
    elif kind == 'oversize': target.write_bytes(b'x'*1_000_001); target.chmod(0o600)
    else:
        control = IndependentPaperLedger(root/'control')
        control._write_once(control._path('attempt:2026-11-20'),
                            b'{"kind":"attempt","session":"2026-11-20"}', 'conflict')
    before = snapshot(root)
    with pytest.raises(host.HostStorageError, match='^'+host.ERROR+'$'):
        host.fingerprint(root, worker_stopped=True)
    assert snapshot(root) == before


@pytest.mark.parametrize('limit', ['MAX_FINGERPRINT_FILES', 'MAX_FINGERPRINT_BYTES'])
def test_fingerprint_has_finite_aggregate_bounds(root, monkeypatch, limit):
    host.initialize(root)
    monkeypatch.setattr(host, limit, 1)
    with pytest.raises(host.HostStorageError): host.fingerprint(root, worker_stopped=True)


def test_fingerprint_blocks_observed_between_pass_change(root, monkeypatch):
    host.initialize(root)
    actual = host.inspect
    def changed(directory):
        result = actual(directory)
        p = root/'paper'/'synthetic.json'; p.write_bytes(b'changed'); p.chmod(0o600)
        return result
    monkeypatch.setattr(host, 'inspect', changed)
    with pytest.raises(host.HostStorageError): host.fingerprint(root, worker_stopped=True)
    assert (root/'paper'/'synthetic.json').read_bytes() == b'changed'


def test_fingerprint_public_ci_and_cli_never_disclose_private_input(root, monkeypatch, capsys):
    host.initialize(root)
    assert host.main(['fingerprint','--directory',str(root)]) == 2
    assert host.main(['fingerprint','--directory',str(root),'--worker-stopped']) == 0
    output = capsys.readouterr().out
    assert 'fingerprinted' in output and str(root) not in output
    monkeypatch.setenv('GITHUB_ACTIONS','true')
    with pytest.raises(host.HostStorageError): host.fingerprint(root, worker_stopped=True)


def test_fingerprint_root_name_does_not_change_snapshot_meaning(root):
    host.initialize(root)
    first = host.fingerprint(root, worker_stopped=True)
    renamed = root.parent / 'outbox'
    root.rename(renamed)  # synthetic test directory only
    assert host.fingerprint(renamed, worker_stopped=True) == first
