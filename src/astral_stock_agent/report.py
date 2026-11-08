"""Strict private snapshot contract, not an invented Astral REST response.

A trusted MCP collector must reconcile and normalize official tool responses.
The source label is metadata, not proof of authenticity. Never accept public PR
data as an operational snapshot.
"""

import hashlib
import json
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo


class InvalidSnapshot(ValueError):
    pass


def text(value, maximum=120):
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise InvalidSnapshot("invalid text field")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise InvalidSnapshot("control character in text")
    return value


def money(value, *, nonnegative=False):
    # Decimal strings avoid binary float rounding and JSON NaN/Infinity.
    if not isinstance(value, str) or len(value) > 40:
        raise InvalidSnapshot("amount must be a decimal string")
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise InvalidSnapshot("invalid amount") from None
    if not number.is_finite() or abs(number) > Decimal("1e15"):
        raise InvalidSnapshot("nonfinite or excessive amount")
    if nonnegative and number < 0:
        raise InvalidSnapshot("negative amount")
    return number


def timestamp(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError):
        raise InvalidSnapshot("invalid timestamp") from None
    if result.tzinfo is None:
        raise InvalidSnapshot("timezone required")
    return result


def symbol(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", value):
        raise InvalidSnapshot("invalid symbol")
    return value


def rows(snapshot, field):
    result = snapshot[field]
    if not isinstance(result, list) or len(result) > 100:
        raise InvalidSnapshot("invalid or excessive rows")
    if any(not isinstance(row, dict) for row in result):
        raise InvalidSnapshot("invalid row")
    return result


def render(snapshot, *, now=None):
    """Validate before rendering. Unknown P&L remains explicitly unknown.

    Collector must provide a completed US session from an exchange calendar.
    Weekday validation alone does NOT establish an exchange trading session.
    All orders/fills are this trading day's records, positions are end-of-day.
    """
    now = now or datetime.now(timezone.utc)
    try:
        return _render(snapshot, now)
    except (KeyError, TypeError, AttributeError, InvalidOperation):
        raise InvalidSnapshot("missing or invalid snapshot fields") from None


def _render(s, now):
    if not isinstance(s, dict) or type(s.get("schema_version")) is not int or s["schema_version"] != 1:
        raise InvalidSnapshot("unsupported schema")
    if s["mode"] != "astral_paper" or s["asset_class"] != "us_equity" or s["currency"] != "USD":
        raise InvalidSnapshot("only USD US-equity paper snapshots accepted")
    if s["source"] not in {"astral_mcp", "fixture"}:
        raise InvalidSnapshot("unknown source")
    if s["complete"] is not True or s["session_verified"] is not True:
        raise InvalidSnapshot("incomplete or unverified session")
    if now.tzinfo is None:
        raise InvalidSnapshot("timezone required")
    collected = timestamp(s["collected_at"])
    if not timedelta(0) <= now - collected <= timedelta(minutes=15):
        raise InvalidSnapshot("stale or future collection")
    try:
        day = date.fromisoformat(s["trading_day"])
    except (ValueError, TypeError):
        raise InvalidSnapshot("invalid trading day") from None
    if day.weekday() >= 5 or day > now.astimezone(ZoneInfo("America/New_York")).date():
        raise InvalidSnapshot("invalid trading day")
    closed = timestamp(s["session_closed_at"])
    if closed.astimezone(ZoneInfo("America/New_York")).date() != day or closed > collected:
        raise InvalidSnapshot("session not closed")
    valuation = timestamp(s["valuation_at"])
    if not closed - timedelta(minutes=30) <= valuation <= collected:
        raise InvalidSnapshot("stale valuation")
    # Scope is explicit so an unrelated BTC deployment cannot become a US report.
    scope = [text(s[k]) for k in ("account_ref", "deployment_ref", "strategy_ref")]
    name = text(s["strategy_name"])
    equity, cash = (money(s[k], nonnegative=True) for k in ("equity_usd", "cash_usd"))
    lines = ["[Astral 미국 주식 · 모의매매]", f"거래일: {day} (미국 동부)",
             f"보고 시각: {now.astimezone(ZoneInfo('Asia/Seoul')).isoformat(timespec='seconds')}",
             f"전략: {name}", f"계좌 평가액: ${equity:,.2f} / 가용 현금: ${cash:,.2f}"]
    if s["source"] == "fixture":
        lines.insert(0, "[검증용 예제 · 실제 거래 기록 아님]")
    for key, label in (("daily_realized_pnl_usd", "당일 실현 손익"),
                       ("daily_unrealized_change_usd", "당일 미실현 손익 변화"),
                       ("cumulative_realized_pnl_usd", "누적 실현 손익"),
                       ("unrealized_pnl_usd", "현재 미실현 손익")):
        value = s[key]
        lines.append(f"{label}: " + ("미확인" if value is None else f"${money(value):,.2f}"))
    positions = rows(s, "positions")
    seen = set()
    position_value = Decimal(0)
    lines.append("보유 종목:" if positions else "보유 종목: 없음")
    for p in positions:
        ticker = symbol(p["symbol"])
        if ticker in seen:
            raise InvalidSnapshot("duplicate position")
        seen.add(ticker)
        quantity = money(p["quantity"], nonnegative=True)
        value = money(p["market_value_usd"], nonnegative=True)
        position_value += value
        lines.append(f"  {ticker} {quantity}주 / ${value:,.2f}")
    if abs(equity - cash - position_value) > Decimal("0.01"):
        raise InvalidSnapshot("cash and position values do not reconcile to equity")
    orders = rows(s, "orders")
    fills = rows(s, "fills")
    ids = set()
    by_order = {}
    lines.append("당일 주문:" if orders else "당일 주문: 없음")
    for order in orders:
        oid = text(order["order_ref"])
        if oid in ids:
            raise InvalidSnapshot("duplicate order")
        ids.add(oid)
        status = order["status"]
        if status not in {"pending", "accepted", "partially_filled", "filled", "cancelled", "rejected", "unknown"}:
            raise InvalidSnapshot("unknown order status")
        if order["side"] not in {"buy", "sell"}:
            raise InvalidSnapshot("invalid side")
        ticker = symbol(order["symbol"])
        requested = money(order["quantity"], nonnegative=True)
        filled = money(order["filled_quantity"], nonnegative=True)
        if requested <= 0 or filled > requested:
            raise InvalidSnapshot("invalid fill quantity")
        if (status == "filled" and filled != requested) or (status == "partially_filled" and not 0 < filled < requested):
            raise InvalidSnapshot("status contradicts quantity")
        if status in {"pending", "accepted", "rejected"} and filled != 0:
            raise InvalidSnapshot("unfilled status has fills")
        by_order[oid] = (order, filled, Decimal(0))
        lines.append(f"  {ticker} {order['side']} {status} / 요청 {requested}주 · 체결 {filled}주")
    lines.append("당일 체결:" if fills else "당일 체결: 없음 (조회 범위 내)")
    seen = set()
    for fill in fills:
        fid = text(fill["fill_ref"])
        oid = text(fill["order_ref"])
        if fid in seen or oid not in by_order:
            raise InvalidSnapshot("duplicate or orphan fill")
        seen.add(fid)
        order, declared, total = by_order[oid]
        qty = money(fill["quantity"], nonnegative=True)
        price = money(fill["price_usd"], nonnegative=True)
        if qty <= 0 or price <= 0:
            raise InvalidSnapshot("nonpositive execution")
        executed = timestamp(fill["executed_at"])
        if executed > collected or executed.astimezone(ZoneInfo("America/New_York")).date() != day:
            raise InvalidSnapshot("fill outside trading day")
        by_order[oid] = order, declared, total + qty
        lines.append(f"  {order['symbol']} {order['side']} {qty}주 @ ${price:,.2f}")
    if any(declared != total for _, declared, total in by_order.values()):
        raise InvalidSnapshot("fill ledger does not reconcile")
    lines.append(f"위험관리/시스템 상태: {text(s['health'], 200)}")
    lines.append("보고 모듈 주문 권한: 없음")
    result = "\n".join(lines)
    if len(result.encode("utf-16-le")) // 2 > 4000:
        raise InvalidSnapshot("report too long; do not silently truncate")
    # One report per account + strategy + market day, even after redeployment.
    key = hashlib.sha256(json.dumps([s["source"], scope[0], scope[2], str(day)]).encode()).hexdigest()
    return key, result
