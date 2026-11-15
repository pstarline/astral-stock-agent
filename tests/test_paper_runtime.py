"""Synthetic one-shot private runtime and CLI; no provider or sender network."""
from datetime import timedelta
import json
import sqlite3

import pytest

import astral_stock_agent.paper_runtime as runtime
from astral_stock_agent.delivery import Outbox, Telegram
from astral_stock_agent.paper_runtime import run_market_paper, PaperRuntimeError
from test_paper_pipeline import setup
from test_price_archive import window


@pytest.fixture
def env(setup,tmp_path,monkeypatch):
    config,prices,paper=setup
    directory=tmp_path/'queue';directory.mkdir(mode=0o700)
    database=directory/'reports.sqlite';Outbox(database,create=True)
    state=dict(data=window(0,3,changed='2026-01-05')[0],now=window(0,3)[1],calls=0)
    def fetch(**kwargs):
        state['calls']+=1
        return state['data']
    def forbidden(*args,**kwargs):raise AssertionError('runtime cannot send')
    monkeypatch.setattr(runtime,'fetch_alpha_daily',fetch)
    monkeypatch.setattr(Outbox,'deliver',forbidden)
    monkeypatch.setattr(Telegram,'send',forbidden)
    return config,prices,paper,database,state


def run(env,bootstrap=False):
    config,prices,paper,database,state=env
    return run_market_paper(config,price_directory=prices,paper_directory=paper,
        database=database,verified_us_symbols=['IBM'],api_key='SYNTHETICKEY',
        bootstrap=bootstrap,clock=lambda:state['now'])


def test_one_collection_records_queues_and_restart_skips_collection(env):
    result=run(env,True)
    assert result['collection_performed'] and not result['delivery_attempted']
    assert result['state']=='pending' and result['candidate_only']
    assert not run(env)['collection_performed']
    assert env[-1]['calls']==1
    state=env[-1]
    state['data'],state['now']=window(1,4,changed='2026-01-05')
    assert run(env)['collection_performed']
    assert state['calls']==2 and Outbox(env[3]).status_counts()['pending']==2


@pytest.mark.parametrize('state',['pending','sending','unknown','sent'])
def test_recorded_session_preserves_state_and_never_recollects_or_sends(env,state):
    run(env,True)
    with sqlite3.connect(env[3]) as db:db.execute('UPDATE reports SET state=?',(state,))
    result=run(env)
    assert result['state']==state and not result['collection_performed']
    assert env[-1]['calls']==1


@pytest.mark.parametrize('case',['bad_rule','bad_cost','oversize_warmup','missing_queue','shared_storage','no_bootstrap'])
def test_invalid_preconditions_fail_before_consuming_market_request(env,case):
    config,prices,paper,database,state=env
    if case=='bad_rule':config['rule']['id']='other'
    if case=='bad_cost':config['slippage_bps']='-1'
    if case=='oversize_warmup':config['rule']['window']=101
    if case=='missing_queue':database=database.parent/'missing.sqlite'
    if case=='shared_storage':paper=prices
    with pytest.raises(PaperRuntimeError):
        run((config,prices,paper,database,state),case!='no_bootstrap')
    assert state['calls']==0


def test_changed_pinned_configuration_blocks_even_if_checkpoint_exists(env):
    run(env,True)
    env[0]['rule']['units']='2'
    with pytest.raises(PaperRuntimeError):run(env)
    assert env[-1]['calls']==1


def test_queue_failure_keeps_checkpoint_then_retry_skips_second_market_request(env,monkeypatch):
    original=runtime.enqueue_latest_paper_report
    def fail(**kwargs):raise RuntimeError('SYNTHETIC_SECRET_ERROR')
    monkeypatch.setattr(runtime,'enqueue_latest_paper_report',fail)
    with pytest.raises(PaperRuntimeError) as error:run(env,True)
    assert 'SYNTHETIC' not in str(error.value)
    monkeypatch.setattr(runtime,'enqueue_latest_paper_report',original)
    result=run(env)
    assert not result['collection_performed'] and env[-1]['calls']==1


def test_session_boundary_during_collection_blocks_recording(env,monkeypatch):
    original=runtime.fetch_alpha_daily
    def late(**kwargs):
        result=original(**kwargs)
        env[-1]['now']+=timedelta(days=1)
        return result
    monkeypatch.setattr(runtime,'fetch_alpha_daily',late)
    with pytest.raises(PaperRuntimeError):run(env,True)
    assert not list(env[1].iterdir()) and not list(env[2].iterdir())


def test_public_ci_blocks_before_clock_or_io(env,monkeypatch):
    monkeypatch.setenv('GITHUB_ACTIONS','true')
    with pytest.raises(PaperRuntimeError):run(env,True)
    assert env[-1]['calls']==0


def test_cli_runs_whole_private_path_without_printing_prices_or_key(env,tmp_path,monkeypatch,capsys):
    import astral_stock_agent.cli as cli
    class Clock:
        @classmethod
        def now(cls,zone):return env[-1]['now']
    monkeypatch.setattr(runtime,'datetime',Clock)
    monkeypatch.setenv('ALPHAVANTAGE_API_KEY','SYNTHETICKEY')
    config=tmp_path/'config.json';config.write_text(json.dumps(env[0]))
    args=['run-paper',str(config),'--price-directory',str(env[1]),
          '--paper-directory',str(env[2]),'--database',str(env[3]),'--us-symbol','IBM','--bootstrap']
    assert cli.main(args)==0
    output=capsys.readouterr().out
    assert 'SYNTHETICKEY' not in output and 'cash' not in output
    assert json.loads(output)['state']=='pending'
    config.write_text(config.read_text().replace('"window": 2','"window": 2, "window": 3'))
    assert cli.main(args)==2
    assert env[-1]['calls']==1
