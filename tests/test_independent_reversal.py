"""New daily hypothesis: synthetic scenarios, not original strategy performance."""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, Context, localcontext
import hashlib
import json
from pathlib import Path

import pytest

from astral_stock_agent.independent_paper import preview_independent_paper
from astral_stock_agent.independent_reversal import preview_independent_reversal
from astral_stock_agent.research_data import ResearchDataError, read_daily_csv
from astral_stock_agent.research_reversal import REVERSAL_RULE, reversal_close_decisions
from astral_stock_agent.research_sessions import UTC_SESSION_DATE, NY_SESSION_DATE


DAYS = ('2026-01-02', '2026-01-05', '2026-01-06', '2026-01-07', '2026-01-08',
        '2026-01-09', '2026-01-12', '2026-01-13', '2026-01-14')
NOW = datetime(2026, 1, 15, tzinfo=timezone.utc)


def inputs(closes=(10, 12, 11, 13, 8, 8, 9, 10, 10), *, opens=None, extremes=None,
           days=DAYS, label='T00:00:00Z'):
    opens = opens or closes
    rows = []
    for i, (day, close, op) in enumerate(zip(days, closes, opens)):
        high, low = (extremes or {}).get(i, (max(close, op) + 1, min(close, op) - 1))
        rows.append(f'{day}{label},{op},{high},{low},{close},100\n'.encode())
    prices = b'timestamp,open,high,low,close,volume\n' + b''.join(rows)
    spec = dict(schema='independent_daily_reversal_v1', symbol='SPY',
        price_sha256=hashlib.sha256(prices).hexdigest(), price_count=len(rows),
        first_day=days[0], last_day=days[len(rows)-1], label_convention=UTC_SESSION_DATE,
        initial_cash='100', commission_bps='0', slippage_bps='0',
        rule=dict(id=REVERSAL_RULE, ema_period=2, rsi_period=2, atr_period=2,
                  atr_mean_period=2, units='1', trade_from=DAYS[0],
                  entry_rsi='60', exit_rsi='90', stop_loss_fraction='0.2',
                  take_profit_fraction='0.2', max_holding_sessions=10))
    return spec, prices


def preview(spec=None, prices=None, **kwargs):
    s, p = inputs()
    return preview_independent_reversal(s if spec is None else spec,
        p if prices is None else prices, verified_us_symbols=['SPY'], now=NOW, **kwargs)


def decisions(spec=None, prices=None):
    s, p = inputs() if spec is None else (spec, prices)
    h = read_daily_csv(p, symbol='SPY', expected_sha256=s['price_sha256'],
        expected_count=s['price_count'], start=s['first_day']+'T00:00:00Z',
        end=s['last_day']+'T23:59:59Z')
    return reversal_close_decisions(h, s['rule'], slippage_bps=s['slippage_bps'],
        first_day=h.bars[0].timestamp.date(), last_day=h.bars[-1].timestamp.date(),
        now=NOW, label_convention=s['label_convention'])


def test_signal_to_next_open_fill_and_independent_accounting():
    manifest, r, body = preview()
    assert [Decimal(x['units_change']) for x in manifest['close_signals']] == [0,0,0,0,1,0,0,-1,0]
    assert r['cumulative_simulated_fills'] == 2
    assert Decimal(r['realized_pnl']) == Decimal(r['cumulative_pnl']) == 2
    assert Decimal(r['units']) == 0
    assert decisions().reasons[7] == 'close_profit_exit'
    assert not r['source_equivalence_verified'] and not r['intrabar_protection_verified']
    assert not r['deployment_approved'] and not r['signal_causality_verified']
    generic, _ = preview_independent_paper(manifest, inputs()[1], verified_us_symbols=['SPY'], now=NOW)
    assert r['cumulative_pnl'] == generic['cumulative_pnl']
    assert '장중 보호 주문 없음' in body
    assert r['report_sha256'] == hashlib.sha256(body.encode()).hexdigest()


def test_entry_bar_is_not_used_to_fill_its_own_signal_and_final_exit_is_pending():
    s, p = inputs(closes=(10,12,11,13,8))
    _, r, _ = preview(s, p)
    assert r['cumulative_simulated_fills'] == 0 and Decimal(r['pending_close_change']) == 1
    s, p = inputs(closes=(10,12,11,13,8,8,9,10))
    _, r, _ = preview(s, p)
    assert r['cumulative_simulated_fills'] == 1 and Decimal(r['pending_close_change']) == -1


def test_gap_stop_exits_at_next_open_and_can_exceed_threshold():
    s, p = inputs(opens=(10,12,11,13,8,20,9,10,10))
    d = decisions(s, p)
    _, r, _ = preview(s, p)
    assert d.reasons[5] == 'close_loss_exit'
    assert Decimal(r['realized_pnl']) == -11  # actual modeled buy20/sell9, not fabricated16


def test_intrabar_touch_of_both_levels_is_not_an_oco_fill():
    s, p = inputs(extremes={5: (30, 1)})
    d = decisions(s, p)
    assert d.reasons[5] == 'hold' and d.changes[5] == 0
    assert preview(s, p)[1]['cumulative_simulated_fills'] == 2


def test_time_limit_counts_fill_day_and_never_pyramids():
    s, p = inputs(); s['rule']['max_holding_sessions'] = 1
    d = decisions(s, p)
    assert d.reasons[5] == 'holding_limit_exit'
    assert d.changes[4:7] == (1, -1, 0)
    assert d.reasons[6] == 'exit_fill_cooldown'
    held = Decimal(0)
    for change in d.changes:
        held += change
        assert held in (0, 1)


def test_rsi_exit_and_loss_exit_priority():
    s, p = inputs(); s['rule'].update(exit_rsi='61', take_profit_fraction='0.9')
    assert decisions(s, p).reasons[7] == 'rsi_exit'
    s, p = inputs(opens=(10,12,11,13,8,20,9,10,10))
    s['rule']['max_holding_sessions'] = 1
    assert decisions(s, p).reasons[5] == 'close_loss_exit'


def test_observed_buy_slippage_changes_close_trigger_level():
    s, p = inputs(closes=(10,12,11,13,8,8,9,9.6,10))
    assert decisions(s, p).reasons[7] == 'close_profit_exit'
    s['slippage_bps'] = '5'
    assert decisions(s, p).reasons[7] == 'hold'


def test_future_changes_and_extensions_preserve_prefix_and_rule_version():
    original = preview()[0]
    s, p = inputs(closes=(10,12,11,13,8,8,9,10,5))
    changed = preview(s, p)[0]
    assert original['close_signals'][:-1] == changed['close_signals'][:-1]
    assert original['version'] == changed['version']
    s, p = inputs(opens=(10,12,11,13,8,20,9,10,10))
    assert preview(s, p)[0]['close_signals'][:5] == original['close_signals'][:5]
    for n in (5, 6, 7, 8):
        s, p = inputs(closes=(10,12,11,13,8,8,9,10,10)[:n])
        m = preview(s, p)[0]
        assert m['close_signals'] == original['close_signals'][:n]
        assert m['version'] == original['version']


def test_costs_applied_without_mutating_inputs_or_choosing_parameters():
    s, p = inputs(); s.update(commission_bps='5', slippage_bps='5')
    before = deepcopy(s)
    _, r, _ = preview(s, p)
    assert Decimal(r['cumulative_pnl']) < 2 and Decimal(r['cumulative_commission']) > 0
    assert before == s
    s['initial_cash'] = '1'
    with pytest.raises(ResearchDataError, match='insufficient'):
        preview(s, p)


@pytest.mark.parametrize('field,value', [('ema_period',3), ('rsi_period',3), ('atr_period',3),
    ('atr_mean_period',3), ('units','2'), ('trade_from','2026-01-07'), ('entry_rsi','50'),
    ('exit_rsi','80'), ('stop_loss_fraction','0.3'), ('take_profit_fraction','0.3'),
    ('max_holding_sessions',3)])
def test_each_explicit_rule_parameter_binds_version(field, value):
    original = preview()[0]['version']
    s, p = inputs(); s['rule'][field] = value
    assert preview(s, p)[0]['version'] != original


def test_slippage_binds_signal_version_but_cash_and_commission_do_not():
    original = preview()[0]['version']
    s, p = inputs(); s['slippage_bps'] = '5'
    assert preview(s, p)[0]['version'] != original
    s, p = inputs(); s.update(initial_cash='200', commission_bps='5')
    assert preview(s, p)[0]['version'] == original


@pytest.mark.parametrize('field,value', [('ema_period',True), ('rsi_period',1),
    ('atr_period',253), ('atr_mean_period','2'), ('units',0), ('units',1.0),
    ('entry_rsi','NaN'), ('entry_rsi',-1), ('entry_rsi','90'), ('exit_rsi',101),
    ('stop_loss_fraction',0), ('stop_loss_fraction',1), ('take_profit_fraction',-1),
    ('max_holding_sessions',True), ('max_holding_sessions',0), ('max_holding_sessions',253),
    ('trade_from','2026-01-15'), ('id','original_astral_strategy')])
def test_invalid_or_implicit_rule_inputs_rejected(field, value):
    s, p = inputs(); s['rule'][field] = value
    with pytest.raises(ResearchDataError):
        preview(s, p)


@pytest.mark.parametrize('case', ['extra', 'rule_extra', 'missing', 'manual_signals',
                                 'scope', 'hash', 'schema', 'missing_day', 'future'])
def test_scope_integrity_and_no_source_execution(case):
    s, p = inputs()
    if case == 'extra': s['source_code'] = 'DO NOT EXECUTE'
    if case == 'rule_extra': s['rule']['accumulate'] = True
    if case == 'missing': s.pop('commission_bps')
    if case == 'manual_signals': s['close_signals'] = []
    if case == 'scope': s['symbol'] = 'BTC'
    if case == 'hash': s['price_sha256'] = 'bad'
    if case == 'schema': s['schema'] = 'independent_sma_candidate_v1'
    if case == 'missing_day':
        s, p = inputs(closes=(10,12,11,8,8,9,10,10), days=DAYS[:3]+DAYS[4:])
    if case == 'future': s['last_day'] = '2026-01-16'
    with pytest.raises(ResearchDataError):
        preview(s, p)


def test_start_boundary_and_pending_only_bootstrap_are_explicit():
    s, p = inputs(); s['rule']['trade_from'] = DAYS[-1]
    with pytest.raises(ResearchDataError):
        preview(s, p)
    _, r, _ = preview(s, p, allow_pending_only=True)
    assert r['cumulative_simulated_fills'] == 0 and Decimal(r['units']) == 0
    s['rule']['trade_from'] = DAYS[4]
    assert preview(s, p)[0]['close_signals'][:4] == preview()[0]['close_signals'][:4]


def test_label_and_hostile_decimal_context_do_not_change_calculation():
    s, p = inputs(label='T00:00:00-05:00'); s['label_convention'] = NY_SESSION_DATE
    expected = preview(s, p)
    hostile = Context(prec=2, Emin=-1, Emax=1)
    for trap in hostile.traps: hostile.traps[trap] = True
    with localcontext(hostile):
        assert preview(s, p) == expected


def test_no_network_storage_delivery_or_activation(tmp_path, monkeypatch):
    import socket
    from pathlib import Path
    from astral_stock_agent.delivery import Outbox, Telegram
    def forbidden(*a, **kw): raise AssertionError('no authority')
    monkeypatch.setattr(socket, 'socket', forbidden)
    monkeypatch.setattr(Path, 'open', forbidden)
    monkeypatch.setattr(Outbox, 'enqueue', forbidden)
    monkeypatch.setattr(Telegram, 'send', forbidden)
    preview()
    assert list(tmp_path.iterdir()) == []


def test_two_session_ledger_transition_and_restart_keep_exact_report(tmp_path, monkeypatch):
    from astral_stock_agent.independent_ledger import IndependentPaperLedger, IndependentLedgerError
    monkeypatch.delenv('GITHUB_ACTIONS', raising=False)
    directory = tmp_path / 'paper'
    directory.mkdir(mode=0o700)
    def session(length, now):
        s, p = inputs(closes=(10,12,11,13,8,8)[:length])
        s['rule']['trade_from'] = DAYS[4]
        return preview_independent_reversal(s, p, verified_us_symbols=['SPY'],
            now=now, allow_pending_only=True)
    first_now = datetime(2026, 1, 9, 1, tzinfo=timezone.utc)
    next_now = datetime(2026, 1, 10, tzinfo=timezone.utc)
    _, first, body = session(5, first_now)
    ledger = IndependentPaperLedger(directory)
    assert first['cumulative_simulated_fills'] == 0 and Decimal(first['units']) == 0
    assert Decimal(first['pending_close_change']) == 1
    assert ledger.append(first, body, now=first_now, bootstrap=True)['created']
    _, second, body2 = session(6, next_now)
    assert second['daily_simulated_fills'] == 1 and Decimal(second['units']) == 1
    assert Decimal(second['daily_units_change']) == Decimal(first['pending_close_change'])
    assert ledger.append(second, body2, now=next_now)['created']
    saved = {p.name: p.read_bytes() for p in directory.iterdir()}
    restarted = IndependentPaperLedger(directory)
    assert not restarted.append(second, body2, now=next_now)['created']
    assert restarted.report_for_latest_session(now=next_now)[1] == body2
    assert {p.name: p.read_bytes() for p in directory.iterdir()} == saved
    bad = dict(second, version='changed-candidate')
    with pytest.raises(IndependentLedgerError):
        restarted.append(bad, body2, now=next_now)
    assert {p.name: p.read_bytes() for p in directory.iterdir()} == saved


@pytest.mark.parametrize('name,value', [('initial_cash',0), ('commission_bps',-1),
                                      ('slippage_bps',10000), ('slippage_bps',True)])
def test_invalid_costs_and_cash_are_rejected(name, value):
    s, p = inputs(); s[name] = value
    with pytest.raises(ResearchDataError):
        preview(s, p)


def test_invalid_pending_policy_and_insufficient_indicator_warmup():
    with pytest.raises(ResearchDataError):
        preview(allow_pending_only='true')
    s, p = inputs(); s['rule']['atr_mean_period'] = 20
    with pytest.raises(ResearchDataError):
        preview(s, p, allow_pending_only=True)


def test_cli_reuses_private_bounded_input_and_rejects_duplicate_rule_fields(tmp_path, monkeypatch, capsys):
    from astral_stock_agent.cli import main
    monkeypatch.delenv('GITHUB_ACTIONS', raising=False)
    s, p = inputs()
    sf, pf = tmp_path / 'candidate.json', tmp_path / 'prices.csv'
    sf.write_text(json.dumps(s)); pf.write_bytes(p)
    args = ['preview-daily-reversal', str(sf), str(pf), '--us-symbol', 'SPY']
    assert main(args) == 0
    assert '별도 일봉 반전 연구 후보' in capsys.readouterr().out
    raw = sf.read_text().replace('"entry_rsi": "60"', '"entry_rsi": "60", "entry_rsi": "50"')
    sf.write_text(raw)
    assert main(args) == 2
    assert 'blocked' in capsys.readouterr().out
    assert sorted(p.name for p in tmp_path.iterdir()) == ['candidate.json', 'prices.csv']


def test_public_ci_rejects_cli_before_file_clock_or_candidate_access(monkeypatch, capsys):
    from astral_stock_agent.cli import main
    monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    def forbidden(*a, **kw): raise AssertionError('private access')
    monkeypatch.setattr(Path, 'open', forbidden)
    assert main(['preview-daily-reversal', 'private.json', 'private.csv', '--us-symbol', 'SPY']) == 2
    assert 'blocked' in capsys.readouterr().out
