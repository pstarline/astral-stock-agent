"""Hand-calculated synthetic research, with no credentials or market rows."""
import copy
import hashlib
import json
from datetime import datetime, timezone

import pytest

from astral_stock_agent.cli import main
from astral_stock_agent.research_data import ResearchDataError
from astral_stock_agent.research_review import review_sma_evidence
from astral_stock_agent.research_sessions import UTC_SESSION_DATE


NOW = datetime(2026, 11, 6, tzinfo=timezone.utc)


def evidence():
    prices = (b'timestamp,open,high,low,close,volume\n'
              b'2026-11-02T00:00:00Z,10,15,1,10,100\n'
              b'2026-11-03T00:00:00Z,11,15,1,12,100\n'
              b'2026-11-04T00:00:00Z,10,15,1,8,100\n'
              b'2026-11-05T00:00:00Z,7,15,1,9,100\n')
    manifest = {'version': 1, 'research': {
        'symbol': 'SPY', 'start': '2026-11-02T00:00:00Z', 'end': '2026-11-05T23:59:59Z',
        'first_day': '2026-11-02', 'last_day': '2026-11-05', 'trade_from': '2026-11-02',
        'label_convention': UTC_SESSION_DATE, 'window': 2, 'units': '1',
        'initial_cash': '100', 'commission_bps': '100', 'slippage_bps': '100'},
        'expected_run': {'backtest_id': 'synthetic-private-run', 'strategy_id': 7, 'first_compared_index': 1},
        'price_response': {'status': 'ok', 'symbol': 'SPY', 'tf': '1d', 'delivery': 'download',
            'limit_capped': False, 'has_more': False, 'count': 4,
            'series_metadata': {'asset_type': 'stock', 'price_kind': 'ohlcv',
                                'synthetic_ohlc': False, 'volume_available': True},
            'artifact': {'format': 'csv', 'row_count': 4, 'size_bytes': len(prices),
                         'sha256': hashlib.sha256(prices).hexdigest()}},
        'backtest_response': {'backtest_id': 'synthetic-private-run', 'strategy_id': 7,
            'backtest_status': 'succeeded', 'health_state': 'ok', 'degraded': False,
            'results': {'health_state': 'ok', 'degraded': False, 'portfolio_value_downsampled': False,
                'portfolio_value': {'2026-11-03T00:00:00Z': '100',
                                    '2026-11-04T00:00:00Z': '97.799',
                                    '2026-11-05T00:00:00Z': '96.6597'},
                'backtest_period': {'bar_size': '1d', 'history_capped': False,
                    'raw_data_start': '2026-11-02T00:00:00Z', 'raw_data_end': '2026-11-05T00:00:00Z',
                    'first_eligible_trade_at': '2026-11-03T00:00:00Z'}}}}
    return manifest, prices


def review(manifest, prices):
    return review_sma_evidence(manifest, prices, verified_us_symbols=['SPY'], now=NOW)


def test_costs_and_final_unexecuted_decision_survive_the_composed_review():
    manifest, prices = evidence()
    original = copy.deepcopy(manifest)
    # Buy at 10.10 plus .101 fee, then sell at 6.93 less .0693 fee.
    # Final close triggers a buy but no following open exists: it stays pending.
    result = review(manifest, prices)
    assert result['curves_match'] and result['compared_points'] == 3
    assert result['mismatched_points'] == 0 and result['max_absolute_gap_usd'] == '0'
    assert result['hypothetical_fill_count'] == 2 and result['pending_final_decision']
    for key in ['external_rules_and_costs_verified', 'full_strategy_parity_verified',
                'actual_execution_verified', 'deployment_approved', 'new_orders_allowed']:
        assert result[key] is False
    assert 'synthetic-private-run' not in json.dumps(result)
    assert manifest == original


def test_intermediate_mismatch_is_returned_even_with_matching_final_equity():
    manifest, prices = evidence()
    manifest['backtest_response']['results']['portfolio_value']['2026-11-03T00:00:00Z'] = '99'
    result = review(manifest, prices)
    assert not result['curves_match'] and result['mismatched_points'] == 1
    assert result['max_absolute_gap_usd'] == '1'


@pytest.mark.parametrize('change', ['hash', 'downsampled', 'run', 'calendar_gap', 'boundary', 'missing_cost', 'version'])
def test_incomplete_or_different_scope_never_becomes_a_success(change):
    manifest, prices = evidence()
    if change == 'hash': prices += b'\n'
    elif change == 'downsampled': manifest['backtest_response']['results']['portfolio_value_downsampled'] = True
    elif change == 'run': manifest['expected_run']['backtest_id'] = 'wrong-private-run'
    elif change == 'calendar_gap': manifest['research']['last_day'] = '2026-11-06'
    elif change == 'boundary': manifest['expected_run']['first_compared_index'] = 2
    elif change == 'missing_cost': del manifest['research']['commission_bps']
    else: manifest['version'] = True
    with pytest.raises(ResearchDataError) as exc:
        review(manifest, prices)
    assert str(exc.value) == 'complete research evidence and explicit matching scope required'


def test_symbol_allowlist_is_explicit_and_not_inferred_from_manifest():
    manifest, prices = evidence()
    with pytest.raises(ResearchDataError):
        review_sma_evidence(manifest, prices, verified_us_symbols=[], now=NOW)


@pytest.fixture
def cli_files(tmp_path, monkeypatch):
    monkeypatch.delenv('GITHUB_ACTIONS', raising=False)
    from astral_stock_agent import cli
    def forbidden(*args, **kwargs):
        raise AssertionError('Research has no delivery or outbox authority')
    monkeypatch.setattr(cli, 'Telegram', forbidden)
    monkeypatch.setattr(cli, 'Outbox', forbidden)
    manifest, prices = evidence()
    path, csv_path = tmp_path / 'private-manifest.json', tmp_path / 'private-prices.csv'
    path.write_text(json.dumps(manifest))
    csv_path.write_bytes(prices)
    return path, csv_path


@pytest.mark.parametrize('mismatch', [False, True])
def test_cli_returns_nonzero_for_mismatch_and_never_sends(cli_files, capsys, mismatch):
    path, csv_path = cli_files
    if mismatch:
        manifest = json.loads(path.read_text())
        manifest['backtest_response']['results']['portfolio_value']['2026-11-03T00:00:00Z'] = '99'
        path.write_text(json.dumps(manifest))
    before = path.read_bytes(), csv_path.read_bytes()
    assert main(['audit-research', str(path), str(csv_path), '--us-symbol', 'SPY']) == (2 if mismatch else 0)
    result = json.loads(capsys.readouterr().out)
    assert result['curves_match'] is not mismatch
    assert 'synthetic-private-run' not in json.dumps(result)
    assert before == (path.read_bytes(), csv_path.read_bytes())
    assert len(list(path.parent.iterdir())) == 2


def test_public_ci_is_blocked_before_reading_any_private_file(monkeypatch, capsys):
    from pathlib import Path
    monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    def forbidden(*args, **kwargs):
        raise AssertionError('No private file access in public CI')
    monkeypatch.setattr(Path, 'open', forbidden)
    assert main(['audit-research', 'PRIVATE_FILE', 'PRIVATE_CSV', '--us-symbol', 'SPY']) == 2
    assert 'PRIVATE' not in capsys.readouterr().out


@pytest.mark.parametrize('extra', [['--send', 'PRIVATE_TOKEN'], ['--reconcile'], ['--database', 'PRIVATE_PATH']])
def test_cli_rejects_delivery_options_without_echoing_values(cli_files, capsys, extra):
    path, csv_path = cli_files
    assert main(['audit-research', str(path), str(csv_path), '--us-symbol', 'SPY', *extra]) == 2
    output = capsys.readouterr()
    assert 'PRIVATE' not in output.out + output.err and str(path) not in output.out + output.err


@pytest.mark.parametrize('bad', [b'{"version":1,"version":1}', b'{"nested":{"x":1,"x":2}}', b'\xff', b'[]'])
def test_cli_rejects_ambiguous_or_unreadable_json(cli_files, capsys, bad):
    path, csv_path = cli_files
    path.write_bytes(bad)
    assert main(['audit-research', str(path), str(csv_path), '--us-symbol', 'SPY']) == 2
    assert capsys.readouterr().out.startswith('Research audit blocked:')
