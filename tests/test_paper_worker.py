"""Private daily scheduler integration with synthetic market and sender only."""
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
import json

import pytest

from astral_stock_agent.delivery import Outbox
from astral_stock_agent.independent_ledger import IndependentPaperLedger, _canonical
from astral_stock_agent.paper_worker import tick, WorkerError
from test_paper_pipeline import setup
from test_price_archive import window, DAYS


@pytest.fixture
def worker(setup,tmp_path,monkeypatch):
    import astral_stock_agent.paper_runtime as runtime
    config,prices,paper=setup
    for name in ('control','outbox'):(tmp_path/name).mkdir(mode=0o700)
    Outbox(tmp_path/'outbox'/'reports.sqlite',create=True)
    env=dict(PAPER_WORKER_ENABLED='true',PAPER_CANDIDATE_APPROVED='true',
        PAPER_REPORTING_ENABLED='true',PAPER_STORAGE_ROOT=str(tmp_path),
        PAPER_CONFIG_JSON=json.dumps(config),PAPER_BOOTSTRAP_SESSION=DAYS[2],
        PAPER_US_SYMBOL='IBM',ALPHAVANTAGE_API_KEY='synthetic',
        TELEGRAM_BOT_TOKEN='synthetic',TELEGRAM_CHAT_ID='synthetic')
    data,now=window(0,3,changed=DAYS[1])
    state=dict(data=data,now=now+timedelta(hours=2),calls=0,sends=0)
    def fetch(**kwargs):state['calls']+=1;return state['data']
    monkeypatch.setattr(runtime,'fetch_alpha_daily',fetch)
    class Sender:
        def __init__(self,*args):pass
        def send(self,body):state['sends']+=1;return 'synthetic-id'
    return env,state,Sender


def run(worker):
    env,state,sender=worker
    return tick(env,clock=lambda:state['now'],sender_factory=sender)


def test_real_pipeline_first_day_restart_next_day(worker):
    assert run(worker)['status']=='completed'
    assert run(worker)['status']=='already_completed'
    state=worker[1]
    state['data'],now=window(1,4,changed=DAYS[1]);state['now']=now+timedelta(hours=2)
    assert run(worker)['status']=='completed'
    assert state['calls']==state['sends']==2


def test_disabled_needs_no_secrets_or_storage(monkeypatch):
    monkeypatch.delenv('GITHUB_ACTIONS',raising=False)
    assert tick({})==dict(status='disabled')


@pytest.mark.parametrize('now',[
    datetime(2026,1,6,20,tzinfo=timezone.utc), # before close
    datetime(2026,1,6,22,tzinfo=timezone.utc), # inside delay
    datetime(2026,1,10,23,tzinfo=timezone.utc), # weekend
    datetime(2026,1,19,23,tzinfo=timezone.utc), # MLK holiday
    datetime(2026,11,27,18,59,tzinfo=timezone.utc), # early close, still inside delay
    datetime(2026,3,9,21,tzinfo=timezone.utc), # DST close20:00 +90min
])
def test_no_requests_outside_session_window(worker,now):
    worker[1]['now']=now
    assert run(worker)['status']=='waiting'
    assert worker[1]['calls']==worker[1]['sends']==0


def test_early_close_after_delay_and_dst_after_delay_use_calendar(worker):
    seen=[]
    for now in [datetime(2026,11,27,19,30,tzinfo=timezone.utc),
                datetime(2026,3,9,21,30,tzinfo=timezone.utc)]:
        # Scheduling only; isolated control stores per loop.
        env=worker[0].copy();root=__import__('pathlib').Path(env['PAPER_STORAGE_ROOT'])/str(len(seen))
        (root/'control').mkdir(parents=True,mode=0o700)
        env['PAPER_STORAGE_ROOT']=str(root);env['PAPER_BOOTSTRAP_SESSION']=now.date().isoformat()
        result=tick(env,clock=lambda:now,runner=lambda *a,**k:seen.append(k) or {'state':'sent'},
                    sender_factory=worker[2])
        assert result['status']=='completed'
    assert len(seen)==2


@pytest.mark.parametrize('change',[('PAPER_WORKER_ENABLED','yes'),('PAPER_CANDIDATE_APPROVED','false'),
    ('PAPER_REPORTING_ENABLED','yes'),('PAPER_CONFIG_JSON','{"symbol":"IBM","symbol":"SPY"}'),
    ('PAPER_US_SYMBOL','SPY'),('PAPER_STORAGE_ROOT','relative')])
def test_bad_configuration_never_fetches(worker,change):
    worker[0][change[0]]=change[1]
    with pytest.raises(WorkerError):run(worker)
    assert not worker[1]['calls'] and not worker[1]['sends']


def test_failure_and_restart_and_next_day_never_retry(worker):
    calls=[]
    def fail(*args,**kwargs):calls.append(1);raise TimeoutError('SYNTHETIC_PRIVATE_TOKEN')
    env,state,sender=worker
    with pytest.raises(WorkerError) as error:
        tick(env,clock=lambda:state['now'],runner=fail,sender_factory=sender)
    assert 'SYNTHETIC' not in str(error.value)
    with pytest.raises(WorkerError):run(worker)
    state['now']+=timedelta(days=1)
    with pytest.raises(WorkerError):run(worker)
    assert len(calls)==1 and state['calls']==state['sends']==0


def test_two_workers_single_attempt(worker):
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(run,worker) for _ in range(2)]
        results=[]
        for f in futures:
            try:results.append(f.result())
            except WorkerError:pass
    assert len(results)>=1
    assert worker[1]['calls']==worker[1]['sends']==1


def test_record_only_never_sends_and_does_not_retroactively_send(worker):
    worker[0]['PAPER_REPORTING_ENABLED']='false'
    assert run(worker)['status']=='completed'
    worker[0]['PAPER_REPORTING_ENABLED']='true'
    assert run(worker)['status']=='already_completed'
    assert worker[1]['calls']==1 and worker[1]['sends']==0


def test_public_ci_before_any_work(worker,monkeypatch):
    monkeypatch.setenv('GITHUB_ACTIONS','true')
    with pytest.raises(WorkerError):run(worker)
    assert worker[1]['calls']==worker[1]['sends']==0
