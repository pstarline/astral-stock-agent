"""Normalize observed Astral activity without inventing order/fill links.

This is a private diagnostic observation, NOT the reconciled daily snapshot
accepted by report.render or Telegram delivery. MCP trade history currently
does not expose a fill ID or order ID. Identical trade rows are preserved.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from math import ceil

from .astral import AstralBoundaryError, paper_deployment
from .report import InvalidSnapshot, money, symbol, text, timestamp


LIMIT = 200
TOOLS = {
    "accounts": "astral_broker_accounts_list",
    "deployments": "astral_deployments_list",
    "orders": "astral_order_list",
    "trades": "astral_trade_history",
}


def _number(value, *, positive=False):
    if type(value) not in (int, float, str):
        raise InvalidSnapshot("invalid Astral number")
    result = money(str(value), nonnegative=True)
    if positive and result <= 0:
        raise InvalidSnapshot("nonpositive Astral number")
    return result


def _result(value):
    if not isinstance(value, dict) or value.get("isError"):
        raise InvalidSnapshot("MCP tool failed")
    # Accept a tool's structuredContent or an already-unwrapped result.
    result = value.get("structuredContent", value)
    if not isinstance(result, dict) or result.get("error") or result.get("status") == "needs_correction":
        raise InvalidSnapshot("MCP structured result unavailable")
    for field in ("truncated", "results_truncated", "has_more"):
        if result.get(field) not in (None, False):
            raise InvalidSnapshot("incomplete MCP result")
    if result.get("rows_omitted_from_model", 0) != 0 or result.get("next_cursor"):
        raise InvalidSnapshot("incomplete MCP result")
    if result.get("delivery") not in (None, "inline") or result.get("artifact") or result.get("download"):
        raise InvalidSnapshot("inline complete response required")
    return result


def _rows(result, field, count_field):
    rows = result.get(field)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        # A human message such as 'No positions' is not an authoritative []
        # dataset contract. Likewise do not turn missing trade rows into [].
        raise InvalidSnapshot("missing structured activity rows")
    count = result.get(count_field)
    if type(count) is not int or count != len(rows) or len(rows) >= LIMIT:
        raise InvalidSnapshot("activity count mismatch or query cap reached")
    return rows


def normalize_activity(bundle, *, verified_us_symbols, now=None):
    now = now or datetime.now(timezone.utc)
    try:
        return _normalize(bundle, verified_us_symbols, now)
    except (KeyError, TypeError, AttributeError, ValueError, OverflowError):
        # Do not propagate private response strings or credential-bearing errors.
        raise InvalidSnapshot("Astral activity unavailable or unverified") from None


def _normalize(bundle, verified_us_symbols, now):
    if type(bundle["version"]) is not int or bundle["version"] != 1:
        raise InvalidSnapshot("unsupported bundle version")
    if bundle["source"] not in {"fixture", "astral_mcp"} or now.tzinfo is None:
        raise InvalidSnapshot("invalid source or clock")
    start, end = timestamp(bundle["start"]), timestamp(bundle["end"])
    if not start < end <= now or now - start > timedelta(days=30):
        raise InvalidSnapshot("invalid observation window")
    account_ref = text(bundle["account_ref"])
    strategy_ref = text(str(bundle["strategy_ref"]))
    expected = {
        "accounts": {}, "deployments": {},
        "orders": {"account_id": account_ref, "strategy_id": int(strategy_ref), "limit": LIMIT},
        "trades": {"scope": "strategy", "strategy_id": int(strategy_ref),
                   "lookback_days": bundle["lookback_days"], "limit": LIMIT, "delivery": "inline"},
    }
    lookback = bundle["lookback_days"]
    if type(lookback) is not int or not 1 <= lookback <= 30 or now - start > timedelta(days=lookback):
        raise InvalidSnapshot("trade query does not cover requested window")
    results = {}
    times = {}
    for name, tool in TOOLS.items():
        envelope = bundle["responses"][name]
        if envelope["tool"] != tool or envelope["arguments"] != expected[name]:
            raise InvalidSnapshot("wrong MCP query scope")
        received = timestamp(envelope["received_at"])
        if not timedelta(0) <= now - received <= timedelta(minutes=5):
            raise InvalidSnapshot("stale MCP observation")
        times[name] = received
        results[name] = _result(envelope["result"])
    if max(times.values()) - min(times.values()) > timedelta(minutes=1) or end > min(times.values()):
        raise InvalidSnapshot("inconsistent collection window")
    selected = paper_deployment(results["accounts"], results["deployments"],
                                account_ref=account_ref, strategy_ref=strategy_ref,
                                verified_us_symbols=verified_us_symbols)
    assets = set(selected["deployment"]["assets"])
    orders, trades, seen = [], [], set()
    # source='live' is observed even for paper intents. Check provider, account,
    # strategy and deployment mode instead; never classify by that label alone.
    for row in _rows(results["orders"], "orders", "count"):
        if (row["provider"] != "astral_paper" or row["broker_account_id"] != account_ref
                or str(row["strategy_id"]) != strategy_ref):
            raise InvalidSnapshot("order scope mismatch")
        ticker = symbol(text(row["symbol"]).upper())
        if ticker not in assets or row["side"] not in {"buy", "sell"}:
            raise InvalidSnapshot("order outside universe")
        oid = text(row["intent_id"])
        if oid in seen:
            raise InvalidSnapshot("duplicate intent")
        seen.add(oid)
        status = text(row["status"], 40)
        quantity = _number(row["quantity"], positive=True)
        # Preserve source timestamps without calling submission a fill time.
        event_times = {}
        for key in ("dispatch_started_at", "broker_submit_started_at", "broker_ack_at", "terminal_at"):
            if row.get(key) is not None:
                value = timestamp(row[key])
                if value > times["orders"]:
                    raise InvalidSnapshot("order timestamp after observation")
                event_times[key] = value.isoformat()
        if not event_times:
            raise InvalidSnapshot("order event time unavailable")
        if any(start <= timestamp(t) < end for t in event_times.values()):
            orders.append({"order_ref": oid, "symbol": ticker, "side": row["side"],
                           "requested_quantity": str(quantity), "status": status,
                           "filled_quantity": None, "event_times": event_times})
    result = results["trades"]
    if result.get("type") != "dataset" or result.get("source") != "portfolio:trades":
        raise InvalidSnapshot("wrong trade dataset")
    for row in _rows(result, "rows", "row_count"):
        if row["source"] != "paper" or str(row["source_id"]) != strategy_ref or row["currency"] != "USD":
            raise InvalidSnapshot("trade scope mismatch")
        ticker = symbol(text(row["symbol"]).upper())
        if ticker not in assets or row["side"] not in {"buy", "sell"}:
            raise InvalidSnapshot("trade outside universe")
        qty = _number(row["qty"], positive=True)
        price = _number(row["price"], positive=True)
        value = _number(row["value"], positive=True)
        if _number(row["contract_multiplier"], positive=True) != 1 or abs(qty * price - value) > Decimal("0.01"):
            raise InvalidSnapshot("non-equity multiplier or inconsistent trade value")
        executed = timestamp(row["timestamp_utc"])
        if executed > times["trades"]:
            raise InvalidSnapshot("trade timestamp after observation")
        if start <= executed < end:
            # No synthetic fill_ref, guessed order_ref or silent deduplication.
            trades.append({"symbol": ticker, "side": row["side"], "quantity": str(qty),
                           "price_usd": str(price), "value_usd": str(value),
                           "executed_at": executed.isoformat()})
    return {"kind": "astral_activity_observation", "source": bundle["source"],
            "mode": "astral_paper", "account_ref": account_ref, "strategy_ref": strategy_ref,
            "deployment_ref": str(selected["deployment"]["id"]),
            "collected_at": max(times.values()).isoformat(), "start": start.isoformat(), "end": end.isoformat(),
            "orders": orders, "observed_trades": trades, "ledger_reconciled": False,
            "complete_daily_snapshot": False}


async def collect_activity(call_tool, *, account_ref, strategy_ref, verified_us_symbols,
                           start, end, clock=None):
    """call_tool(name, arguments) is an authenticated, READ-ONLY MCP bridge.

    Does not obtain credentials or open an HTTP connection. The caller must
    supply actual tool results, not model-written substitutes.
    """
    clock = clock or (lambda: datetime.now(timezone.utc))
    now = clock()
    if now.tzinfo is None or start.tzinfo is None or end.tzinfo is None:
        raise InvalidSnapshot("timezone required")
    if not start < end <= now or now - start >= timedelta(days=29):
        raise InvalidSnapshot("invalid collection window")
    lookback = max(1, ceil((now - start).total_seconds() / 86400) + 1)
    responses = {}

    async def read(name, arguments):
        try:
            result = await call_tool(TOOLS[name], arguments)
        except Exception:
            raise InvalidSnapshot("Astral read failed") from None
        envelope = {"tool": TOOLS[name], "arguments": arguments,
                    "received_at": clock().isoformat(), "result": result}
        responses[name] = envelope
        return _result(result)

    accounts, deployments = await asyncio.gather(read("accounts", {}), read("deployments", {}))
    # Admit scope BEFORE requesting any order/trade history.
    try:
        paper_deployment(accounts, deployments, account_ref=account_ref, strategy_ref=strategy_ref,
                         verified_us_symbols=verified_us_symbols)
    except (AstralBoundaryError, TypeError, AttributeError):
        raise InvalidSnapshot("intended US paper deployment unavailable") from None
    await asyncio.gather(
        read("orders", {"account_id": account_ref, "strategy_id": int(strategy_ref), "limit": LIMIT}),
        read("trades", {"scope": "strategy", "strategy_id": int(strategy_ref),
                        "lookback_days": lookback, "limit": LIMIT, "delivery": "inline"}),
    )
    bundle = {"version": 1, "source": "astral_mcp", "account_ref": account_ref,
              "strategy_ref": strategy_ref, "lookback_days": lookback,
              "start": start.isoformat(), "end": end.isoformat(), "responses": responses}
    return normalize_activity(bundle, verified_us_symbols=verified_us_symbols, now=clock())


def render_activity(observation):
    prefix = "[검증용 예제 · 실제 거래 기록 아님]\n" if observation["source"] == "fixture" else ""
    lines = [prefix + "[Astral Paper 조회 관측 · 일일 보고 미완성]",
             f"조회 구간: {observation['start']} ~ {observation['end']} (끝 시각 제외)",
             f"조회된 주문: {len(observation['orders'])}건",
             f"조회된 거래 기록: {len(observation['observed_trades'])}건",
             "주문-체결 연결: 미검증 / 잔고·손익·휴장 확인: 미완료",
             "기록이 없어도 당일 전체 체결이 없었다고 확정하지 않습니다."]
    for order in observation["orders"]:
        lines.append(f"주문 상태: {order['symbol']} {order['side']} {order['status']} / 요청 {order['requested_quantity']}주")
    for trade in observation["observed_trades"]:
        lines.append(f"거래 관측: {trade['symbol']} {trade['side']} {trade['quantity']}주 @ ${trade['price_usd']} ({trade['executed_at']})")
    return "\n".join(lines)
