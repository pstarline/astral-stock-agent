"""Synthetic process/restore boundaries, never live delivery or host recovery."""
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

import pytest

from astral_stock_agent.delivery import Outbox
from astral_stock_agent.host_storage import initialize, fingerprint, HostStorageError
from astral_stock_agent.independent_ledger import IndependentPaperLedger
from test_price_archive import DAYS


# A fresh interpreter per attempt: no inherited monkeypatches, sender, cache or
# Python objects. Only a private temporary path and synthetic scenario cross it.
CHILD = r'''
import json, os, sys
from datetime import timedelta
from pathlib import Path
from astral_stock_agent.delivery import Telegram
from astral_stock_agent.independent_signals import SMA_RULE
from astral_stock_agent.paper_worker import tick, WorkerError
import astral_stock_agent.paper_runtime as runtime
from test_price_archive import window, DAYS

def deny_network(event, args):
    if event.startswith('socket.'):
        raise AssertionError('synthetic process must not use network')
sys.addaudithook(deny_network)
def no_real_sender(*a, **k):
    raise AssertionError('real Telegram transport forbidden')
Telegram.send = no_real_sender
root, day, mode = Path(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
config = dict(schema='archived_sma_record_v1', symbol='IBM', initial_cash='100',
    commission_bps='5', slippage_bps='5',
    rule=dict(id=SMA_RULE, window=2, units='1', trade_from=DAYS[2]))
data, now = window(day, day+3, changed=DAYS[1] if day <= 1 else None)
now += timedelta(hours=2)
calls = dict(fetches=0, sends=0)
def fetch(**kw):
    calls['fetches'] += 1
    return data
runtime.fetch_alpha_daily = fetch
class SyntheticSender:
    def __init__(self, *a): pass
    def send(self, body):
        calls['sends'] += 1
        if mode == 'crash':
            # Outbox has committed sending; bypass every Python finally/handler.
            os._exit(73)
        return 'synthetic-receipt'
env = dict(PAPER_WORKER_ENABLED='true', PAPER_CANDIDATE_APPROVED='true',
    PAPER_REPORTING_ENABLED='true', PAPER_STORAGE_ROOT=str(root),
    PAPER_CONFIG_JSON=json.dumps(config), PAPER_BOOTSTRAP_SESSION=DAYS[2],
    PAPER_US_SYMBOL='IBM', ALPHAVANTAGE_API_KEY='SYNTHETIC',
    TELEGRAM_BOT_TOKEN='SYNTHETIC', TELEGRAM_CHAT_ID='SYNTHETIC')
try:
    result = tick(env, clock=lambda: now, sender_factory=SyntheticSender)
    status = result['status']
except WorkerError:
    status = 'blocked'
print(json.dumps(dict(status=status, **calls)))
'''


def run_child(root, day=0, mode='normal'):
    repo = Path(__file__).resolve().parents[1]
    # Never inherit operating keys, chat IDs, credentials or worker opt-ins.
    env = dict(PATH=os.defpath, PYTHONPATH=os.pathsep.join(
        (str(repo/'src'), str(repo/'tests'))), PYTHONDONTWRITEBYTECODE='1')
    result = subprocess.run([sys.executable, '-c', CHILD, str(root), str(day), mode],
        env=env, capture_output=True, text=True, timeout=30)
    if mode == 'crash':
        assert result.returncode == 73 and result.stdout == '' and result.stderr == ''
        return None
    # Do not reflect child diagnostics/private paths into CI logs on failure.
    assert result.returncode == 0, 'synthetic child failed'
    assert result.stderr == '', 'unexpected child diagnostics'
    return json.loads(result.stdout)


def snapshot(root):
    return {str(p.relative_to(root)): (stat.S_IMODE(p.stat().st_mode),
            p.read_bytes() if p.is_file() else None) for p in root.rglob('*')}


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.delenv('GITHUB_ACTIONS', raising=False)
    root = tmp_path/'synthetic-source'
    root.mkdir(mode=0o700)
    initialize(root)
    return root


def test_fresh_process_replay_restore_and_next_open_preserve_original(store, tmp_path):
    assert run_child(store) == dict(status='completed', fetches=1, sends=1)
    baseline, _, _ = IndependentPaperLedger(store/'paper')._load_checkpoint(DAYS[2])
    assert baseline['cumulative_simulated_fills'] == 0
    assert baseline['units'] == '0' and baseline['pending_close_change'] == '1'
    before = snapshot(store)
    expected = dict(status='already_completed', fetches=0, sends=0)
    assert run_child(store) == expected
    assert snapshot(store) == before
    original = fingerprint(store, worker_stopped=True)
    restored = tmp_path/'synthetic-offline-copy'
    shutil.copytree(store, restored)
    assert fingerprint(restored, worker_stopped=True) == original
    assert run_child(restored) == expected
    assert snapshot(restored) == before
    assert run_child(restored, day=1) == dict(status='completed', fetches=1, sends=1)
    current, _, _ = IndependentPaperLedger(restored/'paper')._load_checkpoint(DAYS[3])
    assert current['daily_simulated_fills'] == current['cumulative_simulated_fills'] == 1
    assert current['units'] == '1' and current['daily_units_change'] == '1'
    after = snapshot(restored)
    assert run_child(restored, day=1) == expected
    assert snapshot(restored) == after and snapshot(store) == before
    assert Outbox(restored/'outbox'/'reports.sqlite', read_only=True).status_counts()['sent'] == 2


@pytest.mark.parametrize('restore', [False, True])
def test_process_exit_in_sender_leaves_sending_and_blocks_all_later_work(store, tmp_path, restore):
    run_child(store, mode='crash')
    counts = Outbox(store/'outbox'/'reports.sqlite', read_only=True).status_counts()
    assert counts == dict(pending=0, sending=1, sent=0, unknown=0)
    before = snapshot(store)
    target = store
    if restore:
        target = tmp_path/'synthetic-crashed-copy'
        shutil.copytree(store, target)
    for day in (0, 1):
        assert run_child(target, day=day) == dict(status='blocked', fetches=0, sends=0)
        assert snapshot(target) == before and snapshot(store) == before
    # An incomplete control attempt is not a valid recovery fingerprint. Do not
    # clear it, reset the queue, checkpoint a live database or resend anything.
    with pytest.raises(HostStorageError):
        fingerprint(target, worker_stopped=True)
    assert snapshot(target) == before
