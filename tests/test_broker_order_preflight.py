"""Declared synthetic limits, never operating capital, quotes, or permission."""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Context, Decimal, localcontext
from zoneinfo import ZoneInfo

import pytest

from astral_stock_agent.broker_order_state import OrderBook, OrderIntent, OrderRecord
from astral_stock_agent.broker_order_preflight import (
    RiskLimits, PreflightContext, assess_order_preflight, latch_emergency_stop,
)


NOW = datetime(2026, 11, 20, 16, tzinfo=timezone.utc)
DAY = date(2026, 11, 20)


def intent(**changes):
    return OrderIntent(**(dict(mode='paper', scope_digest='a' * 64, client_order_id='new',
        symbol='SPY', side='buy', quantity=1, limit_price=Decimal('100')) | changes))


def context(**changes):
    return PreflightContext(**(dict(mode='paper', scope_digest='a' * 64, symbol='SPY',
        now=NOW, quote_at=NOW, account_at=NOW, session_day=DAY, baseline_day=DAY,
        quote_price=Decimal('100'), held_shares=0, day_start_equity=Decimal('10000'),
        current_equity=Decimal('10000'), emergency_stop=False, operating_approved=True,
        account_reconciled=True, cashflows_reconciled=True, regular_session_verified=True,
        daily_loss_stop=False) | changes))


def limits(**changes):
    return RiskLimits(**(dict(max_daily_loss=Decimal('50'), max_symbol_shares=3,
        max_symbol_notional=Decimal('300'), max_quote_age=timedelta(seconds=10),
        max_account_age=timedelta(seconds=20)) | changes))


def run(*, records=(), order=None, ctx=None, risk=None):
    return assess_order_preflight(OrderBook('paper', 'a' * 64, records),
        order or intent(), ctx or context(), risk or limits())


def test_all_declared_checks_pass_but_never_authorize_order_even_in_live_mode():
    c, item = context(), intent()
    assert run(ctx=c, order=item).checks_passed
    assert not run().order_submission_approved
    result = assess_order_preflight(OrderBook('live', 'a' * 64),
        replace(item, mode='live'), replace(c, mode='live'), limits())
    assert result.checks_passed and result.order_submission_approved is False
    assert c == context() and item == intent()


def test_default_controls_and_missing_limits_block_and_stop_cannot_auto_clear():
    c = context()
    fields = {name: value for name, value in vars(c).items() if name not in (
        'emergency_stop', 'operating_approved', 'account_reconciled', 'cashflows_reconciled',
        'regular_session_verified', 'daily_loss_stop')}
    default = PreflightContext(**fields)
    assert set(run(ctx=default).reasons) == {'emergency_stop', 'approval_missing',
        'account_unreconciled', 'cashflows_unreconciled', 'session_unverified', 'daily_loss_stop'}
    assert assess_order_preflight(OrderBook('paper', 'a'*64), intent(), c).reasons == ('limits_unset',)
    stopped = latch_emergency_stop(c)
    assert run(ctx=stopped).reasons == ('emergency_stop',)
    assert latch_emergency_stop(stopped) == stopped
    assert run(ctx=stopped).reasons == ('emergency_stop',)  # Assessment never clears it.
    assert not c.emergency_stop


@pytest.mark.parametrize('field,value,reason', [
    ('emergency_stop', True, 'emergency_stop'), ('operating_approved', False, 'approval_missing'),
    ('account_reconciled', False, 'account_unreconciled'),
    ('cashflows_reconciled', False, 'cashflows_unreconciled'),
    ('regular_session_verified', False, 'session_unverified'), ('daily_loss_stop', True, 'daily_loss_stop'),
    ('baseline_day', date(2026,11,19), 'baseline_session_mismatch'),
    ('current_equity', Decimal('9950'), 'daily_loss_limit'),
    ('current_equity', Decimal('9949'), 'daily_loss_limit'),
])
def test_each_declared_safety_failure_blocks(field, value, reason):
    assert run(ctx=context(**{field:value})).reasons == (reason,)


def test_equity_recovery_does_not_clear_declared_daily_loss_stop():
    assert run(ctx=context(current_equity=Decimal('11000'), daily_loss_stop=True)).reasons == ('daily_loss_stop',)
    assert run(ctx=context(current_equity=Decimal('9950.01'))).checks_passed


@pytest.mark.parametrize('field,threshold,reason', [
    ('quote_at', 10, 'quote_unusable'), ('account_at', 20, 'account_stale'),
])
def test_exact_utc_age_boundary_future_and_expiry(field, threshold, reason):
    assert run(ctx=context(**{field: NOW-timedelta(seconds=threshold)})).checks_passed
    for stamp in (NOW-timedelta(seconds=threshold, microseconds=1), NOW+timedelta(microseconds=1)):
        assert run(ctx=context(**{field:stamp})).reasons == (reason,)
    offset = timezone(timedelta(hours=9))
    assert run(ctx=context(**{field:NOW.astimezone(offset)})).checks_passed


def test_dst_fold_uses_elapsed_utc_instead_of_same_zone_wall_time():
    zone = ZoneInfo('America/New_York')
    earlier = datetime(2026,11,1,1,30,tzinfo=zone,fold=0)
    later = datetime(2026,11,1,1,30,tzinfo=zone,fold=1)
    result = run(ctx=context(now=later, quote_at=earlier, account_at=later))
    assert result.reasons == ('quote_unusable',)


def record(side='buy', status='accepted', quantity=2, filled=0, key='old'):
    return OrderRecord(intent(side=side, quantity=quantity, client_order_id=key),
                       status, 'synthetic-broker-'+key, filled)


def test_all_pending_buy_remainders_count_and_pending_sell_does_not_free_capacity():
    assert run(records=(record(),), ctx=context(held_shares=1)).reasons == ('position_limit','notional_limit')
    partial = record(status='partially_filled', filled=1)
    assert run(records=(partial,), ctx=context(held_shares=1)).checks_passed
    sell = record(side='sell', quantity=1)
    assert run(records=(sell,), ctx=context(held_shares=3)).reasons == ('position_limit','notional_limit')


def test_pending_sell_reserves_held_and_partial_cancel_preserves_current_position():
    sell = record(side='sell', status='partially_filled', filled=1)
    assert run(records=(sell,), ctx=context(held_shares=1), order=intent(side='sell')).reasons == ('unavailable_held_shares',)
    canceled = replace(sell, status='canceled')
    assert run(records=(canceled,), ctx=context(held_shares=1), order=intent(side='sell')).checks_passed
    # Filled holdings come from the separately reconciled snapshot, not from cancel status.
    assert run(records=(canceled,), ctx=context(held_shares=3)).reasons == ('position_limit','notional_limit')


@pytest.mark.parametrize('status,filled', [('reserved',0),('accepted',0),('partially_filled',1),
    ('filled',2),('canceled',1),('rejected',0),('unknown',0)])
def test_recorded_key_never_becomes_another_submission(status, filled):
    item = intent(quantity=2)
    r = OrderRecord(item,status,None if status in ('reserved','rejected','unknown') else 'id',filled)
    result = run(records=(r,))
    assert 'intent_already_recorded' in result.reasons
    assert not result.checks_passed and not result.order_submission_approved


def test_unknown_other_symbol_also_blocks_account_and_new_buy_uses_higher_price():
    unknown = OrderRecord(intent(symbol='QQQ',client_order_id='other'),'unknown')
    assert run(records=(unknown,)).reasons == ('order_unknown',)
    assert run(order=intent(limit_price=Decimal('301'))).reasons == ('notional_limit',)
    pending = replace(record(quantity=1), intent=intent(client_order_id='old',limit_price=Decimal('250')))
    assert run(records=(pending,)).reasons == ('notional_limit',)
    assert run(order=intent(quantity=3)).checks_passed  # Exact declared cap inclusive.
    assert run(order=intent(side='sell')).reasons == ('unavailable_held_shares',)


@pytest.mark.parametrize('changes', [
    {'mode':'live'},{'scope_digest':'b'*64},{'symbol':'QQQ'}, {'mode':[]},
])
def test_context_scope_must_match_book_and_intent(changes):
    assert run(ctx=context(**changes)).reasons == ('scope_mismatch',)


@pytest.mark.parametrize('changes', [
    {'held_shares':True},{'held_shares':-1},{'quote_price':Decimal('NaN')},
    {'quote_price':Decimal('sNaN')},{'quote_price':Decimal('Infinity')},{'quote_price':Decimal('0')},
    {'quote_price':100.0},{'current_equity':Decimal('-1')},{'current_equity':Decimal('NaN')},
    {'day_start_equity':Decimal('0')},{'day_start_equity':Decimal('1e13')},
    {'now':NOW.replace(tzinfo=None)},{'quote_at':None},{'account_at':None},
    {'session_day':NOW},{'baseline_day':None}, {'operating_approved':1}, {'daily_loss_stop':0},
])
def test_invalid_context_fails_closed_without_input_echo(changes):
    assert run(ctx=context(**changes)).reasons == ('invalid_input',)


@pytest.mark.parametrize('changes', [
    {'max_daily_loss':Decimal('0')},{'max_daily_loss':Decimal('NaN')},
    {'max_symbol_notional':Decimal('-1')},{'max_symbol_shares':True},
    {'max_quote_age':timedelta(0)},{'max_account_age':timedelta(days=2)},
])
def test_invalid_limits_are_never_invented_or_relaxed(changes):
    assert run(risk=limits(**changes)).reasons == ('invalid_input',)


def test_hostile_decimal_context_cannot_change_diagnostic_and_invalid_types_block():
    c=context(current_equity=Decimal('9950.01'))
    expected=run(ctx=c)
    hostile=Context(prec=2,Emin=-1,Emax=1)
    for key in hostile.traps: hostile.traps[key]=True
    with localcontext(hostile):
        assert run(ctx=c)==expected
    assert assess_order_preflight(None,intent(),context(),limits()).reasons==('invalid_input',)
    with pytest.raises(ValueError,match='preflight input invalid'):
        latch_emergency_stop(None)
