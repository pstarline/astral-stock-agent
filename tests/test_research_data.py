"""Synthetic prices only: transfer checks never grant deployment authority."""
import copy
import hashlib
from dataclasses import replace
from decimal import Decimal

import pytest

from astral_stock_agent.research_data import (
    HEADERS, MAX_BYTES, ResearchDataError, alignment_report,
    read_daily_artifact, require_aligned_history,
)

START = '2026-11-02T00:00:00Z'
END = '2026-11-06T23:59:59Z'
ROWS = ['2026-11-02T00:00:00Z,10,12,9,11,100',
        '2026-11-03T00:00:00Z,11,13,10,12,0']


def bundle(rows=None, raw=None):
    if rows is None:
        rows = ROWS
    data = raw if raw is not None else (','.join(HEADERS) + '\n' + '\n'.join(rows) + '\n').encode()
    meta = dict(status='ok', symbol='SPY', tf='1d', delivery='download',
                limit_capped=False, has_more=False, count=len(rows),
                series_metadata=dict(asset_type='stock', price_kind='ohlcv',
                                     synthetic_ohlc=False, volume_available=True),
                artifact=dict(format='csv', row_count=len(rows), size_bytes=len(data),
                              sha256=hashlib.sha256(data).hexdigest()))
    return data, meta


def parse(data, meta, **overrides):
    return read_daily_artifact(data, meta, **(dict(symbol='SPY', start=START, end=END) | overrides))


def test_accepts_official_shape_without_optional_source_count():
    data, meta = bundle()
    before = copy.deepcopy(meta)
    history = parse(data, meta)
    assert len(history.bars) == 2
    assert history.bars[0].close == Decimal('11')
    assert history.bars[1].volume == 0
    assert history.sha256 == hashlib.sha256(data).hexdigest()
    assert meta == before


def test_uses_complete_artifact_even_if_inline_bars_downsampled():
    data, meta = bundle()
    meta.update(bars_downsampled=True, bars=[{'c': '999'}], source_count=2)
    assert parse(data, meta).bars[0].close == Decimal('11')


@pytest.mark.parametrize('path,value', [
    ('status', 'error'), ('symbol', 'BTCUSD'), ('tf', '15m'),
    ('delivery', 'inline'), ('limit_capped', True), ('has_more', True),
    ('limit_capped', 0), ('has_more', 0), ('count', True), ('count', 0),
    ('count', 50001), ('source_count', None), ('source_count', True),
    ('source_count', 1), ('artifact.row_count', 1), ('artifact.row_count', True),
    ('artifact.size_bytes', True), ('artifact.size_bytes', 1),
    ('artifact.sha256', 'wrong'), ('artifact.format', 'json'),
    ('series_metadata.asset_type', 'crypto'), ('series_metadata.price_kind', 'close'),
    ('series_metadata.synthetic_ohlc', True), ('series_metadata.synthetic_ohlc', 0),
    ('series_metadata.volume_available', False), ('series_metadata.volume_available', 1),
])
def test_metadata_fail_closed(path, value):
    data, meta = bundle()
    keys = path.split('.')
    target = meta
    for key in keys[:-1]:
        target = target[key]
    target[keys[-1]] = value
    with pytest.raises(ResearchDataError):
        parse(data, meta)


@pytest.mark.parametrize('rows', [
    [ROWS[1], ROWS[0]], [ROWS[0], ROWS[0]],
    [ROWS[0], ROWS[0].replace('00:00:00', '01:00:00')],
    [ROWS[0].replace('2026-11-02', '2026-10-31')],
    [ROWS[0].replace('2026-11-02', '2026-11-09')],
    [ROWS[0].replace('T00:00:00Z', 'T00:00:00')],
    [ROWS[0].replace('2026-11-02T00:00:00Z', 'private-invalid-time')],
    ['2026-11-02T00:00:00Z,10,9,8,11,100'],
    ['2026-11-02T00:00:00Z,10,12,11,11,100'],
    ['2026-11-02T00:00:00Z,10,12,9,11'],
    ['2026-11-02T00:00:00Z,10,12,9,11,100,extra'],
])
def test_malformed_bar_sequences(rows):
    with pytest.raises(ResearchDataError):
        parse(*bundle(rows))


@pytest.mark.parametrize('value', ['NaN', 'Infinity', '-Infinity', '-1', '0', 'secret-value'])
def test_invalid_prices_do_not_leak(value):
    data, meta = bundle([ROWS[0].replace(',10,', ',' + value + ',', 1)])
    with pytest.raises(ResearchDataError) as exc:
        parse(data, meta)
    assert str(exc.value) == 'invalid OHLCV number'


@pytest.mark.parametrize('raw', [b'\xff', b'wrong,headers\n', b'timestamp,open,high,low,close,volume\n"unterminated'])
def test_bad_csv_encoding_and_columns(raw):
    with pytest.raises(ResearchDataError):
        parse(*bundle(raw=raw))


def test_actual_csv_count_must_match_declared_counts():
    data, meta = bundle()
    meta['count'] = meta['artifact']['row_count'] = 1
    with pytest.raises(ResearchDataError):
        parse(data, meta)
    meta['count'] = meta['artifact']['row_count'] = 3
    with pytest.raises(ResearchDataError):
        parse(data, meta)


@pytest.mark.parametrize('data', [b'', 'not-bytes', b'x' * (MAX_BYTES + 1)])
def test_size_and_bytes_boundaries(data):
    with pytest.raises(ResearchDataError):
        parse(data, bundle()[1])


@pytest.mark.parametrize('overrides', [dict(symbol='BTC/USD'), dict(start=END),
                                       dict(end=START), dict(start='secret-start')])
def test_configured_scope_and_time_bounds(overrides):
    with pytest.raises(ResearchDataError):
        parse(*bundle(), **overrides)


def test_cross_symbol_gaps_are_visible_and_never_filled():
    first = parse(*bundle())
    data, meta = bundle([ROWS[0]])
    meta['symbol'] = 'IEF'
    second = parse(data, meta, symbol='IEF')
    report = alignment_report([first, second])
    assert not report['aligned']
    assert report['missing_bar_labels']['SPY'] == []
    assert report['missing_bar_labels']['IEF'] == ['2026-11-03T00:00:00+00:00']
    assert len(second.bars) == 1
    with pytest.raises(ResearchDataError):
        require_aligned_history([first, second])


def test_aligned_is_not_calendar_adjustment_or_deployment_approval():
    first = parse(*bundle())
    second = replace(first, symbol='IEF')
    report = require_aligned_history([first, second])
    assert report['aligned']
    assert report['observed_union_count'] == 2
    assert not report['calendar_verified']
    assert not report['corporate_actions_verified']
    assert not report['deployment_approved']


@pytest.mark.parametrize('histories', [[], [None]])
def test_empty_or_unparsed_histories_rejected(histories):
    with pytest.raises(ResearchDataError):
        alignment_report(histories)


def test_duplicate_symbols_and_excess_histories_rejected():
    history = parse(*bundle())
    for histories in ([history, history], [replace(history, symbol=str(i)) for i in range(21)]):
        with pytest.raises(ResearchDataError):
            alignment_report(histories)
