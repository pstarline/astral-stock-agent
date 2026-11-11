"""Original signal rule invariants; synthetic OHLC only, no strategy returns."""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, Context, localcontext
import hashlib
import json
from pathlib import Path

import pytest

from astral_stock_agent.cli import main
from astral_stock_agent.independent_paper import preview_independent_paper
from astral_stock_agent.independent_signals import SMA_RULE, preview_independent_sma
from astral_stock_agent.research_data import ResearchDataError
from astral_stock_agent.research_sessions import UTC_SESSION_DATE, NY_SESSION_DATE


DAYS = ('2026-01-02','2026-01-05','2026-01-06','2026-01-07','2026-01-08','2026-01-09')
NOW = datetime(2026,1,10,tzinfo=timezone.utc)


def inputs(closes=(10,12,14,8,7,10), *, days=DAYS, label='T00:00:00Z'):
    prices = b'timestamp,open,high,low,close,volume\n' + b''.join(
        f'{day}{label},{price},20,1,{price},100\n'.encode() for day,price in zip(days,closes))
    spec = dict(schema='independent_sma_candidate_v1',symbol='SPY',
        price_sha256=hashlib.sha256(prices).hexdigest(),price_count=len(closes),
        first_day=days[0],last_day=days[len(closes)-1],label_convention=UTC_SESSION_DATE,
        initial_cash='100',commission_bps='0',slippage_bps='0',
        rule=dict(id=SMA_RULE,window=3,units='1',trade_from=DAYS[0]))
    return spec,prices


def preview(spec=None,prices=None,now=NOW):
    s,p=inputs()
    return preview_independent_sma(s if spec is None else spec,p if prices is None else prices,
                                  verified_us_symbols=['SPY'],now=now)


def test_price_derived_signals_reuse_causal_execution_and_hand_accounting():
    manifest,r,body=preview()
    assert [Decimal(s['units_change']) for s in manifest['close_signals']] == [0,0,1,-1,0,1]
    assert Decimal(r['realized_pnl']) == -1
    assert Decimal(r['units']) == 0
    assert Decimal(r['pending_close_change']) == 1
    assert r['cumulative_simulated_fills'] == 2
    assert r['daily_simulated_fills'] == 0
    assert r['signal_method'] == SMA_RULE and r['candidate_only']
    assert '최근 3개 종가 SMA' in body and '자동 채택되지 않은' in body
    assert not r['signal_causality_verified'] and not r['deployment_approved']
    generic, _ = preview_independent_paper(manifest,inputs()[1],verified_us_symbols=['SPY'],now=NOW)
    assert generic['cumulative_pnl'] == r['cumulative_pnl']


def test_same_day_close_cannot_execute_its_own_signal():
    s,p=inputs(closes=(10,12,14,8),days=DAYS[:4])
    manifest,r,_=preview(s,p)
    assert r['daily_simulated_fills'] == 1  # Jan6 signal buys at Jan7 open8
    assert Decimal(r['cash']) == 92 and Decimal(r['units']) == 1
    assert Decimal(r['pending_close_change']) == -1  # Jan7 exit remains pending
    assert manifest['close_signals'][-1]['session'] == '2026-01-07'


def test_future_price_changes_leave_earlier_signals_and_version_identical():
    original=preview()[0]
    s,p=inputs(closes=(10,12,14,8,7,2))
    changed=preview(s,p)[0]
    assert original['close_signals'][:-1] == changed['close_signals'][:-1]
    assert original['close_signals'][-1] != changed['close_signals'][-1]
    assert original['version'] == changed['version']  # prices are not rule version
    for size in (4,5):
        s,p=inputs(closes=(10,12,14,8,7,10)[:size],days=DAYS[:size])
        prefix=preview(s,p)[0]
        assert prefix['close_signals'] == original['close_signals'][:size]
        assert prefix['version'] == original['version']


def test_warmup_and_evaluation_start_are_explicit():
    s,p=inputs(); s['rule']['trade_from']='2026-01-07'
    manifest,r,_=preview(s,p)
    assert all(Decimal(x['units_change'])==0 for x in manifest['close_signals'][:5])
    assert r['cumulative_simulated_fills'] == 0 and Decimal(r['pending_close_change']) == 1


def test_equality_does_not_create_entry_and_never_optimizes_parameters():
    s,p=inputs(closes=(10,10,10,10,10,10))
    manifest,r,_=preview(s,p)
    assert all(Decimal(x['units_change'])==0 for x in manifest['close_signals'])
    assert r['sma_window']==3 and Decimal(r['target_units'])==1


@pytest.mark.parametrize('name,value',[('window',2),('units','2'),('trade_from','2026-01-05')])
def test_rule_parameters_are_bound_to_derived_version(name,value):
    original=preview()[0]['version']
    s,p=inputs();s['rule'][name]=value
    assert preview(s,p)[0]['version'] != original


def test_explicit_costs_are_applied_and_inputs_remain_unchanged():
    s,p=inputs();s.update(commission_bps='5',slippage_bps='5');before=deepcopy(s)
    _,r,_=preview(s,p)
    assert Decimal(r['cumulative_pnl']) < -1
    assert Decimal(r['cumulative_commission']) > 0
    assert s==before


@pytest.mark.parametrize('name,value',[('window',True),('window',1),('window',3.0),
    ('window',253),('units','0'),('units','-1'),('units','NaN'),('units',1.0),
    ('trade_from','2026-01-09'),('trade_from','2025-12-31'),
    ('id','exec_arbitrary_source'),('id',None)])
def test_invalid_or_unexecutable_rule_parameters(name,value):
    s,p=inputs();s['rule'][name]=value
    with pytest.raises(ResearchDataError): preview(s,p)


@pytest.mark.parametrize('case',['rule_extra','extra','manual_signals','missing_cost','wrong_schema',
                                 'unknown_scope','checksum','missing_day'])
def test_explicit_scope_integrity_and_no_implicit_config(case):
    s,p=inputs()
    if case=='rule_extra': s['rule']['source_code']='synthetic forbidden code'
    if case=='extra': s['strategy']='pretend approved'
    if case=='manual_signals': s['close_signals']=[]
    if case=='missing_cost': s.pop('commission_bps')
    if case=='wrong_schema': s['schema']='independent_price_simulation_v1'
    if case=='unknown_scope': s['symbol']='BTC'
    if case=='checksum': s['price_sha256']='bad'
    if case=='missing_day':
        s,p=inputs(closes=(10,12,8,7,10),days=(DAYS[0],DAYS[1],DAYS[3],DAYS[4],DAYS[5]))
    with pytest.raises(ResearchDataError): preview(s,p)


def test_unclosed_session_rejected_before_signal_generation(monkeypatch):
    import astral_stock_agent.independent_signals as module
    def forbidden(*a,**kw): raise AssertionError('no signal generation')
    monkeypatch.setattr(module,'sma_close_decisions',forbidden)
    with pytest.raises(ResearchDataError,match='unclosed'):
        preview(now=datetime(2026,1,9,18,tzinfo=timezone.utc))


def test_datetime_label_semantics_and_hostile_decimal_context():
    s,p=inputs(label='T00:00:00-05:00');s['label_convention']=NY_SESSION_DATE
    expected=preview(s,p)
    hostile=Context(prec=2,Emin=-1,Emax=1)
    for key in hostile.traps: hostile.traps[key]=True
    with localcontext(hostile):
        assert preview(s,p)==expected
    assert [Decimal(x['units_change']) for x in expected[0]['close_signals']]==[0,0,1,-1,0,1]


def test_cli_generated_preview_cannot_access_astral_outbox_or_telegram(tmp_path,monkeypatch,capsys):
    import astral_stock_agent.cli as cli
    def forbidden(*a,**kw): raise AssertionError('no operations')
    for name in ('Outbox','Telegram','ClosingBooks'):
        monkeypatch.setattr(cli,name,forbidden)
    monkeypatch.delenv('GITHUB_ACTIONS',raising=False)
    s,p=inputs();sf=tmp_path/'candidate.json';pf=tmp_path/'prices.csv'
    sf.write_text(json.dumps(s));pf.write_bytes(p)
    args=['preview-sma',str(sf),str(pf),'--us-symbol','SPY']
    assert main(args)==0
    assert '자동 채택되지 않은' in capsys.readouterr().out
    assert main(args+['--send','SYNTHETIC_PRIVATE_VALUE'])==2
    output=capsys.readouterr()
    assert 'SYNTHETIC_PRIVATE_VALUE' not in output.out+output.err
    sf.write_text(sf.read_text().replace('"window": 3','"window": 3, "window": 2'))
    assert main(args)==2
    assert 'blocked' in capsys.readouterr().out


def test_public_ci_blocks_before_reading_or_running_rule(monkeypatch,capsys):
    monkeypatch.setenv('GITHUB_ACTIONS','true')
    def forbidden(*a,**kw): raise AssertionError('no private access')
    monkeypatch.setattr(Path,'open',forbidden)
    assert main(['preview-sma','private.json','private.csv','--us-symbol','SPY'])==2
    assert 'blocked' in capsys.readouterr().out


def test_checkpoint_digest_covers_the_final_candidate_report():
    manifest,accounting,body=preview()
    _,generic_body=preview_independent_paper(manifest,inputs()[1],
                                           verified_us_symbols=['SPY'],now=NOW)
    assert body != generic_body
    assert accounting['report_sha256'] == hashlib.sha256(body.encode()).hexdigest()
    assert accounting['report_sha256'] != hashlib.sha256(generic_body.encode()).hexdigest()


def test_generated_sma_report_can_bootstrap_the_private_forward_ledger(tmp_path, monkeypatch):
    from astral_stock_agent.independent_ledger import IndependentPaperLedger
    monkeypatch.delenv('GITHUB_ACTIONS', raising=False)
    directory = tmp_path / 'ledger'
    directory.mkdir(mode=0o700)
    _, accounting, body = preview()
    result = IndependentPaperLedger(directory).append(
        accounting, body, now=NOW, bootstrap=True)
    assert result['created'] is True and result['session'] == DAYS[-1]
