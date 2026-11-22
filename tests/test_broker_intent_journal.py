"""Synthetic private directories only; no broker/operating state or secrets."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import select
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
    for action in (lambda:store.read('synthetic-key'),lambda:store.record(intent()),
                   store.load_unreconciled_book):
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
    for action in (lambda:store.read('synthetic-key'),lambda:store.record(intent()),
                   store.load_unreconciled_book):
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
b=j.load_unreconciled_book()
assert b.reconciliation_required and not b.order_submission_approved
assert len(b.records)==1 and b.records[0].status=='unknown'
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


def test_inventory_is_read_only_and_all_intents_are_unknown_after_restart(journal):
    store,path=journal
    empty=store.load_unreconciled_book()
    assert not empty.records and not empty.order_submission_approved
    store.record(intent());store.record(intent(client_order_id='other',symbol='QQQ'))
    before={p.name:(p.read_bytes(),p.stat().st_mode) for p in path.iterdir()}
    book=open_journal(path).load_unreconciled_book()
    assert len(book.records)==2 and book.reconciliation_required
    assert {r.intent.symbol for r in book.records}=={'SPY','QQQ'}
    assert all(r.status=='unknown' and r.broker_order_id is None for r in book.records)
    assert not book.order_submission_approved
    assert {p.name:(p.read_bytes(),p.stat().st_mode) for p in path.iterdir()}==before


def test_reconstructed_other_symbol_unknown_blocks_existing_preflight(journal):
    from test_broker_order_preflight import context,limits,intent as proposed
    from astral_stock_agent.broker_order_preflight import assess_order_preflight
    store,_=journal;store.record(intent(symbol='QQQ'))
    book=store.load_unreconciled_book()
    result=assess_order_preflight(book,proposed(),context(),limits())
    assert result.reasons==('order_unknown',)
    assert not result.order_submission_approved


@pytest.mark.parametrize('kind',['foreign_name','wrong_key_filename','foreign_scope','directory'])
def test_inventory_rejects_foreign_or_misbound_entries_without_cleanup(journal,kind):
    store,path=journal;store.record(intent());p=intent_path(path)
    if kind=='foreign_name':
        extra=path/'unrecognized';extra.write_bytes(b'private-marker');extra.chmod(0o600)
    elif kind=='wrong_key_filename':
        extra=path/('0'*64+'.json');extra.write_bytes(p.read_bytes());extra.chmod(0o600)
    elif kind=='foreign_scope':
        value=json.loads(p.read_bytes());value['intent']['scope_digest']='b'*64
        p.write_bytes(json.dumps(value).encode())
    else:(path/('0'*64+'.json')).mkdir(mode=0o700)
    names={p.name for p in path.iterdir()}
    with pytest.raises(IntentJournalError) as exc:store.load_unreconciled_book()
    assert str(exc.value)==ERROR and {p.name for p in path.iterdir()}==names


@pytest.mark.parametrize('change',['append','remove','content'])
def test_inventory_detects_between_pass_changes_and_preserves_traces(journal,monkeypatch,change):
    store,path=journal;store.record(intent());original=store._inventory_names;calls=0
    def changed_names():
        nonlocal calls
        calls+=1
        if calls==2:
            if change=='append':
                # Simulate a NON-cooperating filesystem writer. Public record()
                # is now excluded for the duration of the inventory read.
                value=json.loads(intent_path(path).read_bytes())
                value['intent']['client_order_id']='new-key'
                name=hashlib.sha256(b'broker-intent-v1:new-key').hexdigest()+'.json'
                added=path/name;added.write_bytes(json.dumps(value).encode());added.chmod(0o600)
            elif change=='remove':intent_path(path).unlink()  # Synthetic race only.
            else:
                value=json.loads(intent_path(path).read_bytes());value['intent']['quantity']=2
                intent_path(path).write_bytes(json.dumps(value).encode())
        return original()
    monkeypatch.setattr(store,'_inventory_names',changed_names)
    with pytest.raises(IntentJournalError):store.load_unreconciled_book()
    if change=='append':assert len(list(path.iterdir()))==3
    elif change=='remove':assert not intent_path(path).exists()
    else:assert json.loads(intent_path(path).read_bytes())['intent']['quantity']==2


def test_inventory_bound_is_checked_before_reading_an_unbounded_directory(journal):
    store,path=journal
    for n in range(1025):
        p=path/(f'{n:064x}'+'.json');p.write_bytes(b'partial');p.chmod(0o600)
    with pytest.raises(IntentJournalError):store.load_unreconciled_book()
    assert len(list(path.iterdir()))==1026


def test_same_pin_excludes_all_public_methods_without_wait_or_mutation(journal):
    store,path=journal;store.record(intent());sibling=open_journal(path)
    before={p.name:p.read_bytes() for p in path.iterdir()}
    with store._exclusive_scope():
        for contender in (store,sibling):
            for action in (lambda:contender.read('synthetic-key'),
                           lambda:contender.record(intent(client_order_id='new-key')),
                           contender.load_unreconciled_book):
                with pytest.raises(IntentJournalError) as exc:action()
                assert str(exc.value)==ERROR
    assert {p.name:p.read_bytes() for p in path.iterdir()}==before
    assert sibling.load_unreconciled_book().reconciliation_required


def test_exception_releases_advisory_lock_without_erasing_records(journal):
    store,path=journal;store.record(intent())
    with pytest.raises(RuntimeError):
        with store._exclusive_scope():raise RuntimeError('synthetic failure')
    receipt=open_journal(path).record(intent())
    assert not receipt.created and receipt.reconciliation_required


def test_distinct_journal_is_not_a_global_account_lock(journal,tmp_path):
    store,_=journal;other_path=tmp_path/'different-scope';other_path.mkdir(mode=0o700)
    other=open_journal(other_path,scope_digest='b'*64,initialize=True)
    with store._exclusive_scope():
        receipt=other.record(intent(scope_digest='b'*64))
        assert receipt.created and not receipt.order_submission_approved


def test_pin_replacement_during_locked_operation_is_detected(journal,tmp_path,monkeypatch):
    store,path=journal;raw=store._pin.read_bytes()
    replacement=tmp_path/'replacement';replacement.write_bytes(raw);replacement.chmod(0o600)
    original=store._storage._write_once
    def replace_pin_after_write(*args,**kwargs):
        result=original(*args,**kwargs)
        os.replace(replacement,store._pin)  # Non-cooperating synthetic mutation.
        return result
    monkeypatch.setattr(store._storage,'_write_once',replace_pin_after_write)
    with pytest.raises(IntentJournalError,match=ERROR):store.record(intent())
    assert store._pin.read_bytes()==raw and intent_path(path).is_file()
    assert open_journal(path).read('synthetic-key').reconciliation_required


@pytest.mark.parametrize('crash',[False,True])
def test_other_process_contention_and_normal_or_crash_release_preserve_unknown(journal,crash):
    store,path=journal;store.record(intent());before={p.name:p.read_bytes() for p in path.iterdir()}
    script='''
import os, sys
from astral_stock_agent.broker_intent_journal import PrivateIntentJournal
def deny(event,args):
    if event.startswith('socket.'):raise AssertionError('network forbidden')
sys.addaudithook(deny)
j=PrivateIntentJournal(sys.argv[1],mode='paper',scope_digest='a'*64)
with j._exclusive_scope():
    print('locked',flush=True)
    sys.stdin.buffer.read(1)
    if sys.argv[2]=='crash':os._exit(73)
'''
    root=Path(__file__).resolve().parents[1]
    child=subprocess.Popen([sys.executable,'-c',script,str(path),'crash' if crash else 'normal'],
        stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,
        env=dict(PATH=os.defpath,PYTHONPATH=str(root/'src'),PYTHONDONTWRITEBYTECODE='1'))
    try:
        ready,_,_=select.select([child.stdout],[],[],20)
        assert ready and child.stdout.readline()=='locked\n'
        for action in (lambda:store.record(intent()),lambda:store.read('synthetic-key'),
                       store.load_unreconciled_book):
            with pytest.raises(IntentJournalError):action()
        out,err=child.communicate(input='x',timeout=20)
        assert child.returncode==(73 if crash else 0) and out==err==''
    finally:
        if child.poll() is None:child.kill()
        child.wait(timeout=20)
    receipt=open_journal(path).record(intent())
    assert not receipt.created and receipt.reconciliation_required and not receipt.order_submission_approved
    assert open_journal(path).load_unreconciled_book().reconciliation_required
    assert {p.name:p.read_bytes() for p in path.iterdir()}==before


@pytest.mark.parametrize('mode',['paper','live'])
def test_checked_record_first_intent_is_diagnostic_only_then_unknown_blocks(journal,tmp_path,mode):
    from test_broker_order_preflight import context,limits
    store,path=journal
    if mode=='live':
        path=tmp_path/'synthetic-live';path.mkdir(mode=0o700)
        store=open_journal(path,mode=mode,initialize=True)
    item=intent(mode=mode);ctx=context(mode=mode)
    result=store.preflight_and_record(item,ctx,limits())
    assert result.assessment.checks_passed and result.receipt.created
    assert result.receipt.reconciliation_required and not result.order_submission_approved
    assert not result.receipt.order_submission_approved
    before={p.name:p.read_bytes() for p in path.iterdir()}
    retry=store.preflight_and_record(item,ctx,limits())
    assert retry.receipt is None and 'intent_already_recorded' in retry.assessment.reasons
    other=store.preflight_and_record(replace(item,client_order_id='different'),ctx,limits())
    assert other.receipt is None and other.assessment.reasons==('order_unknown',)
    assert {p.name:p.read_bytes() for p in path.iterdir()}==before


@pytest.mark.parametrize('changes,reason',[
    ({'emergency_stop':True},'emergency_stop'),
    ({'operating_approved':False},'approval_missing'),
    ({'account_reconciled':False},'account_unreconciled'),
    ({'regular_session_verified':False},'session_unverified'),
    ({'daily_loss_stop':True},'daily_loss_stop'),
    ({'current_equity':Decimal('9950')},'daily_loss_limit'),
    ({'scope_digest':'b'*64},'scope_mismatch'),
])
def test_failed_checked_diagnostics_do_not_write(journal,changes,reason):
    from test_broker_order_preflight import context,limits
    store,path=journal;before={p.name:p.read_bytes() for p in path.iterdir()}
    result=store.preflight_and_record(intent(),context(**changes),limits())
    assert reason in result.assessment.reasons and result.receipt is None
    assert not result.order_submission_approved
    assert {p.name:p.read_bytes() for p in path.iterdir()}==before


def test_checked_missing_limits_or_bad_intent_never_default_to_write(journal):
    from test_broker_order_preflight import context,limits
    store,path=journal
    result=store.preflight_and_record(intent(),context())
    assert result.assessment.reasons==('limits_unset',) and result.receipt is None
    result=store.preflight_and_record(None,context(),limits())
    assert result.assessment.reasons==('invalid_input',) and result.receipt is None
    assert len(list(path.iterdir()))==1


def test_checked_lock_spans_assessment_and_record_without_releasing_in_between(journal,monkeypatch):
    from test_broker_order_preflight import context,limits
    import astral_stock_agent.broker_intent_journal as module
    store,path=journal;sibling=open_journal(path);original=module.assess_order_preflight;calls=[]
    def check_while_locked(*args,**kwargs):
        assessment=original(*args,**kwargs)
        assert assessment.checks_passed
        with pytest.raises(IntentJournalError):sibling.record(intent(client_order_id='competing'))
        calls.append(True)
        return assessment
    monkeypatch.setattr(module,'assess_order_preflight',check_while_locked)
    result=store.preflight_and_record(intent(),context(),limits())
    assert calls==[True] and result.receipt.created
    assert len(list(path.iterdir()))==2


def test_competing_checked_keys_cannot_both_prepare_on_same_empty_snapshot(journal):
    from test_broker_order_preflight import context,limits
    _,path=journal;barrier=threading.Barrier(2)
    def attempt(key):
        store=open_journal(path);barrier.wait()
        try:
            result=store.preflight_and_record(intent(client_order_id=key),context(),limits())
            return result.receipt is not None and result.receipt.created
        except IntentJournalError:return False
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(attempt,('one','two')))
    assert sum(results)==1 and len(list(path.iterdir()))==2
    assert open_journal(path).load_unreconciled_book().reconciliation_required


def test_checked_sync_failure_preserves_trace_and_blocks_later_key(journal,monkeypatch):
    from test_broker_order_preflight import context,limits
    store,path=journal
    def fail(path):raise OSError('synthetic private marker')
    monkeypatch.setattr(store._storage,'_sync',fail)
    with pytest.raises(IntentJournalError,match=ERROR):
        store.preflight_and_record(intent(),context(),limits())
    assert intent_path(path).is_file()
    result=open_journal(path).preflight_and_record(intent(client_order_id='other'),context(),limits())
    assert result.receipt is None and result.assessment.reasons==('order_unknown',)


def test_unexpected_preexisting_receipt_after_clean_check_is_not_success(journal,monkeypatch):
    from test_broker_order_preflight import context,limits
    from astral_stock_agent.broker_intent_journal import _encode
    store,path=journal;original=store._record_receipt
    def noncooperating_writer(item):
        # Direct file injection bypasses advisory locking; it must not become a
        # successful preparation if record() finds an existing same-key file.
        p=intent_path(path);p.write_bytes(_encode(item));p.chmod(0o600)
        return original(item)
    monkeypatch.setattr(store,'_record_receipt',noncooperating_writer)
    with pytest.raises(IntentJournalError,match=ERROR):
        store.preflight_and_record(intent(),context(),limits())
    assert open_journal(path).read('synthetic-key').reconciliation_required
