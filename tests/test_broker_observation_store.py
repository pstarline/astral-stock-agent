"""Only synthetic temporary storage; never operating directories or APIs."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
import subprocess
import sys
import threading

import pytest

from astral_stock_agent.broker_observation_store import (
    ERROR, MAX_RECORDS, ObservationStoreError, PrivateObservationStore,
)
from astral_stock_agent.broker_order_state import OrderBook
from test_broker_observation_codec import baseline, chain, QUERY, SCOPE


def open_store(path, **changes):
    return PrivateObservationStore(path, **(dict(mode='paper', scope_digest=SCOPE) | changes))


def record(store, key='capture', book=None, pages=None):
    return store.record(key, book=book or baseline(), query_digest=QUERY,
                        pages=chain() if pages is None else pages)


def read(store, key='capture', book=None):
    return store.read(key, book=book or baseline(), query_digest=QUERY)


def snapshot(path):
    return {p.name:p.read_bytes() for p in path.iterdir()}


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.delenv('GITHUB_ACTIONS', raising=False)
    path=tmp_path/'observations-only';path.mkdir(mode=0o700)
    return open_store(path, initialize=True), path


@pytest.mark.parametrize('mode', ['paper', 'live'])
def test_record_reopen_read_replay_preserves_bytes_and_no_authority(tmp_path, monkeypatch, mode):
    monkeypatch.delenv('GITHUB_ACTIONS', raising=False)
    path=tmp_path/'evidence';path.mkdir(mode=0o700)
    store=open_store(path, mode=mode, initialize=True)
    result=record(store,book=baseline(mode),pages=chain(mode))
    before=snapshot(path)
    reopened=open_store(path, mode=mode)
    assert read(reopened,book=baseline(mode)).pages==chain(mode)
    assert not record(reopened,book=baseline(mode),pages=chain(mode)).created
    assert snapshot(path)==before and len(before)==2
    assert all(p.stat().st_mode & 0o777==0o600 for p in path.iterdir())
    assert result.created and result.reconciliation_required and not result.order_submission_approved
    assert 'capture' not in repr(result) and 'broker1' not in repr(result)


def test_explicit_empty_bootstrap_only_and_default_ci_block(tmp_path, monkeypatch):
    monkeypatch.delenv('GITHUB_ACTIONS', raising=False)
    path=tmp_path/'empty';path.mkdir(mode=0o700)
    with pytest.raises(ObservationStoreError,match=ERROR):open_store(path)
    assert not list(path.iterdir())
    foreign=path/'foreign';foreign.write_bytes(b'partial');foreign.chmod(0o600)
    with pytest.raises(ObservationStoreError):open_store(path,initialize=True)
    assert snapshot(path)=={'foreign':b'partial'}
    monkeypatch.setenv('GITHUB_ACTIONS','true')
    with pytest.raises(ObservationStoreError):open_store(path,initialize=True)


@pytest.mark.parametrize('change', ['contents','baseline','query','mode','scope'])
def test_conflicts_preserve_all_original_bytes(store, change):
    s,path=store;record(s);before=snapshot(path)
    with pytest.raises(ObservationStoreError,match=ERROR):
        if change=='contents':record(s,pages=(replace(chain()[0],next_cursor=None),))
        elif change=='baseline':read(s,book=OrderBook('paper',SCOPE))
        elif change=='query':s.read('capture',book=baseline(),query_digest='c'*64)
        elif change=='mode':open_store(path,mode='live')
        else:open_store(path,scope_digest='c'*64)
    assert snapshot(path)==before


@pytest.mark.parametrize('key', ['', '../capture', 'x'*65, True])
def test_invalid_key_is_fixed_error_without_write(store,key):
    s,path=store;before=snapshot(path)
    with pytest.raises(ObservationStoreError) as error:record(s,key)
    assert str(error.value)==ERROR and snapshot(path)==before


@pytest.mark.parametrize('damage', ['partial','payload','extra','wrong_key','mode','world_readable','symlink','hardlink'])
def test_corrupt_foreign_or_unsafe_records_preserved_and_blocked(store,tmp_path,damage):
    s,path=store;record(s);p=s._path('capture')
    if damage=='partial':p.write_bytes(b'{')
    elif damage=='world_readable':p.chmod(0o644)
    elif damage=='symlink':
        target=tmp_path/'outside';target.write_bytes(p.read_bytes());target.chmod(0o600)
        p.unlink();p.symlink_to(target)
    elif damage=='hardlink':os.link(p,tmp_path/'linked')
    else:
        value=json.loads(p.read_bytes())
        if damage=='payload':value['observation_b64']='eA=='
        elif damage=='extra':value['approved']=True
        elif damage=='wrong_key':value['evidence_id']='other'
        else:value['mode']='live'
        p.write_text(json.dumps(value))
    before=p.read_bytes()
    for action in (lambda:open_store(path),lambda:read(s),lambda:record(s,'other')):
        with pytest.raises(ObservationStoreError,match=ERROR):action()
    assert p.read_bytes()==before and len(list(path.iterdir()))==2


def test_record_limit_blocks_new_key_but_keeps_exact_replay(store):
    s,path=store
    for n in range(MAX_RECORDS):record(s,str(n))
    before=snapshot(path)
    with pytest.raises(ObservationStoreError):record(s,'overflow')
    assert not record(s,'0').created and snapshot(path)==before


def test_sync_failure_preserves_evidence_without_claiming_prior_ack(store,monkeypatch):
    s,path=store
    def fail(_):raise OSError('private-sentinel')
    monkeypatch.setattr(s._storage,'_sync',fail)
    with pytest.raises(ObservationStoreError) as error:record(s)
    assert str(error.value)==ERROR and s._path('capture').is_file()
    before=snapshot(path)
    # A readable artifact is EVIDENCE only, never an acknowledgment or approved book.
    result=read(open_store(path))
    assert result.reconciliation_required and not result.order_submission_approved
    assert not result.created and snapshot(path)==before


def test_same_key_competing_contents_never_overwrite(store):
    s,path=store;barrier=threading.Barrier(2)
    def attempt(quantity):
        pages=(replace(chain()[0],next_cursor=None,observations=(
            replace(chain()[0].observations[0],filled_quantity=quantity),)),)
        barrier.wait()
        try:return record(s,pages=pages).created
        except ObservationStoreError:return False
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(attempt,(1,2)))
    assert sum(results)==1 and len(list(path.iterdir()))==2
    assert read(open_store(path)).pages[0].observations[0].filled_quantity in (1,2)


def test_subprocess_restart_read_and_nonblocking_lock_contention(store):
    s,path=store;record(s);before=snapshot(path)
    script='''
import sys
from decimal import Decimal
from astral_stock_agent.broker_order_state import OrderBook,OrderIntent,OrderRecord
from astral_stock_agent.broker_observation_store import PrivateObservationStore,ObservationStoreError
def deny(event,args):
 if event.startswith('socket.'):raise AssertionError('network forbidden')
sys.addaudithook(deny)
try:
 s=PrivateObservationStore(sys.argv[1],mode='paper',scope_digest='a'*64)
except ObservationStoreError:
 assert sys.argv[2]=='busy'
else:
 assert sys.argv[2]=='read'
 b=OrderBook('paper','a'*64,tuple(OrderRecord(OrderIntent('paper','a'*64,k,'SPY','buy',3,Decimal('10')),'unknown') for k in ('first','second')))
 r=s.read('capture',book=b,query_digest='b'*64)
 assert r.pages[0].observations[0].filled_quantity==1
 assert r.reconciliation_required and not r.order_submission_approved
print('ok')
'''
    def child(action):
        result=subprocess.run([sys.executable,'-c',script,str(path),action],capture_output=True,text=True,timeout=20)
        assert result.returncode==0 and result.stdout.strip()=='ok'
    child('read')
    with s._exclusive():child('busy')
    child('read')
    assert snapshot(path)==before


def test_old_store_namespace_is_rejected_without_mutation(tmp_path,monkeypatch):
    from astral_stock_agent.broker_intent_journal import PrivateIntentJournal
    monkeypatch.delenv('GITHUB_ACTIONS',raising=False)
    path=tmp_path/'intent';path.mkdir(mode=0o700)
    PrivateIntentJournal(path,mode='paper',scope_digest=SCOPE,initialize=True)
    before=snapshot(path)
    with pytest.raises(ObservationStoreError):open_store(path,initialize=True)
    assert snapshot(path)==before


def two_steps(s, mode='paper'):
    from astral_stock_agent.broker_observation_pages import apply_observation_pages
    original=baseline(mode)
    first=chain(mode)
    record(s,'one',book=original,pages=first)
    partial=apply_observation_pages(original,query_digest=QUERY,pages=first)
    second=(replace(first[0],query_digest='c'*64,next_cursor=None,observations=(
        replace(first[0].observations[0],filled_quantity=2),)),)
    s.record('two',book=partial,query_digest='c'*64,pages=second)
    expected=apply_observation_pages(partial,query_digest='c'*64,pages=second)
    return original,expected,(('one',QUERY),('two','c'*64))


@pytest.mark.parametrize('mode',['paper','live'])
def test_declared_chain_replays_under_one_lock_without_writes_or_authority(tmp_path,monkeypatch,mode):
    monkeypatch.delenv('GITHUB_ACTIONS',raising=False)
    path=tmp_path/'sequence';path.mkdir(mode=0o700)
    s=open_store(path,mode=mode,initialize=True)
    original,expected,steps=two_steps(s,mode)
    before=snapshot(path);records=original.records
    def forbid(*args,**kwargs):raise AssertionError('replay must not write or sync')
    monkeypatch.setattr(s._storage,'_write_once',forbid)
    monkeypatch.setattr(s._storage,'_sync',forbid)
    result=s.read_chain(book=original,steps=steps)
    assert result.book==expected and result.book.records[1] is original.records[1]
    assert original.records is records and records[0].status=='unknown'
    assert result.reconciliation_required and not result.order_submission_approved
    assert not result.book.order_submission_approved
    assert s.read_chain(book=original,steps=steps)==result
    assert 'broker1' not in repr(result) and 'first' not in repr(result)
    assert snapshot(path)==before


@pytest.mark.parametrize('bad',[
    (),[],(None,),(('one',),),(('one',QUERY,'extra'),),
    (('one',True),),(('../private-sentinel',QUERY),),
    (('one',QUERY),('one',QUERY)),(('missing',QUERY),),
    (('two','c'*64),('one',QUERY)),(('two','c'*64),),
    (('one','d'*64),('two','c'*64)),(('one',QUERY),('two',QUERY)),
    (('one',QUERY),('missing','c'*64)),tuple((str(i),QUERY) for i in range(MAX_RECORDS+1)),
])
def test_chain_failure_never_returns_partial_state_or_changes_storage(store,bad):
    s,path=store;original,_,_=two_steps(s);records=original.records;before=snapshot(path)
    with pytest.raises(ObservationStoreError) as error:s.read_chain(book=original,steps=bad)
    assert str(error.value)==ERROR and 'private-sentinel' not in repr(error.value)
    assert original.records is records and records[0].status=='unknown'
    assert snapshot(path)==before


def test_changed_baseline_and_bad_later_payload_do_not_publish_first_step(store):
    import base64
    from hashlib import sha256
    s,path=store;original,expected,steps=two_steps(s)
    with pytest.raises(ObservationStoreError):s.read_chain(book=expected,steps=steps)
    p=s._path('two');envelope=json.loads(p.read_bytes())
    payload=json.loads(base64.b64decode(envelope['observation_b64']))
    payload['pages'][0]['observations'][0]['filled_quantity']=0
    raw=json.dumps(payload).encode()
    envelope.update(observation_b64=base64.b64encode(raw).decode(),payload_sha256=sha256(raw).hexdigest())
    p.write_text(json.dumps(envelope));before=snapshot(path)
    with pytest.raises(ObservationStoreError):s.read_chain(book=original,steps=steps)
    assert original.records[0].status=='unknown' and snapshot(path)==before


def test_chain_holds_lock_across_every_decode(store,monkeypatch):
    s,path=store;original,_,steps=two_steps(s);decode=s._decode;calls=[]
    def observe(*args):
        with pytest.raises(ObservationStoreError):record(s,'concurrent')
        calls.append(1)
        return decode(*args)
    monkeypatch.setattr(s,'_decode',observe)
    s.read_chain(book=original,steps=steps)
    assert len(calls)==2 and not s._path('concurrent').exists()


def test_inventory_change_after_first_stage_is_rejected_and_trace_preserved(store,monkeypatch):
    s,path=store;original,_,steps=two_steps(s);decode=s._decode;inserted=[]
    def noncooperating(*args):
        result=decode(*args)
        if not inserted:
            envelope=json.loads(s._storage._read(s._path('one')))
            envelope['evidence_id']='unexpected'
            s._storage._write_once(s._path('unexpected'),json.dumps(envelope).encode(),'conflict')
            inserted.append(True)
        return result
    monkeypatch.setattr(s,'_decode',noncooperating)
    with pytest.raises(ObservationStoreError):s.read_chain(book=original,steps=steps)
    assert s._path('unexpected').is_file() and original.records[0].status=='unknown'


def test_noop_declarations_cannot_prove_chronological_completeness(store):
    s,path=store;original=baseline()
    empty=(replace(chain()[1],request_cursor=None),)
    record(s,'later',book=original,pages=empty)
    record(s,'earlier',book=original,pages=empty)
    # Both have the same baseline. The caller can omit or reverse them: no clock proof.
    for steps in [(('later',QUERY),),(('later',QUERY),('earlier',QUERY))]:
        result=s.read_chain(book=original,steps=steps)
        assert result.book is original
        assert result.reconciliation_required and not result.order_submission_approved
