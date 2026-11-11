"""Synthetic official-shape daily inputs; no real API keys/data in public CI."""
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from decimal import Context, localcontext, Decimal
import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from astral_stock_agent.cli import main
import astral_stock_agent.market_data as market
from astral_stock_agent.independent_signals import SMA_RULE
from astral_stock_agent.research_sessions import UTC_SESSION_DATE


NOW=datetime(2026,1,8,12,tzinfo=timezone.utc)
DAYS=('2026-01-02','2026-01-05','2026-01-06','2026-01-07')
SECRET='SYNTHETICAPIKEY'


def response():
    return {'Meta Data':{
        '1. Information':'Daily Prices (open, high, low, close) and Volumes',
        '2. Symbol':'IBM','3. Last Refreshed':DAYS[-1],
        '4. Output Size':'Compact','5. Time Zone':'US/Eastern'},
        'Time Series (Daily)':{d:dict(zip(market.ROW_FIELDS,(str(p),str(p+1),str(p-1),str(p),'100')))
                              for d,p in reversed(list(zip(DAYS,(10,12,14,8))))}}


def raw(value=None):
    return json.dumps(response() if value is None else value).encode()


def normalize(data=None,now=NOW):
    return market.normalize_alpha_daily(raw() if data is None else data,
                                       symbol='IBM',verified_us_symbols=['IBM'],now=now)


def config():
    return dict(schema='alpha_sma_preview_v1',symbol='IBM',initial_cash='100',
                commission_bps='5',slippage_bps='5',
                rule=dict(id=SMA_RULE,window=2,units='1',trade_from=DAYS[0]))


class Reply:
    status=200
    def __init__(self,data):self.data=data;self.bounds=[];self.closed=False
    def __enter__(self):return self
    def __exit__(self,*a):self.closed=True
    def read(self,bound):self.bounds.append(bound);return self.data[:bound]


class Transport:
    def __init__(self,data=None,error=None):
        self.reply=Reply(raw() if data is None else data);self.error=error;self.calls=[]
    def open(self,request,*,timeout):
        self.calls.append((request,timeout))
        if self.error:raise self.error
        return self.reply


@pytest.fixture
def wire(monkeypatch):
    monkeypatch.delenv('GITHUB_ACTIONS',raising=False)
    transport=Transport()
    monkeypatch.setattr(market,'build_opener',lambda *a:transport)
    return transport


def fetch(**overrides):
    args=dict(symbol='IBM',api_key=SECRET,verified_us_symbols=['IBM'],clock=lambda:NOW)
    args.update(overrides)
    return market.fetch_alpha_daily(**args)


def test_complete_raw_rows_are_sorted_preserved_and_bound_to_digest():
    data=normalize()
    assert data.symbol=='IBM' and data.count==4
    assert data.first_day.isoformat()==DAYS[0] and data.last_day.isoformat()==DAYS[-1]
    assert [b.timestamp.date().isoformat() for b in data.history.bars]==list(DAYS)
    assert [b.open for b in data.history.bars]==[10,12,14,8]
    assert data.label_convention==UTC_SESSION_DATE and data.price_basis=='raw_as_traded'
    assert data.csv_sha256==hashlib.sha256(data.csv_bytes).hexdigest()
    assert data.response_sha256==hashlib.sha256(raw()).hexdigest()
    assert not data.provider_authenticity_verified and not data.corporate_actions_verified
    assert 'DailyBar' not in repr(data) and 'timestamp,open' not in repr(data)


@pytest.mark.parametrize('field,value',[
    ('2. Symbol','SPY'),('3. Last Refreshed','2026-01-06'),
    ('3. Last Refreshed','2026-01-08'),('3. Last Refreshed','2026-01-07 16:00:00'),
    ('4. Output Size','Full size'),('5. Time Zone','UTC'),
    ('1. Information','Daily Time Series with Splits and Dividend Events')])
def test_wrong_symbol_freshness_format_zone_and_premium_shape_rejected(field,value):
    r=response();r['Meta Data'][field]=value
    with pytest.raises(market.MarketDataError):normalize(raw(r))


@pytest.mark.parametrize('case',['gap','future','weekend','empty','extra_row_field','missing_ohlc',
                                 'zero_price','bad_ohlc','negative_volume','fractional_volume',
                                 'float_price','nan','too_many'])
def test_no_bad_row_drops_imputation_or_future_bars(case):
    r=response();series=r['Time Series (Daily)'];row=series[DAYS[-1]]
    if case=='gap':series.pop(DAYS[1])
    if case=='future':series['2026-01-08']=deepcopy(row)
    if case=='weekend':series['2026-01-04']=deepcopy(row)
    if case=='empty':series.clear()
    if case=='extra_row_field':row['6. adjusted close']='8'
    if case=='missing_ohlc':row.pop('1. open')
    if case=='zero_price':row['1. open']='0'
    if case=='bad_ohlc':row['2. high']='2'
    if case=='negative_volume':row['5. volume']='-1'
    if case=='fractional_volume':row['5. volume']='1.5'
    if case=='float_price':row['1. open']=8.0
    if case=='nan':row['1. open']='NaN'
    if case=='too_many':
        for i in range(101):series[str(i)]=row
    with pytest.raises(market.MarketDataError):normalize(raw(r))


@pytest.mark.parametrize('bad',[b'',b'<html>blocked</html>',b'{"Note":"SYNTHETICAPIKEY https://private.invalid"}',
    b'{"Information":"premium required"}',b'{"Error Message":"unknown symbol"}',b'x'*(market.MAX_RESPONSE_BYTES+1)])
def test_empty_html_throttle_errors_and_size_limits_never_reflect_provider_text(bad):
    with pytest.raises(market.MarketDataError) as e:normalize(bad)
    assert SECRET not in str(e.value) and 'https://' not in str(e.value)


def test_duplicate_response_fields_and_dates_are_rejected():
    for data in [raw().replace(b'"2. Symbol": "IBM"',b'"2. Symbol": "IBM", "2. Symbol": "IBM"'),
                 raw().replace(b'"2026-01-07":',b'"2026-01-02":')]:
        with pytest.raises(market.MarketDataError):normalize(data)


def test_freshness_tracks_market_close_instead_of_utc_date():
    assert normalize(now=datetime(2026,1,8,20,59,tzinfo=timezone.utc)).last_day.isoformat()==DAYS[-1]
    with pytest.raises(market.MarketDataError):normalize(now=datetime(2026,1,8,21,tzinfo=timezone.utc))
    with pytest.raises(market.MarketDataError):normalize(now=datetime(2026,1,8))


def test_one_fixed_origin_get_has_no_paid_parameters_and_a_bounded_closed_response(wire):
    assert fetch().count==4
    assert len(wire.calls)==1
    request,timeout=wire.calls[0];url=urlsplit(request.full_url)
    assert (url.scheme,url.netloc,url.path)==('https','www.alphavantage.co','/query')
    assert parse_qs(url.query)=={'function':['TIME_SERIES_DAILY'],'symbol':['IBM'],'apikey':[SECRET]}
    assert timeout==15 and request.get_method()=='GET'
    assert wire.reply.bounds==[market.MAX_RESPONSE_BYTES+1] and wire.reply.closed
    assert market._NoRedirect().redirect_request(None,None,302,'',{},'https://elsewhere.invalid') is None


@pytest.mark.parametrize('key',[None,'','bad/key','bad?key','bad key','x'*129])
def test_invalid_key_does_not_touch_network(wire,key):
    with pytest.raises(market.MarketDataError):fetch(api_key=key)
    assert wire.calls==[]


def test_wrong_scope_or_demo_symbol_does_not_touch_network(wire):
    for args in [dict(symbol='IBM/evil'),dict(verified_us_symbols=['SPY']),
                 dict(symbol='SPY',verified_us_symbols=['SPY'],api_key='demo')]:
        with pytest.raises(market.MarketDataError):fetch(**args)
    assert wire.calls==[]


def test_network_errors_are_sanitized_and_never_retried(monkeypatch):
    monkeypatch.delenv('GITHUB_ACTIONS',raising=False)
    transport=Transport(error=RuntimeError('https://private.invalid/'+SECRET))
    monkeypatch.setattr(market,'build_opener',lambda *a:transport)
    with pytest.raises(market.MarketDataError) as e:fetch()
    assert len(transport.calls)==1 and SECRET not in str(e.value)
    assert 'https://' not in str(e.value)


@pytest.mark.parametrize('offset',[-1,61])
def test_clock_reversal_or_excess_elapsed_blocks_result(wire,offset):
    ticks=iter((NOW,NOW+timedelta(seconds=offset)))
    with pytest.raises(market.MarketDataError):fetch(clock=lambda:next(ticks))
    assert wire.reply.closed


def test_end_to_end_market_candidate_is_a_replay_not_forward_operation(wire):
    c=config();before=deepcopy(c)
    data,manifest,accounting,body=market.collect_sma_preview(c,api_key=SECRET,
        verified_us_symbols=['IBM'],clock=lambda:NOW)
    assert len(wire.calls)==1 and c==before
    assert manifest['price_sha256']==data.csv_sha256
    assert Decimal(accounting['units'])==1  # buy Jan6 at14, Jan7 exit remains pending
    assert Decimal(accounting['pending_close_change'])==-1
    assert accounting['report_sha256']==hashlib.sha256(body.encode()).hexdigest()
    assert accounting['provider_response_sha256']==data.response_sha256
    assert 'raw as traded' in body and '영속 forward 운영 아님' in body
    assert not accounting['deployment_approved'] and not accounting['actual_execution_verified']


@pytest.mark.parametrize('field,value',[('commission_bps',None),('slippage_bps',-1),('initial_cash',0),
    ('rule',{'id':'arbitrary_code'}),('schema','wrong')])
def test_static_bad_configuration_does_not_consume_a_daily_request(wire,field,value):
    c=config();c[field]=value
    with pytest.raises(market.MarketDataError):market.collect_sma_preview(c,api_key=SECRET,
        verified_us_symbols=['IBM'],clock=lambda:NOW)
    assert wire.calls==[]


def test_public_ci_blocks_client_and_collector_before_clock_or_network(wire,monkeypatch):
    monkeypatch.setenv('GITHUB_ACTIONS','true')
    def forbidden():raise AssertionError('no clock')
    with pytest.raises(market.MarketDataError):fetch(clock=forbidden)
    with pytest.raises(market.MarketDataError):market.collect_sma_preview(config(),api_key=SECRET,
        verified_us_symbols=['IBM'],clock=forbidden)
    assert wire.calls==[]


def test_cli_blocks_before_private_reads_and_never_accepts_key_arguments(monkeypatch,capsys):
    monkeypatch.setenv('GITHUB_ACTIONS','true')
    def forbidden(*a,**k):raise AssertionError('no private file access')
    monkeypatch.setattr(Path,'open',forbidden)
    command=['preview-market-sma','private.json','--us-symbol','IBM']
    assert main(command)==2
    assert main(command+['--api-key',SECRET])==2
    out=capsys.readouterr()
    assert SECRET not in out.out+out.err and 'blocked' in out.out+out.err


def test_cli_uses_environment_only_without_outbox_or_order_access(tmp_path,monkeypatch,capsys,wire):
    import astral_stock_agent.cli as cli
    p=tmp_path/'config.json';p.write_text(json.dumps(config()))
    monkeypatch.setenv('ALPHAVANTAGE_API_KEY',SECRET)
    def forbidden(*a,**k):raise AssertionError('no operational access')
    monkeypatch.setattr(cli,'Outbox',forbidden);monkeypatch.setattr(cli,'Telegram',forbidden)
    original=market.collect_sma_preview
    monkeypatch.setattr(market,'collect_sma_preview',lambda c,**kw:original(c,**kw,clock=lambda:NOW))
    command=['preview-market-sma',str(p),'--us-symbol','IBM']
    assert main(command)==0
    out=capsys.readouterr();assert 'Alpha Vantage' in out.out and SECRET not in out.out+out.err
    p.write_text(p.read_text().replace('"window": 2','"window": 2, "window": 3'))
    assert main(command)==2
    assert len(wire.calls)==1


def test_normalization_is_independent_of_decimal_context():
    expected=normalize()
    hostile=Context(prec=2,Emin=-1,Emax=1)
    for key in hostile.traps:hostile.traps[key]=True
    with localcontext(hostile):assert normalize()==expected


def test_compact_window_cannot_satisfy_large_sma_and_does_not_query(wire):
    c=config();c['rule']['window']=100
    with pytest.raises(market.MarketDataError):market.collect_sma_preview(c,api_key=SECRET,
        verified_us_symbols=['IBM'],clock=lambda:NOW)
    assert wire.calls==[]


def test_non_success_status_closes_response_without_read_or_retry(wire):
    wire.reply.status=429
    with pytest.raises(market.MarketDataError):fetch()
    assert len(wire.calls)==1 and wire.reply.bounds==[] and wire.reply.closed


def test_market_close_boundary_during_composition_blocks_before_signal_generation(wire,monkeypatch):
    ticks=iter((datetime(2026,1,8,20,59,59,tzinfo=timezone.utc),
                datetime(2026,1,8,20,59,59,tzinfo=timezone.utc),
                datetime(2026,1,8,21,tzinfo=timezone.utc)))
    def forbidden(*a,**k):raise AssertionError('no stale signal generation')
    monkeypatch.setattr(market,'preview_independent_sma',forbidden)
    with pytest.raises(market.MarketDataError):market.collect_sma_preview(config(),api_key=SECRET,
        verified_us_symbols=['IBM'],clock=lambda:next(ticks))
    assert len(wire.calls)==1
