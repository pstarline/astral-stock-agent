"""Synthetic normalized preview responses for an original research definition."""
import asyncio
import copy

import pytest

from astral_stock_agent.authoring import (
    AuthoringError, inspect_sma_preview, sma_preview_request, validate_sma_preview,
)


def request(**overrides):
    return sma_preview_request(**(dict(symbol='SPY', window=20, units=1, verified_us_symbols=['SPY']) | overrides))


def result():
    return {'status': 'validated', 'state': {
        'assets': [{'symbol': 'SPY', 'type': 'equity', 'currency': 'USD'}],
        'series': [{'alias': 'SMA_20', 'expression': 'SMA(SPY.close, 20)', 'scope': ['SPY']}],
        'signals': [{'name': 'ABOVE', 'expression': 'SPY.close > SMA_20', 'signal_type': 'function'},
                    {'name': 'BELOW', 'expression': 'SPY.close <= SMA_20', 'signal_type': 'function'}],
        'orders': [{'name': name, 'asset': 'SPY', 'order_type': side,
                    'position_size_type': 'fixed_shares', 'value': 1, 'accumulate': False}
                   for name, side in [('BUY_FIXED', 'buy'), ('SELL_FIXED', 'sell')]],
        'rules_entry': [{'name': 'ENTER', 'condition': 'ABOVE', 'action': ['BUY_FIXED']},
                        {'name': 'EXIT', 'condition': 'BELOW', 'action': ['SELL_FIXED']}],
        'rebalances': [], 'rules_scheduled': [], 'altdata': [], 'indicators': [],
    }, 'intent': {'assumptions': [], 'assumptions_truncated': False},
        'strategy': {'t': 'run_strategy', 'v': 2, 'mode': 'backtest', 'engine': {
            'bar_size': '1d', 'timezone': 'UTC', 'events': ['bar_close'],
            'follow_live': True, 'fill_price_mode': 'on_open'}}}


def test_only_preview_and_explicit_fixed_shares_are_requested():
    r = request()
    assert r['validate_only'] is True
    assert 'capital' not in r and 'start' not in r and 'end' not in r
    assert all(row['sizing_type'] == 'fixed_shares' and row['sizing_value'] == 1 for row in r['orders'])
    assert all('expr' in row and 'expression' not in row for row in r['signals'])


def test_normalized_manifest_does_not_approve_strategy_or_retain_raw_code():
    response = result()
    response['strategy']['strategy_code'] = 'PRIVATE_CODE_SENTINEL'
    before = copy.deepcopy(response)
    manifest = inspect_sma_preview(response, request())
    assert manifest['syntax_validated'] and manifest['research_units'] == 1
    for field in ['full_strategy_parity_verified', 'deployment_approved', 'new_orders_allowed', 'persisted_strategy_verified']:
        assert manifest[field] is False
    assert 'PRIVATE' not in repr(manifest) and response == before


@pytest.mark.parametrize('overrides', [dict(units=True), dict(units=1.0), dict(units=0), dict(units=-1),
    dict(window=True), dict(window=1), dict(window=253), dict(symbol='BTCUSD'),
    dict(symbol='private-invalid-symbol'), dict(verified_us_symbols=None)])
def test_invalid_or_unverified_research_inputs_rejected(overrides):
    with pytest.raises(AuthoringError):
        request(**overrides)


@pytest.mark.parametrize('mutate', [
    lambda d: d.update(status='needs_correction'), lambda d: d.update(strategy_id=42),
    lambda d: d.update(record={}), lambda d: d['intent'].update(assumptions=[{'value': '90%'}]),
    lambda d: d['intent'].update(assumptions_truncated=True),
    lambda d: d['state']['orders'][0].update(position_size_type='percent_of_equity', value=.9),
    lambda d: d['state']['orders'][0].update(value=True),
    lambda d: d['state']['orders'][0].update(accumulate=True),
    lambda d: d['state']['orders'][0].update(protection={'unrequested': True}),
    lambda d: d['state']['signals'][0].update(expression='SPY.close < SMA_20'),
    lambda d: d['strategy'].update(mode='live'),
    lambda d: d['strategy']['engine'].update(fill_price_mode='on_close'),
    lambda d: d['strategy']['engine'].update(follow_live=1),
])
def test_changed_sizing_logic_or_engine_is_not_silently_accepted(mutate):
    d = result()
    mutate(d)
    with pytest.raises(AuthoringError) as exc:
        inspect_sma_preview(d, request())
    assert str(exc.value) == 'research preview unverified or authoring contract changed'


def test_request_cannot_disable_validation_or_add_capital():
    for field, value in [('validate_only', False), ('capital', 10000)]:
        r = request()
        r[field] = value
        with pytest.raises(AuthoringError):
            inspect_sma_preview(result(), r)


@pytest.mark.parametrize('field,value', [('sizing_value', True), ('accumulate', 0)])
def test_request_types_cannot_exploit_boolean_integer_equality(field, value):
    r = request()
    r['orders'][1][field] = value
    with pytest.raises(AuthoringError):
        inspect_sma_preview(result(), r)


def test_adapter_calls_only_validation_once_and_preserves_caller_request():
    calls = []
    async def callback(tool, args):
        calls.append((tool, copy.deepcopy(args)))
        return {'structuredContent': result()}
    manifest = asyncio.run(validate_sma_preview(callback, symbol='SPY', window=20, units=1, verified_us_symbols=['SPY']))
    assert len(calls) == 1 and calls[0][0] == 'astral_strategy_build'
    assert calls[0][1]['validate_only'] is True and manifest['new_orders_allowed'] is False


def test_no_automatic_retry_and_private_callback_errors_suppressed():
    calls = []
    async def fail(tool, args):
        calls.append(tool)
        raise RuntimeError('PRIVATE_TOKEN_SENTINEL')
    with pytest.raises(AuthoringError) as exc:
        asyncio.run(validate_sma_preview(fail, symbol='SPY', window=20, units=1, verified_us_symbols=['SPY']))
    assert len(calls) == 1 and str(exc.value) == 'research syntax validation unavailable'
