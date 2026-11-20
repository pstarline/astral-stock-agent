"""Synthetic compact → archive → reversal → ledger → Outbox, no live APIs."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import datetime, date, timedelta
import io
import json
import sqlite3
from threading import Barrier

import pytest

from astral_stock_agent.delivery import Outbox
from astral_stock_agent.independent_ledger import IndependentPaperLedger
from astral_stock_agent.market_data import normalize_alpha_daily, ROW_FIELDS
from astral_stock_agent.paper_pipeline import PaperPipelineError
from astral_stock_agent.paper_runtime import run_market_paper, PaperRuntimeError
from astral_stock_agent.sessions import session_on
from test_independent_reversal import inputs, DAYS


def normalize(rows, symbol='SPY'):
    series = {row['timestamp'][:10]: dict(zip(ROW_FIELDS,
        [row[k] for k in ('open','high','low','close','volume')])) for row in rows}
    last = max(series)
    now = datetime.fromisoformat(last+'T23:00:00+00:00')
    raw = json.dumps({'Meta Data': {
        '1. Information': 'Daily Prices (open, high, low, close) and Volumes',
        '2. Symbol': symbol, '3. Last Refreshed': last, '4. Output Size': 'Compact',
        '5. Time Zone': 'US/Eastern'}, 'Time Series (Daily)': series}).encode()
    return normalize_alpha_daily(raw, symbol=symbol, verified_us_symbols=[symbol], now=now), now


def window(start=0, end=5):
    _, raw = inputs()
    rows = list(csv.DictReader(io.StringIO(raw.decode())))
    return normalize(rows[start:end])


@pytest.fixture
def env(tmp_path, monkeypatch):
    import astral_stock_agent.paper_runtime as runtime
    monkeypatch.delenv('GITHUB_ACTIONS', raising=False)
    for name in ('prices', 'paper', 'outbox', 'control'):
        (tmp_path/name).mkdir(mode=0o700)
    config = inputs()[0]
    config = {k: config[k] for k in ('symbol','initial_cash','commission_bps','slippage_bps','rule')}
    config.update(schema='archived_reversal_record_v1', commission_bps='5', slippage_bps='5')
    config['rule']['trade_from'] = DAYS[4]
    database = tmp_path/'outbox'/'reports.sqlite'
    Outbox(database, create=True)
    data, now = window()
    state = dict(data=data, now=now, calls=0)
    def fetch(**kwargs):
        state['calls'] += 1
        return state['data']
    monkeypatch.setattr(runtime, 'fetch_alpha_daily', fetch)
    return config, tmp_path, database, state


def run(env, bootstrap=False, **kwargs):
    config, root, db, state = env
    return run_market_paper(config, price_directory=root/'prices', paper_directory=root/'paper',
        database=db, verified_us_symbols=['SPY'], api_key='SYNTHETIC_PRIVATE_KEY',
        bootstrap=bootstrap, clock=lambda: state['now'], **kwargs)


def snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}


def test_flat_bootstrap_rollover_fill_queue_exact_body_and_restart(env):
    first = run(env, True)
    config, root, db, state = env
    paper = IndependentPaperLedger(root/'paper')
    base, body, _ = paper._load_checkpoint(DAYS[4])
    assert base['units'] == '0' and base['cumulative_simulated_fills'] == 0
    assert base['pending_close_change'] == '1' and first['state'] == 'pending'
    assert not base['deployment_approved'] and not base['intrabar_protection_verified']
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT body FROM reports').fetchone() == (body,)
    before = snapshot(root)
    assert not run(env)['collection_performed']
    assert state['calls'] == 1 and snapshot(root) == before
    state['data'], state['now'] = window(1, 6)
    assert run(env)['collection_performed']
    current, body2, _ = paper._load_checkpoint(DAYS[5])
    assert current['units'] == '1' and current['daily_simulated_fills'] == 1
    assert current['daily_units_change'] == base['pending_close_change']
    assert not run(env)['collection_performed'] and state['calls'] == 2
    assert Outbox(db).status_counts()['pending'] == 2
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT body FROM reports ORDER BY rowid DESC LIMIT 1').fetchone() == (body2,)


@pytest.mark.parametrize('state_name', ['pending', 'sending', 'unknown', 'sent'])
def test_existing_queue_state_never_resends_or_recollects(env, state_name):
    run(env, True)
    _, root, db, state = env
    with sqlite3.connect(db) as conn:
        conn.execute('UPDATE reports SET state=?', (state_name,))
    class Sender:
        def send(self, body): raise AssertionError('no retry authority')
    before = snapshot(root)
    result = run(env, sender=Sender(), delivery_authorized=True)
    assert result['state'] == state_name and not result['delivery_attempted']
    assert not result['collection_performed'] and state['calls'] == 1
    assert snapshot(root) == before


@pytest.mark.parametrize('change', ['period','threshold','units','stop','holding','cash','fee','slip','schema'])
def test_changed_candidate_rejected_before_fetch_or_writes_even_on_bootstrap(env, change):
    run(env, True)
    config, root, _, state = env
    if change == 'period': config['rule']['ema_period'] = 3
    if change == 'threshold': config['rule']['entry_rsi'] = '55'
    if change == 'units': config['rule']['units'] = '2'
    if change == 'stop': config['rule']['stop_loss_fraction'] = '0.3'
    if change == 'holding': config['rule']['max_holding_sessions'] = 3
    if change == 'cash': config['initial_cash'] = '200'
    if change == 'fee': config['commission_bps'] = '0'
    if change == 'slip': config['slippage_bps'] = '0'
    if change == 'schema':
        config.update(schema='archived_sma_record_v1', rule=dict(
            id='close_gt_trailing_sma_fixed_units_v1', window=2, units='1', trade_from=DAYS[4]))
    before = snapshot(root)
    for bootstrap in (False, True):
        with pytest.raises(PaperRuntimeError): run(env, bootstrap)
        assert state['calls'] == 1 and snapshot(root) == before


@pytest.mark.parametrize('field,value', [('ema_period',101), ('rsi_period',100), ('atr_mean_period',99)])
def test_impossible_compact_bootstrap_fails_without_api_consumption(env, field, value):
    env[0]['rule'][field] = value
    with pytest.raises(PaperRuntimeError): run(env, True)
    assert env[-1]['calls'] == 0
    assert list((env[1]/'prices').iterdir()) == list((env[1]/'paper').iterdir()) == []


def test_price_overlap_revision_and_skipped_session_never_change_existing_state(env):
    run(env, True)
    _, root, _, state = env
    _, raw = inputs()
    rows = list(csv.DictReader(io.StringIO(raw.decode())))
    rows[4]['volume'] = '101'
    state['data'], state['now'] = normalize(rows[1:6])
    before = snapshot(root)
    with pytest.raises(PaperRuntimeError): run(env)
    assert snapshot(root) == before
    state['data'], state['now'] = window(2, 7)
    calls = state['calls']
    with pytest.raises(PaperRuntimeError): run(env)
    assert state['calls'] == calls and snapshot(root) == before


def test_accounting_failure_retains_pinned_prices_and_identical_resume_only(env, monkeypatch):
    import astral_stock_agent.paper_pipeline as pipeline
    original = pipeline.preview_independent_reversal
    def fail(*a, **kw): raise RuntimeError('SYNTHETIC_PRIVATE_ERROR')
    monkeypatch.setattr(pipeline, 'preview_independent_reversal', fail)
    config, root, _, state = env
    with pytest.raises(PaperRuntimeError) as error: run(env, True)
    assert 'SYNTHETIC' not in str(error.value)
    before = snapshot(root)
    assert list((root/'prices').iterdir()) and list((root/'paper').iterdir())
    config['rule']['entry_rsi'] = '55'
    calls = state['calls']
    with pytest.raises(PaperRuntimeError): run(env, True)
    assert state['calls'] == calls and snapshot(root) == before
    config['rule']['entry_rsi'] = '60'
    monkeypatch.setattr(pipeline, 'preview_independent_reversal', original)
    assert run(env, True)['state'] == 'pending'
    assert not run(env)['collection_performed']


def test_queue_failure_keeps_checkpoint_then_skips_recollection(env, monkeypatch):
    import astral_stock_agent.paper_runtime as runtime
    original = runtime.enqueue_latest_paper_report
    def fail(**kwargs): raise RuntimeError('SYNTHETIC_PRIVATE_ERROR')
    monkeypatch.setattr(runtime, 'enqueue_latest_paper_report', fail)
    with pytest.raises(PaperRuntimeError): run(env, True)
    assert env[-1]['calls'] == 1
    monkeypatch.setattr(runtime, 'enqueue_latest_paper_report', original)
    assert not run(env)['collection_performed'] and env[-1]['calls'] == 1


def test_public_ci_and_orphan_state_are_blocked_before_network(env, monkeypatch):
    monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    with pytest.raises(PaperRuntimeError): run(env, True)
    assert env[-1]['calls'] == 0
    monkeypatch.delenv('GITHUB_ACTIONS')
    (env[1]/'paper'/'unreconciled').write_text('synthetic')
    with pytest.raises(PaperRuntimeError): run(env, True)
    assert env[-1]['calls'] == 0


def test_short_warmup_and_wrong_bootstrap_date_are_blocked_before_recording(env):
    config, root, _, state = env
    config['rule']['ema_period'] = 6
    with pytest.raises(PaperRuntimeError): run(env, True)
    assert not list((root/'prices').iterdir()) and not list((root/'paper').iterdir())
    config['rule']['trade_from'] = DAYS[3]
    calls = state['calls']
    with pytest.raises(PaperRuntimeError): run(env, True)
    assert state['calls'] == calls


def test_authorized_stub_delivery_once_and_timeout_preserves_unknown(env):
    class Sender:
        def __init__(self): self.bodies = []
        def send(self, body):
            self.bodies.append(body)
            if len(self.bodies) == 2: raise TimeoutError('SYNTHETIC_PROVIDER_ERROR')
            return 'synthetic-message'
    sender = Sender()
    assert run(env, True, sender=sender, delivery_authorized=True)['state'] == 'sent'
    assert not run(env, sender=sender, delivery_authorized=True)['delivery_attempted']
    env[-1]['data'], env[-1]['now'] = window(1, 6)
    with pytest.raises(PaperRuntimeError): run(env, sender=sender, delivery_authorized=True)
    assert Outbox(env[2]).status_counts()['unknown'] == 1
    again = run(env, sender=sender, delivery_authorized=True)
    assert again['state'] == 'unknown' and not again['delivery_attempted']
    assert len(sender.bodies) == env[-1]['calls'] == 2


def test_cli_handles_new_schema_without_reflecting_configuration(env, tmp_path, monkeypatch, capsys):
    from astral_stock_agent.cli import main
    import astral_stock_agent.paper_runtime as runtime
    class Clock:
        @classmethod
        def now(cls, zone): return env[-1]['now']
    monkeypatch.setattr(runtime, 'datetime', Clock)
    monkeypatch.setenv('ALPHAVANTAGE_API_KEY', 'SYNTHETIC_PRIVATE_KEY')
    config, root, db, state = env
    path = tmp_path/'candidate.json'; path.write_text(json.dumps(config))
    args = ['run-paper', str(path), '--price-directory', str(root/'prices'),
            '--paper-directory', str(root/'paper'), '--database', str(db), '--us-symbol','SPY','--bootstrap']
    assert main(args) == 0
    output = capsys.readouterr().out
    assert json.loads(output)['state'] == 'pending'
    assert 'entry_rsi' not in output and 'SYNTHETIC' not in output
    assert main(args) == 0 and state['calls'] == 1


def test_worker_with_explicit_stub_permissions_preserves_attempt_and_no_retry(env):
    from astral_stock_agent.paper_worker import tick, WorkerError
    config, root, db, state = env
    worker = dict(PAPER_STORAGE_ROOT=str(root), PAPER_WORKER_ENABLED='true',
        PAPER_CANDIDATE_APPROVED='true', PAPER_REPORTING_ENABLED='true',
        PAPER_CONFIG_JSON=json.dumps(config), PAPER_BOOTSTRAP_SESSION=DAYS[4],
        PAPER_US_SYMBOL='SPY', ALPHAVANTAGE_API_KEY='synthetic',
        TELEGRAM_BOT_TOKEN='synthetic', TELEGRAM_CHAT_ID='synthetic')
    bodies = []
    class Sender:
        def __init__(self, *args): pass
        def send(self, body):
            bodies.append(body)
            if len(bodies) == 2: raise TimeoutError('synthetic')
            return 'synthetic-message'
    def call(): return tick(worker, clock=lambda: state['now'], sender_factory=Sender)
    assert call()['status'] == 'completed'
    assert call()['status'] == 'already_completed'
    state['data'], state['now'] = window(1, 6)
    with pytest.raises(WorkerError): call()
    with pytest.raises(WorkerError): call()
    state['data'], state['now'] = window(2, 7)
    with pytest.raises(WorkerError): call()
    assert state['calls'] == len(bodies) == 2


def test_fixed_start_archive_grows_beyond_compact_and_keeps_recursive_seed(env, monkeypatch):
    import astral_stock_agent.paper_pipeline as pipeline
    config, root, db, state = env
    days, day = [], date(2026, 1, 2)
    while len(days) < 101:
        if session_on(day): days.append(day.isoformat())
        day += timedelta(days=1)
    rows = [dict(timestamp=d+'T00:00:00Z', open=str(10+i%7), high=str(11+i%7),
                 low=str(9+i%7), close=str(10+i%7), volume='100') for i, d in enumerate(days)]
    config['rule'].update(ema_period=40, trade_from=days[99])
    original = pipeline.preview_independent_reversal
    seen = []
    def capture(spec, prices, **kwargs):
        seen.append((spec['first_day'], spec['price_count']))
        return original(spec, prices, **kwargs)
    monkeypatch.setattr(pipeline, 'preview_independent_reversal', capture)
    state['data'], state['now'] = normalize(rows[:100])
    run(env, True)
    state['data'], state['now'] = normalize(rows[1:])
    run(env)
    assert seen == [(days[0], 100), (days[0], 101)]
    assert IndependentPaperLedger(root/'paper').report_for_latest_session(now=state['now'])


def test_existing_sma_state_cannot_be_adopted_by_reversal(env):
    original = deepcopy(env[0])
    env[0].update(schema='archived_sma_record_v1', rule=dict(
        id='close_gt_trailing_sma_fixed_units_v1', window=2, units='1', trade_from=DAYS[4]))
    run(env, True)
    env[0].clear(); env[0].update(original)
    before = snapshot(env[1])
    with pytest.raises(PaperRuntimeError): run(env, True)
    assert env[-1]['calls'] == 1 and snapshot(env[1]) == before


@pytest.mark.parametrize('other_family', [False, True])
def test_two_different_bootstraps_freeze_only_one_candidate_before_price_writes(env, monkeypatch, other_family):
    import astral_stock_agent.paper_pipeline as pipeline
    config, root, _, state = env
    other = deepcopy(config); other['rule']['units'] = '2'
    if other_family:
        other.update(schema='archived_sma_record_v1', rule=dict(
            id='close_gt_trailing_sma_fixed_units_v1', window=2, units='2', trade_from=DAYS[4]))
    barrier, original = Barrier(2), pipeline.check_candidate_pin
    def synchronize(*args, **kwargs):
        path = original(*args, **kwargs)
        barrier.wait(timeout=10)
        return path
    monkeypatch.setattr(pipeline, 'check_candidate_pin', synchronize)
    def record(cfg):
        try:
            return pipeline.record_archived_candidate(cfg, state['data'], price_directory=root/'prices',
                paper_directory=root/'paper', verified_us_symbols=['SPY'], now=state['now'],
                bootstrap=True)
        except PaperPipelineError:
            return None
    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(record, (config, other)))
    assert sum(x is not None and x['created'] for x in outcomes) == 1
    paper = IndependentPaperLedger(root/'paper')
    pinned = json.loads(paper._read(paper._path('archived-candidate-config-v1')))
    accounting, _, _ = paper._load_checkpoint(DAYS[4])
    assert pinned in (config, other)
    if pinned['schema'] == 'archived_reversal_record_v1':
        assert accounting['pending_close_change'] == pinned['rule']['units']
    assert not list((root/'outbox').glob('*-journal'))
    assert state['calls'] == 0


def test_missing_pin_cannot_be_recreated_over_existing_checkpoint(env):
    run(env, True)
    paper = IndependentPaperLedger(env[1]/'paper')
    paper._path('archived-reversal-config-v1').unlink()
    before = snapshot(env[1])
    with pytest.raises(PaperRuntimeError): run(env, True)
    assert env[-1]['calls'] == 1 and snapshot(env[1]) == before


def test_legacy_sma_without_common_claim_restarts_then_migrates_on_next_record(env):
    env[0].update(schema='archived_sma_record_v1', rule=dict(
        id='close_gt_trailing_sma_fixed_units_v1', window=2, units='1', trade_from=DAYS[4]))
    run(env, True)
    paper = IndependentPaperLedger(env[1]/'paper')
    common = paper._path('archived-candidate-config-v1')
    common.unlink()  # emulate a pre-change SMA directory, preserving its family pin/scope
    before = snapshot(env[1])
    assert not run(env)['collection_performed'] and snapshot(env[1]) == before
    env[-1]['data'], env[-1]['now'] = window(1, 6)
    assert run(env)['collection_performed']
    assert json.loads(paper._read(common)) == env[0]


def test_interruption_between_common_claim_and_family_pin_requires_same_manual_bootstrap(env, monkeypatch):
    original = IndependentPaperLedger._write_once
    config, root, _, state = env
    paper = IndependentPaperLedger(root/'paper')
    family = paper._path('archived-reversal-config-v1')
    def fail_family(self, path, data, conflict):
        if path == family: raise RuntimeError('SYNTHETIC_PRIVATE_ERROR')
        return original(self, path, data, conflict)
    monkeypatch.setattr(IndependentPaperLedger, '_write_once', fail_family)
    with pytest.raises(PaperRuntimeError): run(env, True)
    common = paper._path('archived-candidate-config-v1')
    assert common.exists() and not family.exists() and not list((root/'prices').iterdir())
    before = snapshot(root)
    config['rule']['units'] = '2'
    with pytest.raises(PaperRuntimeError): run(env, True)
    assert state['calls'] == 1 and snapshot(root) == before
    config['rule']['units'] = '1'
    monkeypatch.setattr(IndependentPaperLedger, '_write_once', original)
    assert run(env, True)['state'] == 'pending'
