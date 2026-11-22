"""Synthetic broker snapshots only; never credentials, network, or dispatch."""
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal

import pytest

from astral_stock_agent.broker_order_state import (
    OrderBook, OrderIntent, OrderRecord, OrderStateError,
    apply_observation, remember_intent,
)


def intent(**changes):
    values = dict(mode='paper', scope_digest='a' * 64, client_order_id='synthetic-1',
                  symbol='SPY', side='buy', quantity=3, limit_price=Decimal('500.25'))
    return OrderIntent(**(values | changes))


def reserved(**changes):
    item = intent(**changes)
    return remember_intent(OrderBook(item.mode, item.scope_digest), item)[0]


def observe(book, status, quantity=0, broker_id='synthetic-broker-1', key='synthetic-1'):
    return apply_observation(book, key, status=status,
                             broker_order_id=broker_id, filled_quantity=quantity)


def test_partial_cancel_preserves_exposure_and_duplicate_intent_never_retries():
    book = reserved()
    states = [book, observe(book, 'accepted')]
    states += [observe(states[-1], 'partially_filled', 1)]
    states += [observe(states[-1], 'canceled', 1)]
    for current in states:
        replay, created = remember_intent(current, intent())
        assert replay is current and not created
        assert current.order_submission_approved is False
    assert states[-1].records[0].filled_quantity == 1
    assert states[0].records[0].status == 'reserved'
    with pytest.raises(FrozenInstanceError):
        states[-1].mode = 'live'


def test_unknown_keeps_key_and_known_fills_and_blocks_other_intents_until_observed():
    partial = observe(reserved(), 'partially_filled', 1)
    unknown = observe(partial, 'unknown', 1)
    assert unknown.reconciliation_required
    assert remember_intent(unknown, intent()) == (unknown, False)
    with pytest.raises(OrderStateError):
        remember_intent(unknown, intent(client_order_id='synthetic-2'))
    for status, qty in [('accepted', 0), ('rejected', 0), ('partially_filled', 0)]:
        with pytest.raises(OrderStateError):
            observe(unknown, status, qty)
    resolved = observe(unknown, 'filled', 3)
    assert not resolved.reconciliation_required
    assert not resolved.order_submission_approved
    assert unknown.records[0].status == 'unknown'


def test_lost_initial_response_can_bind_id_later_without_recreating_intent():
    unknown = observe(reserved(), 'unknown', broker_id=None)
    assert remember_intent(unknown, intent()) == (unknown, False)
    accepted = observe(unknown, 'accepted')
    assert accepted.records[0].broker_order_id == 'synthetic-broker-1'
    assert not accepted.order_submission_approved


@pytest.mark.parametrize('change', [
    {'symbol': 'QQQ'}, {'side': 'sell'}, {'quantity': 4},
    {'limit_price': Decimal('500.26')}, {'mode': 'live'}, {'scope_digest': 'b' * 64},
])
def test_same_client_key_cannot_change_economic_or_environment_binding(change):
    book = reserved()
    with pytest.raises(OrderStateError):
        remember_intent(book, intent(**change))
    assert book == reserved()


@pytest.mark.parametrize('terminal,qty', [('filled', 3), ('canceled', 1), ('rejected', 0)])
def test_terminal_snapshot_replay_only_and_old_state_survives_conflict(terminal, qty):
    book = observe(reserved(), terminal, qty)
    assert observe(book, terminal, qty) is book
    for status, quantity in [('unknown', qty), ('accepted', 0), ('filled', 3), ('canceled', 2)]:
        if (status, quantity) == (terminal, qty):
            continue
        with pytest.raises(OrderStateError):
            observe(book, status, quantity)
    assert book.records[0].status == terminal
    assert remember_intent(book, intent()) == (book, False)


@pytest.mark.parametrize('status,qty', [('partially_filled', 1), ('filled', 3), ('canceled', 2)])
def test_poll_may_skip_intermediate_snapshots(status, qty):
    book = observe(reserved(), status, qty)
    assert (book.records[0].status, book.records[0].filled_quantity) == (status, qty)


@pytest.mark.parametrize('status,qty', [('partially_filled', 1), ('accepted', 0), ('rejected', 0)])
def test_partial_cumulative_fills_cannot_regress(status, qty):
    book = observe(reserved(), 'partially_filled', 2)
    with pytest.raises(OrderStateError):
        observe(book, status, qty)
    assert book.records[0].filled_quantity == 2


@pytest.mark.parametrize('broker_id', [None, 'different-broker-id'])
def test_bound_broker_id_cannot_disappear_or_change(broker_id):
    book = observe(reserved(), 'accepted')
    with pytest.raises(OrderStateError):
        observe(book, 'unknown', broker_id=broker_id)


def test_broker_id_cannot_bind_two_client_intents():
    book = observe(reserved(), 'accepted')
    book, created = remember_intent(book, intent(client_order_id='synthetic-2'))
    assert created
    with pytest.raises(OrderStateError):
        observe(book, 'accepted', key='synthetic-2')
    assert book.records[1].status == 'reserved'


@pytest.mark.parametrize('change', [
    {'mode': 'LIVE'}, {'mode': []}, {'scope_digest': 'private-sentinel'},
    {'client_order_id': 'private-sentinel!'}, {'symbol': 'spy'}, {'symbol': []},
    {'side': 'short'}, {'quantity': True}, {'quantity': 0}, {'quantity': -1},
    {'quantity': 1.5}, {'quantity': 1_000_001}, {'limit_price': 500.25},
    {'limit_price': Decimal('NaN')}, {'limit_price': Decimal('sNaN')},
    {'limit_price': Decimal('Infinity')}, {'limit_price': Decimal('0')},
    {'limit_price': Decimal('-1')}, {'limit_price': Decimal('1000001')},
])
def test_invalid_intent_fixed_errors_do_not_echo_private_inputs(change):
    with pytest.raises(OrderStateError) as exc:
        intent(**change)
    assert str(exc.value) == 'order state requires reconciliation'


@pytest.mark.parametrize('status,qty,broker_id', [
    ('filled', 2, 'id'), ('partially_filled', 0, 'id'), ('partially_filled', 3, 'id'),
    ('canceled', 3, 'id'), ('rejected', 1, None), ('accepted', 1, 'id'),
    ('accepted', 0, None), ('filled', 3, None), ('unknown', True, None),
    ('unknown', -1, None), ('unknown', 4, None), ('unknown', 0, 'private-sentinel!'),
    ('reserved', 0, None), ('private-sentinel', 0, None), ([], 0, None),
])
def test_invalid_observation_leaves_prior_book_unchanged(status, qty, broker_id):
    book = reserved()
    with pytest.raises(OrderStateError) as exc:
        observe(book, status, qty, broker_id)
    assert str(exc.value) == 'order state requires reconciliation'
    assert book == reserved()


def test_unknown_key_and_foreign_scope_are_not_created_by_observation():
    with pytest.raises(OrderStateError):
        observe(reserved(), 'accepted', key='synthetic-absent')
    for changes in ({'mode': 'live'}, {'scope_digest': 'b' * 64}):
        with pytest.raises(OrderStateError):
            replace(OrderBook('paper', 'a' * 64), records=(OrderRecord(intent(**changes)),))


def test_bounded_unique_immutable_book_and_live_metadata_has_no_authority():
    records = tuple(OrderRecord(intent(client_order_id=f'synthetic-{n}')) for n in range(1024))
    book = OrderBook('paper', 'a' * 64, records)
    assert remember_intent(book, records[0].intent) == (book, False)
    with pytest.raises(OrderStateError):
        remember_intent(book, intent(client_order_id='synthetic-overflow'))
    for invalid in (records + (records[0],), (records[0], records[0]), list(records), (None,)):
        with pytest.raises(OrderStateError):
            replace(book, records=invalid)
    live = reserved(mode='live')
    assert not live.order_submission_approved
    assert 'synthetic-1' not in repr(live.records[0])
    assert 'synthetic-1' not in repr(live.records[0].intent)
    assert 'a' * 64 not in repr(live)


def test_wrong_public_api_types_fail_closed():
    for book, item in [(None, intent()), (reserved(), None)]:
        with pytest.raises(OrderStateError):
            remember_intent(book, item)
    with pytest.raises(OrderStateError):
        observe(None, 'accepted')
