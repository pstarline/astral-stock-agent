"""Offline staging of declared observation pages; no broker I/O or authority.

This is a local mock contract, not a provider response schema. Query/scope and
cursor declarations do not authenticate origin, snapshot consistency, freshness,
or complete account coverage. A missing row NEVER resolves a known order.
Nothing here persists observations, unlocks the intent journal, or sends orders.
"""
from dataclasses import dataclass, field
import re

from .broker_order_state import OrderBook, OrderStateError, apply_observation


class ObservationPagesError(ValueError):
    """Private inputs are deliberately excluded from errors."""


def _require(condition):
    if not condition:
        raise ObservationPagesError('observation pages require reconciliation')


def _digest(value):
    return type(value) is str and re.fullmatch(r'[0-9a-f]{64}', value) is not None


def _cursor(value):
    return (value is None or type(value) is str
            and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value) is not None)


@dataclass(frozen=True)
class OrderObservation:
    client_order_id: str = field(repr=False)
    status: str = field(repr=False)
    broker_order_id: str | None = field(repr=False)
    filled_quantity: int = field(repr=False)


@dataclass(frozen=True)
class ObservationPage:
    mode: str = field(repr=False)
    scope_digest: str = field(repr=False)
    query_digest: str = field(repr=False)
    request_cursor: str | None = field(repr=False)
    next_cursor: str | None = field(repr=False)
    observations: tuple[OrderObservation, ...] = field(repr=False)


def apply_observation_pages(book: OrderBook, *, query_digest: str,
                            pages: tuple[ObservationPage, ...]) -> OrderBook:
    """Validate a whole bounded chain, then atomically return a new model.

    Callers must supply all successful pages from the initial request through an
    explicit terminal page. Failed, truncated, repeated, mixed or conflicting
    input raises without mutating the original book. No absent row is inferred,
    including an empty terminal result. Provider-specific validation, provenance,
    inventory completeness, time/sequence and durable reconciliation remain
    separate requirements before any future operational use.
    """
    _require(type(book) is OrderBook and _digest(query_digest))
    _require(type(pages) is tuple and 0 < len(pages) <= 16)
    expected_cursor, cursors, keys, count = None, set(), set(), 0
    for index, page in enumerate(pages):
        _require(type(page) is ObservationPage)
        _require(type(page.mode) is str and page.mode == book.mode)
        _require(_digest(page.scope_digest) and page.scope_digest == book.scope_digest)
        _require(_digest(page.query_digest) and page.query_digest == query_digest)
        _require(_cursor(page.request_cursor) and _cursor(page.next_cursor))
        _require(page.request_cursor == expected_cursor)
        terminal = index == len(pages) - 1
        _require((page.next_cursor is None) == terminal)
        if page.next_cursor is not None:
            _require(page.next_cursor not in cursors)
            cursors.add(page.next_cursor)
        expected_cursor = page.next_cursor
        _require(type(page.observations) is tuple)
        count += len(page.observations)
        _require(count <= 1024)
        for observation in page.observations:
            _require(type(observation) is OrderObservation)
            key = observation.client_order_id
            _require(type(key) is str and re.fullmatch(r'[A-Za-z0-9_-]{1,64}', key))
            _require(key not in keys)  # Even identical overlaps need explicit reconciliation.
            keys.add(key)

    staged = book
    try:
        for page in pages:
            for observation in page.observations:
                staged = apply_observation(
                    staged, observation.client_order_id, status=observation.status,
                    broker_order_id=observation.broker_order_id,
                    filled_quantity=observation.filled_quantity,
                )
    except OrderStateError:
        raise ObservationPagesError('observation pages require reconciliation') from None
    return staged
