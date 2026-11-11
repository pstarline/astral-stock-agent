"""Independent hand-calculated accounting and private CLI regression scenarios."""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, localcontext
import hashlib
import json
from pathlib import Path

import pytest

from astral_stock_agent.cli import main
from astral_stock_agent.independent_paper import preview_independent_paper
from astral_stock_agent.research_data import ResearchDataError
from astral_stock_agent.research_sessions import UTC_SESSION_DATE


NOW = datetime(2026, 1, 10, tzinfo=timezone.utc)
DAYS = ('2026-01-02', '2026-01-05', '2026-01-06', '2026-01-07')


def inputs(changes=('2', '0', '-1', '3')):
    # Buy2 next open11, partial sell1 next open15, terminal held1 at16.
    prices = b'timestamp,open,high,low,close,volume\n' + b''.join(
        f'{d}T00:00:00Z,{op},20,1,{cl},100\n'.encode()
        for d, op, cl in zip(DAYS, (10, 11, 12, 15), (10, 12, 13, 16)))
    manifest = dict(schema='independent_price_simulation_v1', strategy='synthetic rule',
                    version='v1', symbol='SPY', price_sha256=hashlib.sha256(prices).hexdigest(),
                    price_count=4, first_day=DAYS[0], last_day=DAYS[-1],
                    label_convention=UTC_SESSION_DATE, initial_cash='100', commission_bps='0',
                    slippage_bps='0', close_signals=[dict(session=d, units_change=c)
                                                   for d, c in zip(DAYS, changes)])
    return manifest, prices


def preview(m=None, p=None, **kwargs):
    default_m, default_p = inputs()
    return preview_independent_paper(m if m is not None else default_m,
        p if p is not None else default_p, verified_us_symbols=['SPY'], now=kwargs.get('now', NOW))


def test_partial_sale_and_daily_accounting_by_hand():
    r, body = preview()
    assert {k: Decimal(r[k]) for k in ('cash', 'units', 'equity', 'daily_pnl', 'cumulative_pnl',
                                     'realized_pnl', 'unrealized_pnl')} == dict(
        cash=93, units=1, equity=109, daily_pnl=5, cumulative_pnl=9, realized_pnl=4, unrealized_pnl=5)
    assert Decimal(r['daily_realized_pnl']) == 4
    assert Decimal(r['daily_unrealized_change']) == 1
    assert r['daily_simulated_fills'] == 1 and r['cumulative_simulated_fills'] == 2
    assert r['pending_close_change'] == '3'  # not a third fill
    assert '자체 모의매매' in body and '실제 주문/체결 아님' in body
    assert '당일 손익: 5.00 USD' in body
    assert '다음 시가 미확인' in body and '미검증' in body
    assert not any(r[k] for k in ('provider_authenticity_verified', 'corporate_actions_verified',
                                 'actual_execution_verified', 'deployment_approved'))


def test_explicit_costs_and_pnl_reconciliation():
    m, p = inputs(); m.update(commission_bps='5', slippage_bps='5')
    r, _ = preview(m, p)
    dec = lambda k: Decimal(r[k])
    # Buy 2 at11.0055, pay.0110055; sell1 at14.9925, pay.00749625.
    assert dec('cash') == Decimal('92.96299825')
    assert dec('realized_pnl') == Decimal('3.974001')
    assert dec('unrealized_pnl') == Decimal('4.98899725')
    assert dec('cumulative_pnl') == dec('realized_pnl') + dec('unrealized_pnl')
    assert dec('daily_pnl') == dec('daily_realized_pnl') + dec('daily_unrealized_change')
    assert dec('daily_commission') == Decimal('.00749625')


@pytest.mark.parametrize('changes', [('0','0','0','1'), ('1','0','0','-1'), ('1','-1','0','0')])
def test_no_trade_holding_and_flat_days(changes):
    m, p = inputs(changes)
    r, _ = preview(m, p)
    assert r['daily_simulated_fills'] == 0
    assert Decimal(r['daily_realized_pnl']) == 0
    if changes[0] == '0':
        assert Decimal(r['cumulative_pnl']) == 0 and r['cumulative_simulated_fills'] == 0
    elif changes[1] == '0':
        assert Decimal(r['daily_pnl']) == 3 and Decimal(r['unrealized_pnl']) == 5
    else:
        assert Decimal(r['daily_pnl']) == 0 and Decimal(r['units']) == 0


def test_sale_on_last_day_does_not_force_remaining_liquidation():
    r, _ = preview()
    assert Decimal(r['units']) == 1
    assert Decimal(r['unrealized_pnl']) == 5


@pytest.mark.parametrize('field,value', [('price_sha256','bad'), ('price_count',3),
    ('commission_bps',None), ('slippage_bps',-1), ('initial_cash','1'),
    ('label_convention','guess'), ('strategy','https://private.invalid/token'),
    ('version','bad\nlabel'), ('schema','astral_mcp'), ('symbol','BTC')])
def test_bad_inputs_fail_without_partial_report(field, value):
    m, p = inputs(); m[field] = value
    with pytest.raises((ResearchDataError, TypeError)):
        preview(m, p)


@pytest.mark.parametrize('case', ['missing', 'duplicate', 'order', 'nan', 'short', 'extra'])
def test_incomplete_or_ambiguous_signal_contract(case):
    m, p = inputs()
    if case == 'missing': m['close_signals'].pop()
    if case == 'duplicate': m['close_signals'][1]['session'] = DAYS[0]
    if case == 'order': m['close_signals'].reverse()
    if case == 'nan': m['close_signals'][0]['units_change'] = 'NaN'
    if case == 'short': m['close_signals'][0]['units_change'] = '-1'
    if case == 'extra': m['close_signals'][0]['unverified_field'] = True
    with pytest.raises(ResearchDataError): preview(m, p)


def test_missing_bar_and_unclosed_session_block_calculation():
    m, p = inputs(); p = b'\n'.join(p.split(b'\n')[:2] + p.split(b'\n')[3:])
    m['price_sha256'] = hashlib.sha256(p).hexdigest(); m['price_count'] = 3
    m['close_signals'].pop(1)
    with pytest.raises(ResearchDataError, match='scheduled sessions'): preview(m, p)
    with pytest.raises(ResearchDataError, match='unclosed'):
        preview(now=datetime(2026,1,7,19,tzinfo=timezone.utc))


def test_decimal_precision_and_inputs_unchanged():
    m,p=inputs(); original=deepcopy(m); expected=preview(m,p)
    with localcontext() as ctx:
        ctx.prec=3
        assert preview(m,p) == expected
    assert m == original


def test_cli_preview_has_no_astral_outbox_or_telegram_access(tmp_path,monkeypatch,capsys):
    monkeypatch.delenv('GITHUB_ACTIONS',raising=False)
    m,p=inputs(); mf=tmp_path/'manifest.json'; pf=tmp_path/'prices.csv'
    mf.write_text(json.dumps(m)); pf.write_bytes(p)
    import astral_stock_agent.cli as cli
    def forbidden(*a,**kw): raise AssertionError('no operational access')
    monkeypatch.setattr(cli,'Telegram',forbidden); monkeypatch.setattr(cli,'Outbox',forbidden)
    assert main(['preview-simulation',str(mf),str(pf),'--us-symbol','SPY']) == 0
    assert '당일 손익: 5.00 USD' in capsys.readouterr().out
    mf.write_text(mf.read_text().replace('"version": "v1"','"version": "v1", "version": "v2"'))
    assert main(['preview-simulation',str(mf),str(pf),'--us-symbol','SPY']) == 2
    assert 'blocked' in capsys.readouterr().out


def test_cli_public_ci_and_invalid_arguments_block_before_file_access(monkeypatch,capsys):
    def forbidden(*a,**kw): raise AssertionError('no input access')
    monkeypatch.setattr(Path,'open',forbidden)
    monkeypatch.setenv('GITHUB_ACTIONS','true')
    base=['preview-simulation','secret.json','secret.csv','--us-symbol','SPY']
    assert main(base) == 2
    assert main(base+['--send','SYNTHETIC_PRIVATE_VALUE']) == 2
    assert 'SYNTHETIC_PRIVATE_VALUE' not in str(capsys.readouterr())


def test_multiple_purchases_use_average_cost_for_partial_sale():
    m,p=inputs(('1','2','-2','0'))
    r,_=preview(m,p)
    assert Decimal(r['cash']) == 95 and Decimal(r['equity']) == 111
    assert Decimal(r['units']) == 1
    assert abs(Decimal(r['realized_pnl']) - Decimal(20)/3) < Decimal('1e-24')
    assert abs(Decimal(r['unrealized_pnl']) - Decimal(13)/3) < Decimal('1e-24')


def test_full_sale_leaves_no_unrealized_position():
    m,p=inputs(('1','2','-3','0'))
    r,_=preview(m,p)
    assert Decimal(r['units']) == 0
    assert Decimal(r['unrealized_pnl']) == 0
    assert Decimal(r['realized_pnl']) == Decimal(r['cumulative_pnl']) == 10


def test_new_york_midnight_labels_are_explicit_not_shifted():
    from astral_stock_agent.research_sessions import NY_SESSION_DATE
    m,p=inputs()
    p=p.replace(b'T00:00:00Z',b'T00:00:00-05:00')
    m.update(price_sha256=hashlib.sha256(p).hexdigest(),label_convention=NY_SESSION_DATE)
    assert preview(m,p)[0]['daily_pnl'] == '5'
    m['label_convention']=UTC_SESSION_DATE
    with pytest.raises(ResearchDataError,match='midnights'): preview(m,p)
