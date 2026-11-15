"""Synthetic archive→causal signal→daily ledger tests, no external delivery."""
from copy import deepcopy
from decimal import Decimal

import pytest

from astral_stock_agent.paper_pipeline import record_archived_sma, PaperPipelineError
from astral_stock_agent.independent_ledger import IndependentPaperLedger
from astral_stock_agent.independent_signals import SMA_RULE
from test_price_archive import window, DAYS


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.delenv('GITHUB_ACTIONS', raising=False)
    prices, paper = tmp_path/'prices', tmp_path/'paper'
    prices.mkdir(mode=0o700); paper.mkdir(mode=0o700)
    config = dict(schema='archived_sma_record_v1',symbol='IBM',initial_cash='100',
        commission_bps='5',slippage_bps='5',
        rule=dict(id=SMA_RULE,window=2,units='1',trade_from=DAYS[2]))
    return config, prices, paper


def record(setup, start, end, bootstrap=False):
    config, prices, paper = setup
    data, now = window(start,end,changed=DAYS[1] if start<=1 else None)
    return record_archived_sma(config,data,price_directory=prices,paper_directory=paper,
        verified_us_symbols=['IBM'],now=now,bootstrap=bootstrap)


def test_bootstrap_is_flat_then_pending_signal_executes_next_open_across_restart(setup):
    _, _, paper = setup
    first = record(setup,0,3,True)
    baseline, _, _ = IndependentPaperLedger(paper)._load_checkpoint(DAYS[2])
    assert first['bootstrap'] and not first['delivery_attempted']
    assert baseline['cumulative_simulated_fills'] == 0
    assert Decimal(baseline['equity']) == 100 and Decimal(baseline['pending_close_change']) == 1
    second = record(setup,1,4)
    current, _, _ = IndependentPaperLedger(paper)._load_checkpoint(DAYS[3])
    assert current['daily_simulated_fills'] == 1
    assert Decimal(current['units']) == 1 and Decimal(current['daily_units_change']) == 1
    assert Decimal(current['daily_commission']) > 0
    assert Decimal(current['pending_close_change']) == -1
    repeat = record(setup,1,4)
    assert not repeat['created'] and not repeat['price_history_created']
    record(setup,2,5)
    final, _, _ = IndependentPaperLedger(paper)._load_checkpoint(DAYS[4])
    assert Decimal(final['units']) == 0 and final['daily_simulated_fills'] == 1


@pytest.mark.parametrize('change',['units','window','cost','cash','start'])
def test_pinned_parameters_cannot_change_between_days(setup,change):
    record(setup,0,3,True)
    config, prices, paper = setup
    config = deepcopy(config)
    if change=='units':config['rule']['units']='2'
    if change=='window':config['rule']['window']=3
    if change=='start':config['rule']['trade_from']=DAYS[1]
    if change=='cost':config['commission_bps']='0'
    if change=='cash':config['initial_cash']='200'
    before={str(p):p.read_bytes() for root in (prices,paper) for p in root.iterdir()}
    with pytest.raises(PaperPipelineError):record((config,prices,paper),1,4)
    assert before=={str(p):p.read_bytes() for root in (prices,paper) for p in root.iterdir()}


def test_backdated_bootstrap_and_shared_directory_are_blocked_before_writes(setup):
    config, prices, paper = setup
    config['rule']['trade_from']=DAYS[0]
    with pytest.raises(PaperPipelineError):record(setup,0,3,True)
    config['rule']['trade_from']=DAYS[2]
    with pytest.raises(PaperPipelineError):record((config,prices,prices),0,3,True)
    assert not list(prices.iterdir()) and not list(paper.iterdir())


def test_saved_prices_survive_accounting_failure_and_identical_retry(setup,monkeypatch):
    import astral_stock_agent.paper_pipeline as module
    record(setup,0,3,True)
    original = module.preview_independent_sma
    def failed(*args,**kwargs):raise RuntimeError('SYNTHETIC_PRIVATE_ERROR')
    monkeypatch.setattr(module,'preview_independent_sma',failed)
    with pytest.raises(PaperPipelineError) as error:record(setup,1,4)
    assert 'SYNTHETIC' not in str(error.value)
    monkeypatch.setattr(module,'preview_independent_sma',original)
    result=record(setup,1,4)
    assert result['created'] and not result['price_history_created']


def test_public_ci_blocks_before_store_construction(setup,monkeypatch):
    import astral_stock_agent.paper_pipeline as module
    def forbidden(*args,**kwargs):raise AssertionError('no private state access')
    monkeypatch.setattr(module,'IndependentPaperLedger',forbidden)
    monkeypatch.setenv('GITHUB_ACTIONS','true')
    with pytest.raises(PaperPipelineError):record(setup,0,3,True)


def test_exact_warmup_can_bootstrap_with_only_a_pending_signal(setup):
    config, _, paper = setup
    config['rule']['trade_from']=DAYS[1]
    result=record(setup,0,2,True)
    accounting,_,_=IndependentPaperLedger(paper)._load_checkpoint(DAYS[1])
    assert result['created'] and accounting['cumulative_simulated_fills']==0
