"""Private offline page bytes bound to a caller's original order book.

This local mock format is NOT a provider schema, signature, durable store or
reconciliation authority. No I/O, clocks, journal unlocking or order dispatch.
Returned bytes/pages contain private identifiers and must not enter public logs.
"""
from dataclasses import asdict
from hashlib import sha256
import json

from .broker_observation_pages import (
    ObservationPage, OrderObservation, apply_observation_pages,
)
from .broker_order_state import OrderBook
from .independent_ledger import _canonical, _unique


MAX_BYTES = 512000
ERROR = 'private observation bytes require reconciliation'


class ObservationCodecError(ValueError):
    pass


def _reject_constant(_):
    raise ValueError


def _closed(value, keys):
    if type(value) is not dict or set(value) != set(keys):
        raise ValueError


def _book_digest(book):
    if type(book) is not OrderBook:
        raise ValueError
    # Include ALL intents and prior outcomes, including rows absent from pages.
    # Exact decimal spelling and record order are deliberately part of binding.
    value = asdict(book)
    for record in value['records']:
        price = str(record['intent']['limit_price'])
        if len(price) > 128:
            raise ValueError
        record['intent']['limit_price'] = price
    return sha256(_canonical(value)).hexdigest()


def encode_observation_pages(book, *, query_digest, pages):
    """Validate before encoding; no result is persisted or applied in place."""
    try:
        apply_observation_pages(book, query_digest=query_digest, pages=pages)
        value = dict(schema='private_order_observation_pages_v1',
                     baseline_digest=_book_digest(book), query_digest=query_digest,
                     pages=[asdict(page) for page in pages])
        raw = _canonical(value)
        if not 0 < len(raw) <= MAX_BYTES:
            raise ValueError
        return raw
    except (ValueError, TypeError, OverflowError, RecursionError):
        raise ObservationCodecError(ERROR) from None


def decode_observation_pages(raw, *, book, query_digest):
    """Return validated pages only for the supplied original baseline/query.

    Callers cannot use this to restore an approved book or authorize preflight.
    A matching digest proves content agreement only, not authenticity, freshness,
    historical completeness, or an atomic relationship with an intent journal.
    """
    try:
        if type(raw) is not bytes or not 0 < len(raw) <= MAX_BYTES:
            raise ValueError
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique,
                           parse_constant=_reject_constant)
        _closed(value, ('schema', 'baseline_digest', 'query_digest', 'pages'))
        if (value['schema'] != 'private_order_observation_pages_v1'
                or value['baseline_digest'] != _book_digest(book)
                or type(query_digest) is not str or value['query_digest'] != query_digest):
            raise ValueError
        rows = value['pages']
        if type(rows) is not list or not 0 < len(rows) <= 16:
            raise ValueError
        pages, count = [], 0
        for page in rows:
            _closed(page, ('mode', 'scope_digest', 'query_digest', 'request_cursor',
                           'next_cursor', 'observations'))
            observations = page['observations']
            if type(observations) is not list:
                raise ValueError
            count += len(observations)
            if count > 1024:
                raise ValueError
            for row in observations:
                _closed(row, ('client_order_id', 'status', 'broker_order_id', 'filled_quantity'))
            pages.append(ObservationPage(**{**page, 'observations': tuple(
                OrderObservation(**row) for row in observations)}))
        result = tuple(pages)
        apply_observation_pages(book, query_digest=query_digest, pages=result)
        return result
    except (ValueError, TypeError, OverflowError, RecursionError):
        raise ObservationCodecError(ERROR) from None
