"""Synthetic private delivery integration; never constructs real Telegram."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from astral_stock_agent.delivery import Outbox
from astral_stock_agent.independent_ledger import IndependentPaperLedger
from astral_stock_agent.paper_outbox import report_latest_paper_once, PaperOutboxError
from astral_stock_agent.paper_runtime import run_market_paper, PaperRuntimeError
from test_paper_pipeline import setup, record
from test_price_archive import window


class Sender:
    def __init__(self, database, fail=False):
        self.database, self.fail, self.bodies = database, fail, []

    def send(self, body):
        # A separate connection observes the committed claim before network I/O.
        with sqlite3.connect(self.database) as db:
            assert db.execute("SELECT state FROM reports ORDER BY rowid DESC LIMIT 1").fetchone()==('sending',)
        self.bodies.append(body)
        if self.fail:
            raise TimeoutError('SYNTHETIC_PRIVATE_PROVIDER_ERROR')
        return 'synthetic-message'


@pytest.fixture
def env(setup, tmp_path):
    record(setup, 0, 3, True)
    root=tmp_path/'queue';root.mkdir(mode=0o700)
    database=root/'reports.sqlite';Outbox(database,create=True)
    return setup, database, Sender(database)


def report(env, **kwargs):
    setup, database, sender=env
    return report_latest_paper_once(paper_directory=setup[2], database=database,
        now=window(0,3)[1], sender=sender, **kwargs)


def test_first_report_exact_body_once_restart_and_ledger_unchanged(env):
    setup,database,sender=env
    before={p.name:p.read_bytes() for p in setup[2].iterdir()}
    result=report(env,delivery_authorized=True)
    assert result['state']=='sent' and result['delivery_attempted']
    key,body=IndependentPaperLedger(setup[2]).report_for_latest_session(now=window(0,3)[1])
    assert sender.bodies==[body] and Outbox(database).state(key)=='sent'
    assert not report(env,delivery_authorized=True)['delivery_attempted']
    assert before=={p.name:p.read_bytes() for p in setup[2].iterdir()}


@pytest.mark.parametrize('authorization',[False,None,1,'true'])
def test_opt_in_is_explicit_and_does_not_even_enqueue(env,authorization):
    with pytest.raises(PaperOutboxError):report(env,delivery_authorized=authorization)
    assert Outbox(env[1]).status_counts()==dict(pending=0,sending=0,sent=0,unknown=0)
    assert not env[2].bodies


@pytest.mark.parametrize('state',['pending','sending','unknown','sent'])
def test_preexisting_rows_never_send_even_pending(env,state):
    key,body=IndependentPaperLedger(env[0][2]).report_for_latest_session(now=window(0,3)[1])
    box=Outbox(env[1]);assert box.enqueue(key,body) is True
    assert box.enqueue(key,body) is False
    with sqlite3.connect(env[1]) as db:db.execute('UPDATE reports SET state=?',(state,))
    result=report(env,delivery_authorized=True)
    assert result['state']==state and not result['delivery_attempted'] and not env[2].bodies


def test_timeout_keeps_unknown_no_retry_and_no_private_error(env):
    env[2].fail=True
    with pytest.raises(PaperOutboxError) as error:report(env,delivery_authorized=True)
    assert 'SYNTHETIC' not in str(error.value)
    result=report(env,delivery_authorized=True)
    assert result['state']=='unknown' and not result['delivery_attempted']
    assert len(env[2].bodies)==1


def test_crash_after_insert_leaves_pending_and_blocks_recovery_send(env,monkeypatch):
    original=Outbox.enqueue
    def interrupted(self,key,body):
        original(self,key,body)
        raise RuntimeError('SYNTHETIC_PRIVATE_ERROR')
    monkeypatch.setattr(Outbox,'enqueue',interrupted)
    with pytest.raises(PaperOutboxError):report(env,delivery_authorized=True)
    monkeypatch.setattr(Outbox,'enqueue',original)
    result=report(env,delivery_authorized=True)
    assert result['state']=='pending' and result['operator_reconciliation_required']
    assert not env[2].bodies


def test_two_concurrent_callers_only_inserter_can_send(env):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:report(env,delivery_authorized=True),range(2)))
    assert sum(r['delivery_attempted'] for r in results)==1
    assert len(env[2].bodies)==1 and Outbox(env[1]).status_counts()['sent']==1


def test_corrupt_report_and_conflicting_queue_body_cannot_send(env):
    key,body=IndependentPaperLedger(env[0][2]).report_for_latest_session(now=window(0,3)[1])
    Outbox(env[1]).enqueue(key,body+' altered')
    with pytest.raises(PaperOutboxError):report(env,delivery_authorized=True)
    assert not env[2].bodies


def test_public_ci_stops_before_private_access(env,monkeypatch):
    monkeypatch.setenv('GITHUB_ACTIONS','true')
    with pytest.raises(PaperOutboxError):report(env,delivery_authorized=True)
    assert not env[2].bodies


def test_runtime_collect_record_send_restart_next_day(setup,tmp_path,monkeypatch):
    import astral_stock_agent.paper_runtime as runtime
    root=tmp_path/'outbox';root.mkdir(mode=0o700)
    database=root/'reports.sqlite';Outbox(database,create=True)
    sender=Sender(database)
    config,prices,paper=setup
    data,now=window(0,3,changed='2026-01-05')
    calls=[]
    def fetch(**kwargs):calls.append(1);return data
    monkeypatch.setattr(runtime,'fetch_alpha_daily',fetch)
    def run(bootstrap=False,**kwargs):
        return run_market_paper(config,price_directory=prices,paper_directory=paper,
            database=database,verified_us_symbols=['IBM'],api_key='SYNTHETIC',
            clock=lambda:now,bootstrap=bootstrap,sender=sender,**kwargs)
    with pytest.raises(PaperRuntimeError):run(True)  # sender alone grants nothing
    assert not calls and not list(prices.iterdir()) and not list(paper.iterdir())
    first=run(True,delivery_authorized=True)
    assert first['state']=='sent' and not first['recipient_receipt_verified']
    assert not run(delivery_authorized=True)['delivery_attempted']
    data,now=window(1,4,changed='2026-01-05')
    assert run(delivery_authorized=True)['state']=='sent'
    assert len(calls)==len(sender.bodies)==2
