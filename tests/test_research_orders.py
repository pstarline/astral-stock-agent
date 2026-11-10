"""Synthetic order-label comparisons with no private provider records."""
import copy
from dataclasses import replace

import pytest

from astral_stock_agent.research_data import ResearchDataError
from astral_stock_agent.research_orders import compare_backtest_orders
from test_research_audit import inputs


def evidence():
    h, s, curve = inputs()
    orders = [{'timestamp': str(h.bars[i].timestamp), 'asset': 'SPY', 'side': side,
               'shares': 1, 'price': price} for i, side, price in [(1, 'buy', 12), (2, 'sell', 8)]]
    status = {k: curve[k] for k in ('backtest_id', 'strategy_id', 'backtest_status')}
    result = {'delivery': 'inline', 'strategy': {'strategy_id': 7, 'latest_backtest': {
        '_mcp_backtest_id': 'synthetic-run', 'health_state': 'ok', 'degraded': False,
        'backtest_period': copy.deepcopy(curve['results']['backtest_period']),
        'trade_statistics': {'executed_orders': len(orders)},
        'vectorbt_portfolio': {'orders': orders},
        'fiscal_data': {'execution': {'orders_truncated': False, 'orders': copy.deepcopy(orders)}}}}}
    return h, s, result, status


def audit(h, s, result, status):
    return compare_backtest_orders(h, s, result, status, expected_backtest_id='synthetic-run',
                                    expected_strategy_id=7, first_compared_index=0)


def test_exact_sequence_is_only_a_research_diagnostic():
    values = evidence()
    before = copy.deepcopy(values[2:])
    a = audit(*values)
    assert a['sequences_match'] and a['compared_pairs'] == 2
    assert not any(a[k] for k in ('fees_verified', 'full_strategy_parity_verified',
                                 'actual_execution_verified', 'deployment_approved'))
    assert values[2:] == before
    assert 'synthetic-run' not in str(a) and 'SPY' not in str(a)


def test_signal_day_labels_are_not_shifted_into_execution_day_matches():
    h, s, r, status = evidence()
    for key in (r['strategy']['latest_backtest']['vectorbt_portfolio'],
                r['strategy']['latest_backtest']['fiscal_data']['execution']):
        for i, row in enumerate(key['orders']):
            row['timestamp'] = str(h.bars[i].timestamp)
    a = audit(h, s, r, status)
    assert not a['sequences_match'] and a['label_mismatches'] == 2
    assert a['price_mismatches'] == 0 and a['side_or_size_mismatches'] == 0


def test_final_order_is_counted_but_open_trade_placeholder_is_not_an_exit():
    h, s, r, status = evidence()
    b = r['strategy']['latest_backtest']
    extra = dict(b['vectorbt_portfolio']['orders'][-1], side='buy')
    b['vectorbt_portfolio']['orders'].append(extra)
    b['fiscal_data']['execution']['orders'].append(copy.deepcopy(extra))
    b['trade_statistics']['executed_orders'] = 3
    b['trades'] = [{'Status': 'Open', 'Exit Timestamp': 'unverified-placeholder'}]
    a = audit(h, s, r, status)
    assert a['external_orders'] == 3 and a['unpaired_tail_orders'] == 1
    assert not a['sequences_match']


@pytest.mark.parametrize('field,value', [('shares', '2'), ('side', 'sell'), ('price', '12.5')])
def test_order_fields_are_compared_without_reconstructing_identity(field, value):
    h, s, r, status = evidence()
    b = r['strategy']['latest_backtest']
    for container in (b['vectorbt_portfolio'], b['fiscal_data']['execution']):
        container['orders'][0][field] = value
    a = audit(h, s, r, status)
    assert not a['sequences_match'] and a['label_mismatches'] == 0


@pytest.mark.parametrize('mutation', ['status', 'run', 'strategy', 'truncated', 'count', 'projection',
                                     'scope', 'degraded', 'bad_number', 'outside', 'duplicate_projection'])
def test_wrong_or_incomplete_evidence_is_rejected_with_fixed_error(mutation):
    h, s, r, status = evidence()
    b = r['strategy']['latest_backtest']
    if mutation == 'status': status['backtest_status'] = 'running'
    elif mutation == 'run': b['_mcp_backtest_id'] = 'private-other-run'
    elif mutation == 'strategy': r['strategy']['strategy_id'] = True
    elif mutation == 'truncated': b['fiscal_data']['execution']['orders_truncated'] = True
    elif mutation == 'count': b['trade_statistics']['executed_orders'] = True
    elif mutation == 'projection': b['fiscal_data']['execution']['orders'] = []
    elif mutation == 'scope': b['backtest_period']['raw_data_start'] = str(h.bars[1].timestamp)
    elif mutation == 'degraded': b['degraded'] = True
    elif mutation == 'duplicate_projection': b['vectorbt_portfolio']['orders'].append(b['vectorbt_portfolio']['orders'][0])
    else:
        for container in (b['vectorbt_portfolio'], b['fiscal_data']['execution']):
            if mutation == 'bad_number': container['orders'][0]['shares'] = True
            else: container['orders'][0]['timestamp'] = '2026-11-05T00:00:00Z'
    with pytest.raises(ResearchDataError) as exc:
        audit(h, s, r, status)
    assert str(exc.value) == 'complete matching-scope backtest orders required'


def test_local_fill_must_retain_causal_next_open_indices():
    h, s, r, status = evidence()
    fill = replace(s.fills[0], execution_index=0)
    with pytest.raises(ResearchDataError):
        audit(h, replace(s, fills=(fill, *s.fills[1:])), r, status)
