"""Synthetic offline bytes; no broker data or private storage."""
from dataclasses import replace
from decimal import Decimal
import json

import pytest

from astral_stock_agent.broker_observation_codec import (
    ERROR, MAX_BYTES, ObservationCodecError, decode_observation_pages,
    encode_observation_pages,
)
from astral_stock_agent.broker_observation_pages import (
    ObservationPage, OrderObservation, apply_observation_pages,
)
from astral_stock_agent.broker_order_state import OrderBook, OrderIntent, OrderRecord


SCOPE, QUERY = 'a' * 64, 'b' * 64


def baseline(mode='paper'):
    return OrderBook(mode, SCOPE, tuple(OrderRecord(
        OrderIntent(mode, SCOPE, key, 'SPY', 'buy', 3, Decimal('10')), 'unknown')
        for key in ('first', 'second')))


def chain(mode='paper'):
    return (ObservationPage(mode, SCOPE, QUERY, None, 'next', (
        OrderObservation('first', 'partially_filled', 'broker1', 1),)),
        ObservationPage(mode, SCOPE, QUERY, 'next', None, ()))


def encode(book=None, pages=None):
    return encode_observation_pages(book or baseline(), query_digest=QUERY,
                                    pages=chain() if pages is None else pages)


def decode(raw, book=None, query=QUERY):
    return decode_observation_pages(raw, book=book or baseline(), query_digest=query)


def rejected(raw, book=None, query=QUERY):
    original = book or baseline()
    records = original.records
    with pytest.raises(ObservationCodecError) as error:
        decode(raw, original, query)
    assert str(error.value) == ERROR
    assert 'private-sentinel' not in repr(error.value)
    assert original.records is records
    assert original.order_submission_approved is False


@pytest.mark.parametrize('mode', ['paper', 'live'])
def test_roundtrip_preserves_partial_missing_rows_and_original(mode):
    book, pages = baseline(mode), chain(mode)
    raw = encode_observation_pages(book, query_digest=QUERY, pages=pages)
    restored = decode_observation_pages(raw, book=book, query_digest=QUERY)
    assert restored == pages
    assert encode_observation_pages(book, query_digest=QUERY, pages=restored) == raw
    result = apply_observation_pages(book, query_digest=QUERY, pages=restored)
    assert result.records[0].filled_quantity == 1
    assert result.records[1] is book.records[1]
    assert result.reconciliation_required and not result.order_submission_approved
    assert all(r.status == 'unknown' for r in book.records)
    assert 'broker1' not in repr(restored)
    # A different state, even produced by these exact pages, is not the baseline.
    rejected(raw, result)


def test_empty_result_is_not_reconciliation():
    book = baseline()
    pages = (ObservationPage('paper', SCOPE, QUERY, None, None, ()),)
    restored = decode(encode(book, pages), book)
    assert apply_observation_pages(book, query_digest=QUERY, pages=restored) is book
    assert book.reconciliation_required


@pytest.mark.parametrize('change', ['price', 'quantity', 'symbol', 'side', 'key',
                                   'status', 'broker', 'fill', 'omitted', 'order', 'mode', 'scope'])
def test_binding_covers_every_intent_and_prior_outcome_including_absent_rows(change):
    book = baseline()
    raw = encode(book)
    first, second = book.records
    if change in ('price', 'quantity', 'symbol', 'side', 'key'):
        changes = dict(price={'limit_price': Decimal('10.0')}, quantity={'quantity': 4},
                       symbol={'symbol': 'QQQ'}, side={'side': 'sell'},
                       key={'client_order_id': 'third'})[change]
        changed = replace(book, records=(first, replace(second, intent=replace(second.intent, **changes))))
    elif change in ('status', 'broker', 'fill'):
        changed_record = dict(status=replace(second, status='accepted', broker_order_id='b2'),
                              broker=replace(second, broker_order_id='b2'),
                              fill=replace(second, filled_quantity=1))[change]
        changed = replace(book, records=(first, changed_record))
    elif change == 'omitted':
        changed = replace(book, records=(first,))
    elif change == 'order':
        changed = replace(book, records=(second, first))
    elif change == 'mode':
        changed = baseline('live')
    else:
        changed = OrderBook('paper', 'c' * 64, tuple(replace(r, intent=replace(
            r.intent, scope_digest='c' * 64)) for r in book.records))
    rejected(raw, changed)


@pytest.mark.parametrize('raw', [b'', b'\xff', b'null', b'[]', b'{', b'NaN', b'Infinity',
                               b'"private-sentinel"', b' ' * (MAX_BYTES + 1),
                               b'[' * 1100 + b']' * 1100, '', bytearray(b'{}')])
def test_malformed_nonfinite_deep_or_oversized_input_is_fixed_error(raw):
    rejected(raw)


@pytest.mark.parametrize('target', ['root', 'page', 'row'])
@pytest.mark.parametrize('action', ['extra', 'missing', 'duplicate'])
def test_closed_keys_and_duplicate_json_keys_at_every_level(target, action):
    value = json.loads(encode())
    item = value if target == 'root' else value['pages'][0] if target == 'page' else value['pages'][0]['observations'][0]
    key = next(iter(item))
    if action == 'extra':
        item['private-sentinel'] = True
    elif action == 'missing':
        del item[key]
    else:
        raw = json.dumps(value)
        needle = json.dumps(key) + ':'
        raw = raw.replace(needle, needle + json.dumps(item[key]) + ', ' + needle, 1)
        rejected(raw.encode())
        return
    rejected(json.dumps(value).encode())


@pytest.mark.parametrize('change', ['schema', 'digest', 'query', 'page_query', 'cursor',
                                   'incomplete', 'overlap', 'status', 'bool_fill', 'regression',
                                   'untracked', 'pages_type', 'rows_type', 'too_many_pages', 'too_many_rows'])
def test_revalidates_decoded_semantics_and_page_limits(change):
    value = json.loads(encode())
    page, row = value['pages'][0], value['pages'][0]['observations'][0]
    if change == 'schema': value['schema'] = 'private-sentinel'
    elif change == 'digest': value['baseline_digest'] = 'c' * 64
    elif change == 'query': value['query_digest'] = 'c' * 64
    elif change == 'page_query': page['query_digest'] = 'c' * 64
    elif change == 'cursor': page['request_cursor'] = 'private-sentinel'
    elif change == 'incomplete': value['pages'].pop()
    elif change == 'overlap': value['pages'][1]['observations'] = [row]
    elif change == 'status': row['status'] = 'private-sentinel'
    elif change == 'bool_fill': row['filled_quantity'] = True
    elif change == 'regression': row['filled_quantity'] = 0
    elif change == 'untracked': row['client_order_id'] = 'private-sentinel'
    elif change == 'pages_type': value['pages'] = {}
    elif change == 'rows_type': page['observations'] = {}
    elif change == 'too_many_pages': value['pages'] *= 9
    else: page['observations'] = [row] * 1025
    rejected(json.dumps(value).encode())


def test_caller_query_and_invalid_encode_are_checked_without_mutation():
    rejected(encode(), query='c' * 64)
    rejected(encode(), query=True)
    for pages in [(), [], (replace(chain()[0], next_cursor=None), chain()[1])]:
        with pytest.raises(ObservationCodecError, match='^' + ERROR + '$'):
            encode(pages=pages)


def test_content_binding_is_not_a_signature_or_broker_authentication():
    value = json.loads(encode())
    # A self-consistent alternate DECLARATION can pass: bytes are not signed.
    value['pages'][0]['observations'][0].update(status='filled', filled_quantity=3)
    restored = decode(json.dumps(value).encode())
    book = baseline()
    result = apply_observation_pages(book, query_digest=QUERY, pages=restored)
    assert result.records[0].status == 'filled'
    assert result.order_submission_approved is False
    assert book.records[0].status == 'unknown'


def test_private_long_decimal_encoding_rejected_and_no_io_is_used(monkeypatch):
    book = baseline()
    record = replace(book.records[0], intent=replace(book.records[0].intent,
                      limit_price=Decimal('1.' + '0' * 128)))
    with pytest.raises(ObservationCodecError, match='^' + ERROR + '$'):
        encode(replace(book, records=(record, book.records[1])))
    def forbidden(*args, **kwargs):
        pytest.fail('codec attempted I/O')
    import builtins
    import socket
    import subprocess
    monkeypatch.setattr(builtins, 'open', forbidden)
    monkeypatch.setattr(socket, 'socket', forbidden)
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    assert decode(encode()) == chain()
