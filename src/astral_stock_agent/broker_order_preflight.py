"""Offline safety diagnostics only. No broker, I/O, live authorization or defaults
for money/limits. Caller declarations are not verified account/market facts.
Passing checks NEVER authorizes submission. Persisted approval, reconciliation,
stop enforcement and an adapter must be implemented before operational use.
"""
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Context, Decimal, localcontext

from .broker_order_state import OrderBook, OrderIntent


@dataclass(frozen=True)
class RiskLimits:
    max_daily_loss: Decimal
    max_symbol_shares: int
    max_symbol_notional: Decimal
    max_quote_age: timedelta
    max_account_age: timedelta


@dataclass(frozen=True, kw_only=True)
class PreflightContext:
    mode: str
    scope_digest: str = field(repr=False)
    symbol: str
    now: datetime
    quote_at: datetime
    account_at: datetime
    session_day: date
    baseline_day: date
    quote_price: Decimal = field(repr=False)
    held_shares: int
    day_start_equity: Decimal = field(repr=False)
    current_equity: Decimal = field(repr=False)
    emergency_stop: bool = True
    operating_approved: bool = False
    account_reconciled: bool = False
    cashflows_reconciled: bool = False
    regular_session_verified: bool = False
    daily_loss_stop: bool = True


@dataclass(frozen=True)
class PreflightResult:
    reasons: tuple[str, ...]

    @property
    def checks_passed(self):
        return not self.reasons

    @property
    def order_submission_approved(self):
        return False


def latch_emergency_stop(context: PreflightContext) -> PreflightContext:
    """Only closes the declared gate. No auto-clear or remote cancellation."""
    if type(context) is not PreflightContext:
        raise ValueError('preflight input invalid')
    return replace(context, emergency_stop=True)


def _money(value, *, positive=False):
    return (type(value) is Decimal and value.is_finite()
            and (value > 0 if positive else value >= 0) and value <= Decimal('1e12'))


def _shares(value, *, positive=False):
    return type(value) is int and (value > 0 if positive else value >= 0) and value <= 1_000_000


def _age_limit(value):
    return type(value) is timedelta and timedelta(0) < value <= timedelta(days=1)


def _aware(value):
    return type(value) is datetime and value.tzinfo is not None and value.utcoffset() is not None


def assess_order_preflight(book: OrderBook, intent: OrderIntent,
                           context: PreflightContext, limits: RiskLimits | None = None) -> PreflightResult:
    """Conservative single-symbol long-only *declared* exposure diagnostics.

    All open buy remainders reserve shares/notional; sells never free buy capacity.
    Open sell remainders reserve held shares. Cancel acknowledgments release only
    their unfilled remainder. Notional uses max(quote, limit) for pending/new buys.
    No cash, fees, FX, portfolio-wide exposure, execution price or credit model.
    Net equity loss uses the supplied same-session baseline; a separate declared
    daily_loss_stop remains blocking after recovery. No daily reset is performed.
    """
    if limits is None:
        return PreflightResult(('limits_unset',))
    try:
        if (type(book) is not OrderBook or type(intent) is not OrderIntent
                or type(context) is not PreflightContext or type(limits) is not RiskLimits):
            return PreflightResult(('invalid_input',))
        c = context
        flags = (c.emergency_stop, c.operating_approved, c.account_reconciled,
                 c.cashflows_reconciled, c.regular_session_verified, c.daily_loss_stop)
        if (any(type(x) is not bool for x in flags)
                or not all(_aware(x) for x in (c.now, c.quote_at, c.account_at))
                or type(c.session_day) is not date or type(c.baseline_day) is not date
                or not _money(c.quote_price, positive=True) or not _shares(c.held_shares)
                or not _money(c.day_start_equity, positive=True) or not _money(c.current_equity)
                or not _money(limits.max_daily_loss, positive=True)
                or not _money(limits.max_symbol_notional, positive=True)
                or not _shares(limits.max_symbol_shares, positive=True)
                or not _age_limit(limits.max_quote_age) or not _age_limit(limits.max_account_age)):
            return PreflightResult(('invalid_input',))
        reasons = []
        if (type(c.mode) is not str or type(c.scope_digest) is not str
                or (book.mode, book.scope_digest) != (intent.mode, intent.scope_digest)
                or (c.mode, c.scope_digest) != (intent.mode, intent.scope_digest)
                or type(c.symbol) is not str or c.symbol != intent.symbol):
            return PreflightResult(('scope_mismatch',))
        for failed, reason in (
            (c.emergency_stop, 'emergency_stop'), (not c.operating_approved, 'approval_missing'),
            (not c.account_reconciled, 'account_unreconciled'),
            (not c.cashflows_reconciled, 'cashflows_unreconciled'),
            (not c.regular_session_verified, 'session_unverified'),
            (c.daily_loss_stop, 'daily_loss_stop'),
            (c.session_day != c.baseline_day, 'baseline_session_mismatch'),
            (book.reconciliation_required, 'order_unknown'),
            (any(r.intent.client_order_id == intent.client_order_id for r in book.records), 'intent_already_recorded'),
            (not timedelta(0) <= c.now.astimezone(timezone.utc) - c.quote_at.astimezone(timezone.utc)
             <= limits.max_quote_age, 'quote_unusable'),
            (not timedelta(0) <= c.now.astimezone(timezone.utc) - c.account_at.astimezone(timezone.utc)
             <= limits.max_account_age, 'account_stale'),
        ):
            if failed:
                reasons.append(reason)
        with localcontext(Context(prec=50)):
            if c.day_start_equity - c.current_equity >= limits.max_daily_loss:
                reasons.append('daily_loss_limit')
            open_orders = [r for r in book.records if r.intent.symbol == intent.symbol
                           and r.status not in ('filled', 'canceled', 'rejected')]
            buys = [r for r in open_orders if r.intent.side == 'buy']
            sells = [r for r in open_orders if r.intent.side == 'sell']
            pending_buys = sum(r.intent.quantity - r.filled_quantity for r in buys)
            pending_sells = sum(r.intent.quantity - r.filled_quantity for r in sells)
            new_buys = intent.quantity if intent.side == 'buy' else 0
            if c.held_shares + pending_buys + new_buys > limits.max_symbol_shares:
                reasons.append('position_limit')
            notional = c.quote_price * c.held_shares + sum(
                max(c.quote_price, r.intent.limit_price) * (r.intent.quantity - r.filled_quantity)
                for r in buys) + max(c.quote_price, intent.limit_price) * new_buys
            if notional > limits.max_symbol_notional:
                reasons.append('notional_limit')
            new_sells = intent.quantity if intent.side == 'sell' else 0
            if pending_sells + new_sells > c.held_shares:
                reasons.append('unavailable_held_shares')
        return PreflightResult(tuple(reasons))
    except (ValueError, TypeError, OverflowError):
        return PreflightResult(('invalid_input',))
