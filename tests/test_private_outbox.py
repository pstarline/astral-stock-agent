"""Filesystem privacy and missing-ledger failures; no external delivery."""
import os
import sqlite3
import stat
from pathlib import Path

import pytest

from astral_stock_agent.delivery import DeliveryError, Outbox


class Sender:
    def __init__(self):
        self.calls = 0

    def send(self, body):
        self.calls += 1
        return '12'


def test_new_database_is_owner_only_even_under_permissive_umask(tmp_path):
    old = os.umask(0)
    try:
        box = Outbox(tmp_path / 'reports.sqlite', create=True)
        box.enqueue('day', 'private-body')
    finally:
        os.umask(old)
    assert stat.S_IMODE(Path(box.path).stat().st_mode) == 0o600
    assert box.state('day') == 'pending'


@pytest.mark.parametrize('mode', [0o644, 0o640, 0o660, 0o400, 0o700])
def test_existing_file_permissions_not_silently_changed(tmp_path, mode):
    path = tmp_path / 'reports.sqlite'
    path.write_text('private sentinel')
    path.chmod(mode)
    with pytest.raises(DeliveryError, match='private outbox file'):
        Outbox(path, create=True)
    assert path.read_text() == 'private sentinel'
    assert stat.S_IMODE(path.stat().st_mode) == mode


@pytest.mark.parametrize('mode', [0o755, 0o750, 0o770, 0o777])
def test_parent_must_be_private(tmp_path, mode):
    parent = tmp_path / 'runtime'
    parent.mkdir(mode=mode)
    parent.chmod(mode)
    with pytest.raises(DeliveryError):
        Outbox(parent / 'reports.sqlite', create=True)
    assert not (parent / 'reports.sqlite').exists()


def test_symlink_database_never_touches_target(tmp_path):
    target = tmp_path / 'target'
    target.write_text('private sentinel')
    target.chmod(0o600)
    link = tmp_path / 'reports.sqlite'
    link.symlink_to(target)
    with pytest.raises(DeliveryError):
        Outbox(link, create=True)
    assert target.read_text() == 'private sentinel'


def test_symlink_directory_rejected(tmp_path):
    target = tmp_path / 'runtime'
    target.mkdir(mode=0o700)
    link = tmp_path / 'linked-runtime'
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(DeliveryError):
        Outbox(link / 'reports.sqlite', create=True)
    assert not (target / 'reports.sqlite').exists()


def test_hardlinked_database_rejected(tmp_path):
    path = tmp_path / 'reports.sqlite'
    Outbox(path, create=True)
    os.link(path, tmp_path / 'other-name')
    with pytest.raises(DeliveryError):
        Outbox(path, create=True)


def test_unsafe_writable_ancestor_rejected(tmp_path):
    ancestor = tmp_path / 'shared'
    ancestor.mkdir(mode=0o777)
    ancestor.chmod(0o777)
    parent = ancestor / 'runtime'
    parent.mkdir(mode=0o700)
    with pytest.raises(DeliveryError):
        Outbox(parent / 'reports.sqlite', create=True)
    assert not (parent / 'reports.sqlite').exists()


@pytest.mark.parametrize('operation', ['enqueue', 'state', 'deliver'])
def test_deleted_database_is_not_recreated(tmp_path, operation):
    path = tmp_path / 'reports.sqlite'
    box = Outbox(path, create=True)
    box.enqueue('day', 'report')
    path.unlink()
    sender = Sender()
    with pytest.raises(DeliveryError):
        if operation == 'enqueue':
            box.enqueue('day', 'report')
        elif operation == 'state':
            box.state('day')
        else:
            box.deliver('day', sender)
    assert not path.exists() and sender.calls == 0


def test_replaced_database_blocks_before_sender(tmp_path):
    path = tmp_path / 'reports.sqlite'
    box = Outbox(path, create=True)
    box.enqueue('day', 'report')
    replacement = tmp_path / 'replacement.sqlite'
    Outbox(replacement, create=True).enqueue('day', 'replacement')
    os.replace(replacement, path)
    sender = Sender()
    with pytest.raises(DeliveryError, match='storage changed'):
        box.deliver('day', sender)
    assert sender.calls == 0


def test_permissions_changed_after_enqueue_blocks_sender(tmp_path):
    path = tmp_path / 'reports.sqlite'
    box = Outbox(path, create=True)
    box.enqueue('day', 'report')
    path.chmod(0o644)
    sender = Sender()
    with pytest.raises(DeliveryError):
        box.deliver('day', sender)
    assert sender.calls == 0
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT state FROM reports').fetchone()[0] == 'pending'


@pytest.mark.parametrize('suffix', ['-journal', '-wal', '-shm'])
def test_linked_sidecar_rejected_without_target_write(tmp_path, suffix):
    path = tmp_path / 'reports.sqlite'
    box = Outbox(path, create=True)
    target = tmp_path / 'private-target'
    target.write_text('private sentinel')
    target.chmod(0o600)
    Path(str(path) + suffix).symlink_to(target)
    with pytest.raises(DeliveryError):
        box.enqueue('day', 'report')
    assert target.read_text() == 'private sentinel'


def test_loss_after_send_never_recreates_ledger_or_claims_saved_receipt(tmp_path):
    path = tmp_path / 'reports.sqlite'
    box = Outbox(path, create=True)
    box.enqueue('day', 'report')

    class StorageLoss(Sender):
        def send(self, body):
            self.calls += 1
            path.unlink()
            return '42'

    sender = StorageLoss()
    with pytest.raises(DeliveryError):
        box.deliver('day', sender)
    assert sender.calls == 1 and not path.exists()
    with pytest.raises(DeliveryError):
        box.deliver('day', sender)
    assert sender.calls == 1


def test_private_existing_ledger_reopens_without_resetting_sent_state(tmp_path):
    path = tmp_path / 'reports.sqlite'
    sender = Sender()
    box = Outbox(path, create=True)
    box.enqueue('day', 'report')
    assert box.deliver('day', sender)
    reopened = Outbox(path, create=True)
    assert reopened.state('day') == 'sent'
    assert not reopened.deliver('day', sender)
    assert sender.calls == 1


def test_operational_open_never_initializes_missing_file_after_restart(tmp_path):
    path = tmp_path / 'reports.sqlite'
    Outbox(path, create=True).enqueue('day', 'report')
    path.unlink()
    with pytest.raises(DeliveryError):
        Outbox(path)
    assert not path.exists()


def test_operational_open_rejects_empty_existing_file(tmp_path):
    path = tmp_path / 'reports.sqlite'
    path.touch(mode=0o600)
    with pytest.raises(DeliveryError, match='uninitialized'):
        Outbox(path)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE name='reports'").fetchall() == []


def test_default_reopen_preserves_sent_receipt(tmp_path):
    path = tmp_path / 'reports.sqlite'
    box = Outbox(path, create=True)
    box.enqueue('day', 'report')
    sender = Sender()
    box.deliver('day', sender)
    assert Outbox(path).state('day') == 'sent'
    assert not Outbox(path).deliver('day', sender)
    assert sender.calls == 1


def test_cli_initializes_only_explicit_private_database(tmp_path, monkeypatch, capsys):
    from astral_stock_agent.cli import main
    monkeypatch.delenv('GITHUB_ACTIONS', raising=False)
    path = tmp_path / 'reports.sqlite'
    assert main(['init-outbox', '--database', str(path)]) == 0
    capsys.readouterr()
    assert Outbox(path).state('day') is None
    Outbox(path).enqueue('day', 'preserved')
    assert main(['init-outbox', '--database', str(path)]) == 0
    assert Outbox(path).state('day') == 'pending'


def test_public_ci_cannot_initialize_operational_outbox(tmp_path, monkeypatch):
    from astral_stock_agent.cli import main
    monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    path = tmp_path / 'reports.sqlite'
    assert main(['init-outbox', '--database', str(path)]) == 2
    assert not path.exists()


def test_cli_private_directory_error_omits_path(tmp_path, monkeypatch, capsys):
    from astral_stock_agent.cli import main
    monkeypatch.delenv('GITHUB_ACTIONS', raising=False)
    parent = tmp_path / 'private-path-sentinel'
    parent.mkdir(mode=0o755)
    assert main(['init-outbox', '--database', str(parent / 'reports.sqlite')]) == 2
    assert 'private-path-sentinel' not in capsys.readouterr().out


def test_uri_metacharacters_remain_literal_private_filename(tmp_path):
    path = tmp_path / 'reports?cache=shared#private.sqlite'
    Outbox(path, create=True).enqueue('day', 'private body')
    assert Outbox(path).state('day') == 'pending'
    assert path.exists() and not (tmp_path / 'reports').exists()


def test_shared_readable_sidecar_rejected(tmp_path):
    path = tmp_path / 'reports.sqlite'
    box = Outbox(path, create=True)
    box.enqueue('day', 'report')
    journal = Path(str(path) + '-journal')
    journal.write_bytes(b'private sentinel')
    journal.chmod(0o644)
    sender = Sender()
    with pytest.raises(DeliveryError):
        box.deliver('day', sender)
    assert sender.calls == 0 and journal.read_bytes() == b'private sentinel'


def test_send_cli_never_bootstraps_missing_ledger(tmp_path, monkeypatch, capsys):
    import json
    import astral_stock_agent.cli as cli
    monkeypatch.delenv('GITHUB_ACTIONS', raising=False)
    path = tmp_path / 'missing.sqlite'
    snapshot = tmp_path / 'snapshot.json'
    snapshot.write_text(json.dumps({'source': 'astral_mcp'}))
    monkeypatch.setattr(cli, 'render', lambda *a, **kw: ('day', 'report'))
    sender = Sender()
    monkeypatch.setattr(cli, 'Telegram', lambda *args: sender)
    assert cli.main([str(snapshot), '--send', '--database', str(path)]) == 2
    assert sender.calls == 0 and not path.exists()
    assert 'Report blocked' in capsys.readouterr().out
