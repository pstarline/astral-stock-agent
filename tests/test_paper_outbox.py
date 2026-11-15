"""Synthetic checkpoint→Outbox handoff; no sender or network calls."""
import sqlite3
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from astral_stock_agent.delivery import Outbox, Telegram
from astral_stock_agent.independent_ledger import IndependentPaperLedger
from astral_stock_agent.paper_outbox import enqueue_latest_paper_report, PaperOutboxError
from test_paper_pipeline import setup, record
from test_price_archive import window, DAYS


@pytest.fixture
def runtime(setup, tmp_path, monkeypatch):
    record(setup,0,3,True)
    directory=tmp_path/'outbox'; directory.mkdir(mode=0o700)
    database=directory/'reports.sqlite'
    Outbox(database,create=True)
    def forbidden(*args,**kwargs):raise AssertionError('handoff cannot send')
    monkeypatch.setattr(Outbox,'deliver',forbidden)
    monkeypatch.setattr(Telegram,'send',forbidden)
    return setup,database


def handoff(runtime):
    setup,database=runtime
    _,now=window(0,3)
    return enqueue_latest_paper_report(paper_directory=setup[2],database=database,now=now)


def test_exact_stored_body_is_queued_once_and_ledger_is_unchanged(runtime):
    setup,database=runtime
    before={p.name:p.read_bytes() for p in setup[2].iterdir()}
    first=handoff(runtime); second=handoff(runtime)
    assert first==second==dict(report_present=True,state='pending',delivery_attempted=False,
                               operator_reconciliation_required=True)
    _,now=window(0,3)
    key,body=IndependentPaperLedger(setup[2]).report_for_latest_session(now=now)
    with sqlite3.connect(database) as db:
        assert db.execute('SELECT report_key,body,state FROM reports').fetchall()==[(key,body,'pending')]
    assert before=={p.name:p.read_bytes() for p in setup[2].iterdir()}


@pytest.mark.parametrize('state',['pending','sending','unknown','sent'])
def test_existing_states_are_preserved_without_resending(runtime,state):
    _,database=runtime
    handoff(runtime)
    with sqlite3.connect(database) as db:db.execute('UPDATE reports SET state=?',(state,))
    result=handoff(runtime)
    assert result['state']==state and not result['delivery_attempted']
    assert result['operator_reconciliation_required']==(state!='sent')


def test_changed_outbox_body_is_not_replaced(runtime):
    _,database=runtime
    handoff(runtime)
    with sqlite3.connect(database) as db:db.execute("UPDATE reports SET body='synthetic conflict'")
    with pytest.raises(PaperOutboxError):handoff(runtime)
    with sqlite3.connect(database) as db:
        assert db.execute('SELECT body FROM reports').fetchone()[0]=='synthetic conflict'


def test_concurrent_handoffs_create_one_row(runtime):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:handoff(runtime),range(2)))
    assert all(r['state']=='pending' for r in results)
    assert Outbox(runtime[1]).status_counts()['pending']==1


def test_crash_after_insert_can_repeat_without_duplicate(runtime,monkeypatch):
    original=Outbox.enqueue
    def interrupted(self,key,body):
        original(self,key,body)
        raise RuntimeError('SYNTHETIC_PRIVATE_ERROR')
    monkeypatch.setattr(Outbox,'enqueue',interrupted)
    with pytest.raises(PaperOutboxError) as error:handoff(runtime)
    assert 'SYNTHETIC' not in str(error.value)
    monkeypatch.setattr(Outbox,'enqueue',original)
    assert handoff(runtime)['state']=='pending'
    assert Outbox(runtime[1]).status_counts()['pending']==1


def test_missing_latest_checkpoint_blocks_before_queue(runtime):
    setup,database=runtime
    _,now=window(1,4)
    with pytest.raises(PaperOutboxError):
        enqueue_latest_paper_report(paper_directory=setup[2],database=database,now=now)
    assert Outbox(database).status_counts()['pending']==0


def test_previous_link_is_required_for_next_session(runtime):
    setup,database=runtime
    record(setup,1,4)
    previous=IndependentPaperLedger(setup[2])._path('session:'+DAYS[2])
    previous.write_bytes(b'{')
    _,now=window(1,4)
    with pytest.raises(PaperOutboxError):
        enqueue_latest_paper_report(paper_directory=setup[2],database=database,now=now)
    assert Outbox(database).status_counts()['pending']==0


def test_public_ci_blocks_before_private_access(runtime,monkeypatch):
    import astral_stock_agent.paper_outbox as module
    def forbidden(*args,**kwargs):raise AssertionError('no private access')
    monkeypatch.setattr(module,'IndependentPaperLedger',forbidden)
    monkeypatch.setenv('GITHUB_ACTIONS','true')
    with pytest.raises(PaperOutboxError):handoff(runtime)


def test_missing_outbox_is_not_silently_initialized(runtime):
    setup,database=runtime
    absent=database.parent/'absent.sqlite'
    _,now=window(0,3)
    with pytest.raises(PaperOutboxError):
        enqueue_latest_paper_report(paper_directory=setup[2],database=absent,now=now)
    assert not absent.exists()


@pytest.mark.parametrize('case',['body','scope'])
def test_changed_stored_body_or_scope_never_reaches_outbox(runtime,case):
    setup,database=runtime
    store=IndependentPaperLedger(setup[2])
    path=store._path('scope' if case=='scope' else 'session:'+DAYS[2])
    data=json.loads(path.read_bytes())
    if case=='body':data['report_body']+='\nsynthetic altered report'
    else:data['scope']['symbol']='SPY'
    path.write_text(json.dumps(data))
    with pytest.raises(PaperOutboxError):handoff(runtime)
    assert Outbox(database).status_counts()['pending']==0


def test_next_session_report_has_a_distinct_key_and_preserves_previous_pending(runtime):
    setup,database=runtime
    handoff(runtime)
    record(setup,1,4)
    _,now=window(1,4)
    result=enqueue_latest_paper_report(paper_directory=setup[2],database=database,now=now)
    assert result['state']=='pending'
    assert Outbox(database).status_counts()['pending']==2
