"""Synthetic private directories only; no broker/operating state or secrets."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from astral_stock_agent.broker_order_state import OrderIntent
from astral_stock_agent.broker_intent_journal import (
    PrivateIntentJournal, IntentJournalError, ERROR,
)


def intent(**changes):
    return OrderIntent(**(dict(mode='paper', scope_digest='a'*64, client_order_id='synthetic-key',
        symbol='SPY', side='buy', quantity=1, limit_price=Decimal('100.00')) | changes))


def open_journal(path, **changes):
    return PrivateIntentJournal(path, **(dict(mode='paper', scope_digest='a'*64) | changes))


@pytest.fixture
def journal(tmp_path, monkeypatch):
    monkeypatch.delenv('GITHUB_ACTIONS', raising=False)
    path=tmp_path/'intent-only'; path.mkdir(mode=0o700)
    return open_journal(path,initialize=True),path


def intent_path(path):
    name=hashlib.sha256(b'broker-intent-v1:synthetic-key').hexdigest()+'.json'
    return path/name


def test_reopen_same_intent_is_noop_preserving_first_bytes_and_no_authority(journal):
    store,path=journal
    receipt=store.record(intent())
    first=intent_path(path).read_bytes()
    assert receipt.created and receipt.reconciliation_required and not receipt.order_submission_approved
    reopened=open_journal(path)
    assert reopened.read('synthetic-key').intent==intent()
    assert not reopened.record(intent(limit_price=Decimal('100.0'))).created
    assert intent_path(path).read_bytes()==first
    assert len(list(path.iterdir()))==2
    assert all(p.stat().st_mode&0o777==0o600 for p in path.iterdir())
    assert 'synthetic-key' not in repr(receipt)


@pytest.mark.parametrize('change', [
    {'quantity':2},{'symbol':'QQQ'},{'side':'sell'},{'limit_price':Decimal('100.01')},
    {'mode':'live'},{'scope_digest':'b'*64},
])
def test_same_key_changes_never_overwrite_first_intent(journal, change):
    store,path=journal;store.record(intent());before={p.name:p.read_bytes() for p in path.iterdir()}
    with pytest.raises(IntentJournalError,match=ERROR):
        store.record(intent(**change))
    assert {p.name:p.read_bytes() for p in path.iterdir()}==before


def test_explicit_empty_bootstrap_only_and_missing_receipt_is_not_created(tmp_path,monkeypatch):
    monkeypatch.delenv('GITHUB_ACTIONS',raising=False)
    path=tmp_path/'empty';path.mkdir(mode=0o700)
    with pytest.raises(IntentJournalError): open_journal(path)
    assert not list(path.iterdir())
    foreign=path/'foreign.json';foreign.write_bytes(b'partial');foreign.chmod(0o600)
    with pytest.raises(IntentJournalError): open_journal(path,initialize=True)
    assert foreign.read_bytes()==b'partial' and len(list(path.iterdir()))==1


def test_missing_key_mode_scope_and_missing_pin_block_without_recreation(journal):
    store,path=journal
    with pytest.raises(IntentJournalError): store.read('absent')
    for args in ({'mode':'live'},{'scope_digest':'b'*64}):
        with pytest.raises(IntentJournalError): open_journal(path,initialize=True,**args)
    assert len(list(path.iterdir()))==1
    pin=next(path.iterdir());before=pin.read_bytes();pin.unlink()  # Synthetic failure injection only.
    with pytest.raises(IntentJournalError): store.record(intent())
    assert not list(path.iterdir()) and before


@pytest.mark.parametrize('raw', [
    b'',b'{',b'null',b'[]',b'NaN',b'{"schema":"a","schema":"b"}',
    b'x'*4097,b'{"schema":"private_broker_intent_v1","intent":{}}',
])
def test_partial_malformed_and_oversize_receipts_preserved(journal,raw):
    store,path=journal;p=intent_path(path);p.write_bytes(raw);p.chmod(0o600)
    for action in (lambda:store.read('synthetic-key'),lambda:store.record(intent())):
        with pytest.raises(IntentJournalError) as exc: action()
        assert str(exc.value)==ERROR
    assert p.read_bytes()==raw


@pytest.mark.parametrize('mutation', ['quantity','price','key','scope','extra'])
def test_closed_schema_and_intent_binding_checked_on_read(journal,mutation):
    store,path=journal;store.record(intent());p=intent_path(path);value=json.loads(p.read_bytes())
    if mutation=='quantity': value['intent']['quantity']=True
    elif mutation=='price': value['intent']['limit_price']='NaN'
    elif mutation=='key': value['intent']['client_order_id']='different-key'
    elif mutation=='scope': value['intent']['scope_digest']='b'*64
    else:value['extra']='synthetic-private-marker'
    raw=json.dumps(value).encode();p.write_bytes(raw)
    with pytest.raises(IntentJournalError) as exc:store.read('synthetic-key')
    assert str(exc.value)==ERROR and p.read_bytes()==raw


@pytest.mark.parametrize('kind',['symlink','hardlink','permissions'])
def test_unsafe_receipt_storage_is_never_repaired_or_followed(journal,tmp_path,kind):
    store,path=journal;p=intent_path(path)
    other=tmp_path/'target';other.write_bytes(b'synthetic-private-marker');other.chmod(0o600)
    if kind=='symlink':p.symlink_to(other)
    elif kind=='hardlink':os.link(other,p)
    else:p.write_bytes(b'partial');p.chmod(0o644)
    for action in (lambda:store.read('synthetic-key'),lambda:store.record(intent())):
        with pytest.raises(IntentJournalError):action()
    assert other.read_bytes()==b'synthetic-private-marker'
    if kind=='symlink':assert p.is_symlink()
    if kind=='permissions':assert p.stat().st_mode&0o777==0o644


def test_concurrent_same_key_creates_at_most_one_receipt_and_never_dispatches(journal):
    _,path=journal;barrier=threading.Barrier(6)
    def attempt(n):
        store=open_journal(path);barrier.wait()
        try:return store.record(intent()).created
        except IntentJournalError:return 'blocked'  # A concurrent partial write may fail closed.
    with ThreadPoolExecutor(max_workers=6) as pool:results=list(pool.map(attempt,range(6)))
    assert sum(r is True for r in results)==1
    receipt=open_journal(path).read('synthetic-key')
    assert receipt.intent==intent() and not receipt.created
    assert receipt.reconciliation_required and not receipt.order_submission_approved


def test_competing_payloads_cannot_replace_winning_intent(journal):
    _,path=journal;barrier=threading.Barrier(2)
    def attempt(qty):
        store=open_journal(path);barrier.wait()
        try:return store.record(intent(quantity=qty)).created
        except IntentJournalError:return 'blocked'
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(attempt,(1,2)))
    assert sum(x is True for x in results)==1 and 'blocked' in results
    winner=open_journal(path).read('synthetic-key').intent
    assert winner.quantity in (1,2)
    with pytest.raises(IntentJournalError):open_journal(path).record(replace(winner,quantity=3))


def test_fsync_failure_leaves_receipt_and_requires_reconciliation(journal,monkeypatch):
    store,path=journal
    def fail(path):raise OSError('synthetic-private-marker')
    monkeypatch.setattr(store._storage,'_sync',fail)
    with pytest.raises(IntentJournalError) as exc:store.record(intent())
    assert str(exc.value)==ERROR and intent_path(path).is_file()
    reopened=open_journal(path);receipt=reopened.record(intent())
    assert not receipt.created and receipt.reconciliation_required and not receipt.order_submission_approved


def test_fresh_process_reads_same_receipt_without_operating_environment(journal):
    store,path=journal;store.record(intent());before={p.name:p.read_bytes() for p in path.iterdir()}
    script='''
import sys
from astral_stock_agent.broker_intent_journal import PrivateIntentJournal
def deny(event,args):
    if event.startswith('socket.'):raise AssertionError('network forbidden')
sys.addaudithook(deny)
j=PrivateIntentJournal(sys.argv[1],mode='paper',scope_digest='a'*64)
r=j.read('synthetic-key')
assert not r.created and r.reconciliation_required and not r.order_submission_approved
'''
    root=Path(__file__).resolve().parents[1]
    r=subprocess.run([sys.executable,'-c',script,str(path)],capture_output=True,text=True,
        env=dict(PATH=os.defpath,PYTHONPATH=str(root/'src'),PYTHONDONTWRITEBYTECODE='1'),timeout=20)
    assert r.returncode==0 and r.stdout==r.stderr==''
    assert {p.name:p.read_bytes() for p in path.iterdir()}==before


def test_public_ci_default_block_occurs_before_storage_probe(tmp_path,monkeypatch):
    monkeypatch.setenv('GITHUB_ACTIONS','true')
    def forbidden(*args,**kwargs):raise AssertionError('storage probe forbidden')
    monkeypatch.setattr(Path,'lstat',forbidden)
    with pytest.raises(IntentJournalError,match=ERROR):open_journal(tmp_path/'absent',initialize=True)


@pytest.mark.parametrize('key',[None,'../synthetic','synthetic-private-marker!',[]])
def test_invalid_keys_are_fixed_errors(journal,key):
    with pytest.raises(IntentJournalError) as exc:journal[0].read(key)
    assert str(exc.value)==ERROR
