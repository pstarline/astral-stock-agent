"""Compare backtest order sequences, never real fills or broker executions.

Sequence rows are compared by position; no dates are shifted and no order IDs
or execution times are inferred from a daily bar label. Raw rows stay private.
"""
from decimal import Context, Decimal, localcontext

from .research_audit import CURVE_TOLERANCE_USD
from .research_data import DailyHistory, ResearchDataError, _timestamp
from .research_execution import PriceOnlySimulation, _decimal


def compare_backtest_orders(history, simulation, response, status_response, *,
                            expected_backtest_id, expected_strategy_id, first_compared_index):
    """Inspect the complete latest backtest from strategy_get(include_results).

    A separate succeeded status must name the same exact run. Counts and the
    explicit untruncated execution projection must agree. Price differences
    tolerate serialization noise only; fees/rules/corporate actions remain
    unverified. A trade row marked Open is never synthesized into an exit.
    """
    try:
        if (not isinstance(history, DailyHistory) or not isinstance(simulation, PriceOnlySimulation)
                or not isinstance(expected_backtest_id, str) or not expected_backtest_id
                or type(expected_strategy_id) is not int or expected_strategy_id <= 0
                or type(first_compared_index) is not int
                or not 0 <= first_compared_index < len(history.bars) <= 50_000
                or response.get('isError') or status_response.get('isError')):
            raise ValueError
        result = response.get('structuredContent', response)
        state = status_response.get('structuredContent', status_response)
        if (state['backtest_id'] != expected_backtest_id
                or type(state['strategy_id']) is not int or state['strategy_id'] != expected_strategy_id
                or state['backtest_status'] != 'succeeded'
                or result.get('delivery') != 'inline' or result.get('artifact')
                or any(result.get(k, False) is not False for k in ('strategy_truncated', 'response_truncated'))):
            raise ValueError
        strategy = result['strategy']
        backtest = strategy['latest_backtest']
        if (type(strategy['strategy_id']) is not int or strategy['strategy_id'] != expected_strategy_id
                or backtest['_mcp_backtest_id'] != expected_backtest_id
                or backtest['health_state'] != 'ok' or backtest['degraded'] is not False):
            raise ValueError
        labels = tuple(_timestamp(b.timestamp.isoformat()) for b in history.bars)
        if any(a >= b for a, b in zip(labels, labels[1:])):
            raise ValueError
        period = backtest['backtest_period']
        if (period['bar_size'] != '1d' or period['history_capped'] is not False
                or _timestamp(period['raw_data_start']) != labels[0]
                or _timestamp(period['raw_data_end']) != labels[-1]
                or _timestamp(period['first_eligible_trade_at']) != labels[first_compared_index]):
            raise ValueError
        raw = backtest['vectorbt_portfolio']['orders']
        execution = backtest['fiscal_data']['execution']
        count = backtest['trade_statistics']['executed_orders']
        if (not isinstance(raw, list) or len(raw) > 10_000
                or type(count) is not int or count != len(raw)
                or execution['orders_truncated'] is not False or execution['orders'] != raw):
            raise ValueError
        observed = []
        eligible = set(labels[first_compared_index:])
        for row in raw:
            stamp = _timestamp(row['timestamp'])
            if (stamp not in eligible or row['asset'] != history.symbol or row['side'] not in ('buy', 'sell')
                    or (observed and stamp < observed[-1][0])):
                raise ValueError
            if type(row['shares']) not in (str, int, float, Decimal) or type(row['price']) not in (str, int, float, Decimal):
                raise ValueError
            size, price = _decimal(str(row['shares'])), _decimal(str(row['price']))
            if size <= 0 or price <= 0:
                raise ValueError
            observed.append((stamp, row['side'], size, price))
        local = []
        if len(simulation.fills) > 10_000:
            raise ValueError
        for fill in simulation.fills:
            if (type(fill.signal_index) is not int or type(fill.execution_index) is not int
                    or not first_compared_index <= fill.signal_index < fill.execution_index < len(labels)
                    or fill.execution_index != fill.signal_index + 1):
                raise ValueError
            stamp = labels[fill.execution_index]
            size, price = _decimal(fill.units_change), _decimal(fill.price)
            if not size or price <= 0 or (local and stamp < local[-1][0]):
                raise ValueError
            local.append((stamp, 'buy' if size > 0 else 'sell', size.copy_abs(), price))
        with localcontext(Context(prec=28)):
            pairs = list(zip(local, observed))
            label_gaps = sum(a[0] != b[0] for a, b in pairs)
            side_size_gaps = sum(a[1:3] != b[1:3] for a, b in pairs)
            price_gaps = sum(abs(a[3] - b[3]) > CURVE_TOLERANCE_USD for a, b in pairs)
        tail = abs(len(local) - len(observed))
        return {'kind': 'hypothetical_order_sequence_audit', 'local_orders': len(local),
                'external_orders': len(observed), 'compared_pairs': len(pairs),
                'label_mismatches': label_gaps, 'side_or_size_mismatches': side_size_gaps,
                'price_mismatches': price_gaps, 'unpaired_tail_orders': tail,
                'sequences_match': not (label_gaps or side_size_gaps or price_gaps or tail),
                'fees_verified': False, 'full_strategy_parity_verified': False,
                'actual_execution_verified': False, 'deployment_approved': False}
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        raise ResearchDataError('complete matching-scope backtest orders required') from None
