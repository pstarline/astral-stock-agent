"""Reconcile independently sourced closing books and session accounting.

Inputs are the project's PRIVATE normalized evidence contract, not a new
Astral API response. Missing fee/capital-flow/P&L evidence must remain missing.
This module cannot convert activity observations into authoritative books.
"""

import copy
from datetime import date, datetime, timezone
from decimal import Context, Decimal, DecimalException, Inexact, localcontext

from .report import InvalidSnapshot, money, render, symbol, text, timestamp
from .sessions import SessionError, latest_closed_session, require_closed_session


class ReconciliationError(ValueError):
    pass


SCOPE = ("account_ref", "deployment_ref", "strategy_ref", "run_ref")
CENT = Decimal("0.01")


def _equal(left, right):
    if abs(left - right) > CENT:
        raise ReconciliationError("accounting evidence disagrees")


def _holdings(snapshot, verified):
    result = {}
    for row in snapshot["positions"]:
        ticker = symbol(row["symbol"])
        if ticker not in verified:
            raise ReconciliationError("position outside verified US universe")
        result[ticker] = money(row["quantity"], nonnegative=True)
    return result


def reconcile_daily(previous, current, accounting, *, verified_us_symbols, now=None):
    """Return a report snapshot only after cash, shares and P&L agree.

    previous/current use report schema v1 and exact scheduled close marks.
    accounting declares the SAME scope and previous-close/current-close period,
    complete signed net capital flow, net other income, execution fees, and a
    net-of-execution-fees P&L basis. Zero must be an explicit verified value.
    Splits, transfers of securities, cross-day orders and missing beginning
    books need additional source evidence; they are not silently repaired.
    """
    now = now or datetime.now(timezone.utc)
    try:
        # Keep fees, cash and exact share counts independent of caller context.
        # Do not silently round extreme exponent spreads into matching books.
        with localcontext(Context(prec=96)) as context:
            context.traps[Inexact] = True
            return _reconcile(previous, current, accounting, set(verified_us_symbols), now)
    except (InvalidSnapshot, SessionError, KeyError, TypeError, AttributeError, ValueError, DecimalException):
        raise ReconciliationError("daily reconciliation blocked: missing or inconsistent evidence") from None


def _reconcile(previous, current, accounting, verified, now):
    # Validate historical baseline at its recorded capture time, not as if it
    # were fetched fresh today. Persistence/authenticity is the collector's job.
    render(previous, now=timestamp(previous["collected_at"]))
    render(current, now=now)
    if previous["source"] != current["source"] or accounting["source"] != current["source"]:
        raise ReconciliationError("mixed fixture and operational evidence")
    scope = tuple(text(current[key]) for key in SCOPE)
    if tuple(text(previous[key]) for key in SCOPE) != scope:
        raise ReconciliationError("checkpoint scope changed")
    if tuple(text(accounting[key]) for key in SCOPE) != scope:
        raise ReconciliationError("accounting scope mismatch")
    if (accounting["kind"] != "session_accounting" or accounting["complete"] is not True
            or accounting["mode"] != "astral_paper" or accounting["currency"] != "USD"
            or accounting["pnl_basis"] != "net_of_execution_fees"):
        raise ReconciliationError("complete net-basis paper accounting required")
    session = require_closed_session(date.fromisoformat(current["trading_day"]), now)
    prior = latest_closed_session(session.opens_at)
    if previous["trading_day"] != prior.day.isoformat():
        raise ReconciliationError("previous trading-day baseline missing")
    if (timestamp(accounting["start_at"]) != prior.closes_at
            or timestamp(accounting["end_at"]) != session.closes_at):
        raise ReconciliationError("accounting period mismatch")
    for book, closed in ((previous, prior.closes_at), (current, session.closes_at)):
        if timestamp(book["valuation_at"]) != closed:
            raise ReconciliationError("exact closing book required")
    received = timestamp(accounting["collected_at"])
    if received < session.closes_at or abs((now - received).total_seconds()) > 900 or received > now:
        raise ReconciliationError("stale accounting capture")

    # Missing values (including explicit null) fail through money(); no default 0.
    capital = money(accounting["net_capital_flow_usd"])
    income = money(accounting["net_other_income_usd"])
    fees = money(accounting["execution_fees_usd"], nonnegative=True)
    holdings = _holdings(previous, verified)
    expected_cash = money(previous["cash_usd"], nonnegative=True) + capital + income - fees
    for fill in sorted(current["fills"], key=lambda row: timestamp(row["executed_at"])):
        order = next(order for order in current["orders"] if order["order_ref"] == fill["order_ref"])
        ticker = symbol(order["symbol"])
        if ticker not in verified:
            raise ReconciliationError("execution outside verified US universe")
        qty, price = money(fill["quantity"], nonnegative=True), money(fill["price_usd"], nonnegative=True)
        direction = Decimal(1) if order["side"] == "buy" else Decimal(-1)
        holdings[ticker] = holdings.get(ticker, Decimal(0)) + direction * qty
        expected_cash -= direction * qty * price
        if holdings[ticker] < 0:
            raise ReconciliationError("short or missing starting position")
    ending = _holdings(current, verified)
    # Share counts must match exactly, not within a dollar/cent tolerance.
    for ticker in holdings.keys() | ending.keys():
        if holdings.get(ticker, Decimal(0)) != ending.get(ticker, Decimal(0)):
            raise ReconciliationError("unexplained position movement")
    _equal(expected_cash, money(current["cash_usd"], nonnegative=True))
    daily_total = money(current["equity_usd"]) - money(previous["equity_usd"]) - capital
    realized = money(current["cumulative_realized_pnl_usd"]) - money(previous["cumulative_realized_pnl_usd"])
    unrealized = money(current["unrealized_pnl_usd"]) - money(previous["unrealized_pnl_usd"])
    _equal(daily_total, realized + unrealized + income)
    # A supplied manual daily figure must agree; do not silently overwrite it.
    for field, computed in (("daily_realized_pnl_usd", realized),
                            ("daily_unrealized_change_usd", unrealized),
                            ("daily_total_pnl_usd", daily_total)):
        if current.get(field) is not None:
            _equal(money(current[field]), computed)
    snapshot = copy.deepcopy(current)
    snapshot.update(daily_realized_pnl_usd=str(realized), daily_unrealized_change_usd=str(unrealized),
                    daily_total_pnl_usd=str(daily_total), daily_other_income_usd=str(income),
                    daily_execution_fees_usd=str(fees), daily_net_capital_flow_usd=str(capital))
    # Only the copied report fields change. Never edit trading/closing books.
    render(snapshot, now=now)
    return snapshot
