"""Synthetic pages only: no accounts, providers, credentials or runtime writes."""
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal

import pytest

from astral_stock_agent.broker_observation_pages import (
    ObservationPage, ObservationPagesError, OrderObservation, apply_observation_pages,
)
from astral_stock_agent.broker_order_state import OrderBook, OrderIntent, OrderRecord


SCOPE, QUERY = 'a' * 64, 'b' * 64


def book(mode='paper'):
    return OrderBook(mode, SCOPE, tuple(
        OrderRecord(OrderIntent(mode, SCOPE, key, 'SPY', 'buy', 3, Decimal('10')),
                    'unknown') for key in ('first', 'second')
    ))


def page(rows=(), request=None, next_cursor=None, mode='paper'):
    return ObservationPage(mode, SCOPE, QUERY, request, next_cursor, rows)


def apply(original, pages):
    return apply_observation_pages(original, query_digest=QUERY, pages=pages)


def observation(key='first', status='accepted', broker='broker1', quantity=0):
    return OrderObservation(key, status, broker, quantity)


@pytest.mark.parametrize('mode', ['paper', 'live'])
def test_complete_chain_applies_partial_fill_and_cancel_without_mutation(mode):
    original = book(mode)
    pages = (page((observation(status='partially_filled', quantity=1),),
                  next_cursor='page2', mode=mode),
             page((observation('second', 'canceled', 'broker2', 2),),
                  request='page2', mode=mode))
    result = apply(original, pages)
    assert [r.status for r in original.records] == ['unknown', 'unknown']
    assert [(r.status, r.filled_quantity) for r in result.records] == [
        ('partially_filled', 1), ('canceled', 2)]
    assert result.order_submission_approved is False
    assert apply(result, pages) is result  # Exact replay is no authority to submit.
    with pytest.raises(FrozenInstanceError):
        pages[0].next_cursor = None


def test_empty_and_missing_rows_never_resolve_unknown_or_invent_absence():
    original = book()
    assert apply(original, (page(),)) is original
    result = apply(original, (page((observation(),)),))
    assert result.records[1] is original.records[1]
    assert result.reconciliation_required is True
    assert result.order_submission_approved is False
    empty = OrderBook('paper', SCOPE)
    assert apply(empty, (page(),)) is empty
    assert empty.order_submission_approved is False


@pytest.mark.parametrize('pages', [
    (), [], (None,), (page(request='later'),),
    (page(next_cursor='remaining'),),  # Page/depth limit is not completion.
    (page(), page()),  # Nothing may follow an explicit terminal page.
    (page(next_cursor='p2'), page(request='wrong')),
    (page(next_cursor='p2'), page(request='p2', next_cursor='p2'),
     page(request='p2')),
    (page(next_cursor='p2'), None),  # Failure on page 2 cannot publish page 1.
    (page(next_cursor='p2'), replace(page(request='p2'), query_digest='c' * 64)),
    (replace(page(), scope_digest='c' * 64),),
    (replace(page(), mode='live'),),
    (replace(page(), mode=True),),
    (replace(page(), query_digest='bad'),),
    (replace(page(), observations=[]),),
    (page((object(),)),),
    (page(next_cursor='private\nvalue'), page(request='private\nvalue')),
    (page(next_cursor='x' * 129), page(request='x' * 129)),
    (page((observation(), observation())),),
    (page((observation(),), next_cursor='p2'),
     page((observation(),), request='p2')),
])
def test_invalid_incomplete_or_overlapping_chain_preserves_original(pages):
    original = book()
    before = original.records
    with pytest.raises(ObservationPagesError, match='^observation pages require reconciliation$'):
        apply(original, pages)
    assert original.records is before
    assert all(r.status == 'unknown' for r in before)


@pytest.mark.parametrize('bad', [
    observation('untracked'), observation(status='reserved'),
    observation(status='filled', quantity=2), observation(quantity=True),
    observation(status='rejected', quantity=1), observation(broker=None),
    observation(key='private\nvalue'), observation(broker='private\nvalue'),
])
def test_invalid_second_row_cannot_publish_valid_first_row(bad):
    original = book()
    # Put the malformed observation on a distinct key where possible.
    if bad.client_order_id == 'first':
        bad = replace(bad, client_order_id='second')
    with pytest.raises(ObservationPagesError) as exc:
        apply(original, (page((observation(),)), page((bad,))))
    assert str(exc.value) == 'observation pages require reconciliation'
    # Above also rejects an early terminal chain. Exercise reducer staging itself.
    with pytest.raises(ObservationPagesError):
        apply(original, (page((observation(),), next_cursor='p2'),
                         page((bad,), request='p2')))
    assert all(r.status == 'unknown' for r in original.records)


@pytest.mark.parametrize('later', [
    observation(status='partially_filled', quantity=1),  # Cumulative fill regresses.
    observation(status='filled', broker='changed', quantity=3),
    observation(status='accepted'),
])
def test_conflicting_late_snapshot_preserves_prior_fills(later):
    original = book()
    initial = apply(original, (page((observation(status='filled', quantity=3),)),))
    with pytest.raises(ObservationPagesError):
        apply(initial, (page((later,)),))
    assert initial.records[0].status == 'filled'
    assert initial.records[0].filled_quantity == 3


def test_page_and_row_bounds_include_exact_boundary():
    original = book()
    pages = tuple(page(request=None if i == 0 else f'p{i}',
                       next_cursor=None if i == 15 else f'p{i + 1}') for i in range(16))
    assert apply(original, pages) is original
    with pytest.raises(ObservationPagesError):
        apply(original, pages + (page(),))
    intents = tuple(OrderIntent('paper', SCOPE, f'k{i}', 'SPY', 'buy', 1, Decimal('1'))
                    for i in range(1024))
    full = OrderBook('paper', SCOPE, tuple(OrderRecord(i, 'unknown') for i in intents))
    rows = tuple(observation(i.client_order_id, broker=f'b{n}')
                 for n, i in enumerate(intents))
    assert len(apply(full, (page(rows),)).records) == 1024
    with pytest.raises(ObservationPagesError):
        apply(full, (page(rows + (observation('overflow'),)),))


def test_private_repr_and_error_and_no_query_or_book_coercion():
    sensitive = observation('private-marker', broker='private-marker')
    declared = page((sensitive,), next_cursor='private-marker')
    assert 'private-marker' not in repr(declared) + repr(sensitive)
    for original, query in [(None, QUERY), (book(), False), (book(), 'private-marker')]:
        with pytest.raises(ObservationPagesError) as exc:
            apply_observation_pages(original, query_digest=query, pages=(declared,))
        assert str(exc.value) == 'observation pages require reconciliation'
