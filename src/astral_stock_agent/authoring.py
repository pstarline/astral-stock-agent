"""Original SMA syntax preview using the observed Astral authoring interface.

Always validate_only. No saving, backtest start, deployment or order submission
is available through this adapter. Syntax acceptance is not profitability,
execution parity or approval of operational sizing/capital/risk.
"""
import copy
import json

from .report import symbol as checked_symbol


class AuthoringError(ValueError):
    pass


def sma_preview_request(*, symbol, window, units, verified_us_symbols):
    try:
        symbol = checked_symbol(symbol)
        if symbol not in set(verified_us_symbols):
            raise ValueError
        if type(window) is not int or not 2 <= window <= 252:
            raise ValueError
        if type(units) is not int or units <= 0:
            raise ValueError
    except (ValueError, TypeError):
        raise AuthoringError('verified symbol and explicit research parameters required') from None
    alias = f'SMA_{window}'
    return {
        'name': f'ASA research {symbol} SMA{window} fixed-shares preview',
        'description': 'Original research syntax preview; never save, allocate capital or deploy.',
        'bar_size': '1d', 'validate_only': True,
        'assets': [{'symbol': symbol, 'type': 'equity', 'currency': 'USD'}],
        'series': [{'alias': alias, 'expression': f'SMA({symbol}.close, {window})', 'scope': [symbol]}],
        'signals': [{'name': 'ABOVE', 'expr': f'{symbol}.close > {alias}'},
                    {'name': 'BELOW', 'expr': f'{symbol}.close <= {alias}'}],
        'orders': [{'name': name, 'asset': symbol, 'side': side, 'sizing_type': 'fixed_shares',
                    'sizing_value': units, 'accumulate': False}
                   for name, side in [('BUY_FIXED', 'buy'), ('SELL_FIXED', 'sell')]],
        'rules': [{'name': 'ENTER', 'condition': 'ABOVE', 'actions': ['BUY_FIXED']},
                  {'name': 'EXIT', 'condition': 'BELOW', 'actions': ['SELL_FIXED']}],
    }


def inspect_sma_preview(response, request):
    """Require exact round-trip syntax/sizing and no implicit assumptions.

    Raw compiler state and strategy code are not returned. The observed on_open
    label remains a literal; its next-session semantics are NOT certified here.
    """
    try:
        if not isinstance(response, dict) or response.get('isError'):
            raise ValueError
        result = response.get('structuredContent', response)
        if (result['status'] != 'validated' or 'strategy_id' in result
                or 'saved_strategy_id' in result or result.get('record') is not None):
            raise ValueError
        # Regenerate to prevent arbitrary/mutated requests being blessed here.
        symbol = request['assets'][0]['symbol']
        units = request['orders'][0]['sizing_value']
        window = int(request['series'][0]['alias'].removeprefix('SMA_'))
        expected = sma_preview_request(symbol=symbol, window=window, units=units,
                                       verified_us_symbols=[symbol])
        if (json.dumps(request, sort_keys=True, allow_nan=False)
                != json.dumps(expected, sort_keys=True, allow_nan=False)
                or request['validate_only'] is not True):
            raise ValueError
        state = result['state']
        expected_state = {
            'assets': request['assets'], 'series': request['series'],
            'signals': [{'name': row['name'], 'expression': row['expr'], 'signal_type': 'function'}
                        for row in request['signals']],
            'orders': [{'name': row['name'], 'asset': symbol, 'order_type': row['side'],
                        'position_size_type': 'fixed_shares', 'value': units, 'accumulate': False}
                       for row in request['orders']],
            'rules_entry': [{'name': row['name'], 'condition': row['condition'], 'action': row['actions']}
                            for row in request['rules']],
            'rebalances': [], 'rules_scheduled': [], 'altdata': [], 'indicators': [],
        }
        if state != expected_state:
            raise ValueError
        if any(type(row['value']) is not int or row['accumulate'] is not False for row in state['orders']):
            raise ValueError
        intent = result['intent']
        if intent['assumptions'] != [] or intent['assumptions_truncated'] is not False:
            raise ValueError
        run = result['strategy']
        engine = run['engine']
        if (run['t'] != 'run_strategy' or type(run['v']) is not int or run['v'] != 2
                or run['mode'] != 'backtest' or engine != {
                    'bar_size': '1d', 'timezone': 'UTC', 'events': ['bar_close'],
                    'follow_live': True, 'fill_price_mode': 'on_open'}
                or engine['follow_live'] is not True):
            raise ValueError
        return {'kind': 'research_authoring_preview', 'syntax_validated': True,
                'validation_only': True, 'symbol': symbol, 'window': window, 'research_units': units,
                'implicit_sizing': False, 'engine_fill_mode': 'on_open',
                'full_strategy_parity_verified': False, 'deployment_approved': False,
                'new_orders_allowed': False, 'persisted_strategy_verified': False}
    except (ValueError, KeyError, TypeError, AttributeError, IndexError):
        raise AuthoringError('research preview unverified or authoring contract changed') from None


async def validate_sma_preview(call_tool, *, symbol, window, units, verified_us_symbols):
    """One authenticated validation call; no retries or credential acquisition."""
    request = sma_preview_request(symbol=symbol, window=window, units=units,
                                  verified_us_symbols=verified_us_symbols)
    try:
        result = await call_tool('astral_strategy_build', copy.deepcopy(request))
    except Exception:
        raise AuthoringError('research syntax validation unavailable') from None
    return inspect_sma_preview(result, request)
