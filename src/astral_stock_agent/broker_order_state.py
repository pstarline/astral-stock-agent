"""Offline, immutable order bookkeeping; no adapter or submission authority.

All inputs are caller declarations, NOT authenticated broker observations. This
bounded in-memory model is not durable or concurrent idempotency. A future
adapter must bind scope to broker/account/environment, verify observations, and
persist intent before I/O. No account identifiers or credentials belong here.
Only positive integer-share limit intents are modeled. No cancel requests,
replacement, corrections, cash accounting, fees, sessions or risk gate yet.
"""
from dataclasses import dataclass, field, replace
from decimal import Decimal
import re


class OrderStateError(ValueError):
    """Fixed errors deliberately omit private caller input."""


def _require(condition):
    if not condition:
        raise OrderStateError('order state requires reconciliation')


def _identifier(value):
    return type(value) is str and re.fullmatch(r'[A-Za-z0-9_-]{1,64}', value) is not None


def _scope(value):
    return type(value) is str and re.fullmatch(r'[0-9a-f]{64}', value) is not None


@dataclass(frozen=True)
class OrderIntent:
    mode: str
    scope_digest: str = field(repr=False)
    client_order_id: str = field(repr=False)
    symbol: str
    side: str
    quantity: int
    limit_price: Decimal

    def __post_init__(self):
        _require(type(self.mode) is str and self.mode in ('paper', 'live'))
        _require(_scope(self.scope_digest) and _identifier(self.client_order_id))
        _require(type(self.symbol) is str and re.fullmatch(r'[A-Z][A-Z0-9.]{0,9}', self.symbol))
        _require(type(self.side) is str and self.side in ('buy', 'sell'))
        _require(type(self.quantity) is int and 0 < self.quantity <= 1_000_000)
        _require(type(self.limit_price) is Decimal and self.limit_price.is_finite()
                 and Decimal('0') < self.limit_price <= Decimal('1000000'))


_REPORTED = frozenset(('unknown', 'accepted', 'partially_filled', 'filled', 'canceled', 'rejected'))
_TERMINAL = frozenset(('filled', 'canceled', 'rejected'))


@dataclass(frozen=True)
class OrderRecord:
    intent: OrderIntent = field(repr=False)
    status: str = 'reserved'
    broker_order_id: str | None = field(default=None, repr=False)
    filled_quantity: int = 0

    def __post_init__(self):
        _require(type(self.intent) is OrderIntent)
        _require(type(self.status) is str and self.status in _REPORTED | {'reserved'})
        _require(self.broker_order_id is None or _identifier(self.broker_order_id))
        q, total = self.filled_quantity, self.intent.quantity
        _require(type(q) is int and 0 <= q <= total)
        if self.status in ('reserved', 'accepted', 'rejected'):
            _require(q == 0)
        if self.status == 'reserved':
            _require(self.broker_order_id is None)
        if self.status == 'partially_filled':
            _require(0 < q < total)
        if self.status == 'filled':
            _require(q == total)
        if self.status == 'canceled':
            _require(q < total)  # A cancel acknowledgment does not erase prior fills.
        if self.status in ('accepted', 'partially_filled', 'filled', 'canceled'):
            _require(self.broker_order_id is not None)


@dataclass(frozen=True)
class OrderBook:
    mode: str
    scope_digest: str = field(repr=False)
    records: tuple[OrderRecord, ...] = field(default=(), repr=False)

    def __post_init__(self):
        _require(type(self.mode) is str and self.mode in ('paper', 'live'))
        _require(_scope(self.scope_digest))
        _require(type(self.records) is tuple and len(self.records) <= 1024)
        keys, ids = set(), set()
        for record in self.records:
            _require(type(record) is OrderRecord)
            intent = record.intent
            _require((intent.mode, intent.scope_digest) == (self.mode, self.scope_digest))
            _require(intent.client_order_id not in keys)
            keys.add(intent.client_order_id)
            if record.broker_order_id is not None:
                _require(record.broker_order_id not in ids)
                ids.add(record.broker_order_id)

    @property
    def order_submission_approved(self):
        return False  # Even mode='live' is only an offline declaration.

    @property
    def reconciliation_required(self):
        return any(record.status == 'unknown' for record in self.records)


def remember_intent(book: OrderBook, intent: OrderIntent) -> tuple[OrderBook, bool]:
    """Same key+payload is a no-op in EVERY state, never a permission to retry."""
    _require(type(book) is OrderBook and type(intent) is OrderIntent)
    _require((intent.mode, intent.scope_digest) == (book.mode, book.scope_digest))
    for record in book.records:
        if record.intent.client_order_id == intent.client_order_id:
            _require(record.intent == intent)
            return book, False
    _require(not book.reconciliation_required and len(book.records) < 1024)
    return replace(book, records=book.records + (OrderRecord(intent),)), True


def apply_observation(book: OrderBook, client_order_id: str, *, status: str,
                      broker_order_id: str | None, filled_quantity: int) -> OrderBook:
    """Apply one caller-normalized snapshot; reject ambiguity, never repair it.

    No clock/sequence/authentication is invented. Out-of-order regression or
    contradictory terminal snapshots raise, preserving the prior immutable book.
    Unknown may be resolved only by a consistent explicit observation, not by
    remembering/retrying an intent. This function itself does not query a broker.
    """
    _require(type(book) is OrderBook and _identifier(client_order_id))
    _require(type(status) is str and status in _REPORTED)
    index = next((i for i, record in enumerate(book.records)
                  if record.intent.client_order_id == client_order_id), None)
    _require(index is not None)
    old = book.records[index]
    new = OrderRecord(old.intent, status, broker_order_id, filled_quantity)
    _require(new.filled_quantity >= old.filled_quantity)
    if old.broker_order_id is not None:
        _require(new.broker_order_id == old.broker_order_id)
    if old.status in _TERMINAL:
        _require(new == old)
    if old.status == 'partially_filled':
        _require(new.status in ('partially_filled', 'filled', 'canceled', 'unknown'))
    if new == old:
        return book
    records = book.records[:index] + (new,) + book.records[index + 1:]
    return replace(book, records=records)
