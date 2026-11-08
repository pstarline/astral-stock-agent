"""Calendar expectations from NYSE's published 2026-2028 cash-equity schedule.

Source: https://www.nyse.com/trade/hours-calendars (checked 2026-11-08).
Expected holidays/times below are independent of the calendar library output.
"""

import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from astral_stock_agent.activity import collect_session_activity, render_activity
from astral_stock_agent.report import InvalidSnapshot, render
from astral_stock_agent.sessions import SessionError, latest_closed_session, require_closed_session, session_on


def dt(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


HOLIDAYS = [
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
    "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
    "2028-01-17", "2028-02-21", "2028-04-14", "2028-05-29", "2028-06-19",
    "2028-07-04", "2028-09-04", "2028-11-23", "2028-12-25",
]


@pytest.mark.parametrize("day", HOLIDAYS)
def test_nyse_published_holidays_closed(day):
    assert session_on(date.fromisoformat(day)) is None


@pytest.mark.parametrize("day,opens,closes", [
    ("2026-03-06", "14:30", "21:00"), ("2026-03-09", "13:30", "20:00"),
    ("2026-10-30", "13:30", "20:00"), ("2026-11-02", "14:30", "21:00"),
    ("2026-11-27", "14:30", "18:00"), ("2026-12-24", "14:30", "18:00"),
    ("2027-11-26", "14:30", "18:00"), ("2028-07-03", "13:30", "17:00"),
    ("2028-11-24", "14:30", "18:00"),
    # July 2 2026 is NOT an early close; Dec 31 2027 is NOT a New Year observance.
    ("2026-07-02", "13:30", "20:00"), ("2027-12-31", "14:30", "21:00"),
    # US federal bank holidays that are normal NYSE trading sessions.
    ("2026-10-12", "13:30", "20:00"), ("2026-11-11", "14:30", "21:00"),
])
def test_core_hours_dst_and_published_early_closes(day, opens, closes):
    s = session_on(date.fromisoformat(day))
    assert s.opens_at == dt(f"{day}T{opens}:00Z")
    assert s.closes_at == dt(f"{day}T{closes}:00Z")


@pytest.mark.parametrize("now,expected", [
    ("2026-11-08T08:00:00Z", "2026-11-06"),
    ("2026-11-09T20:59:59Z", "2026-11-06"),
    ("2026-11-09T21:00:00Z", "2026-11-09"),
    ("2026-11-27T17:59:59Z", "2026-11-25"),
    ("2026-11-27T18:00:00Z", "2026-11-27"),
    ("2026-11-28T08:00:00Z", "2026-11-27"),
    ("2026-07-06T19:59:59Z", "2026-07-02"),
    ("2026-07-06T20:00:00Z", "2026-07-06"),
    ("2026-11-07T05:59:59+09:00", "2026-11-05"),
    ("2026-11-07T06:00:00+09:00", "2026-11-06"),
])
def test_latest_completed_session_uses_new_york_date(now, expected):
    assert latest_closed_session(dt(now)).day == date.fromisoformat(expected)


def test_weekends_range_and_clock_errors_are_not_weekday_fallbacks():
    assert session_on(date(2026, 11, 7)) is None
    for day in (date(2025, 12, 31), date(2029, 1, 2), "2026-11-06"):
        with pytest.raises(SessionError):
            session_on(day)
    for clock in (datetime(2026, 11, 6), dt("2029-01-02T22:00:00Z")):
        with pytest.raises(SessionError):
            latest_closed_session(clock)
    with pytest.raises(SessionError):
        latest_closed_session(dt("2026-01-01T22:00:00Z"))
    with pytest.raises(SessionError):
        require_closed_session(date(2026, 11, 6), dt("2026-11-06T20:59:59Z"))


def test_calendar_failure_blocks_report(monkeypatch):
    import astral_stock_agent.sessions as module

    def broken():
        raise RuntimeError("calendar unavailable")

    monkeypatch.setattr(module, "_calendar", broken)
    with pytest.raises(SessionError):
        session_on(date(2026, 11, 6))
    snapshot = snapshot_for("2026-11-06", "2026-11-06T21:00:00Z")
    with pytest.raises(InvalidSnapshot):
        render(snapshot, now=dt(snapshot["collected_at"]))


def snapshot_for(day, closes, collected=None):
    s = json.loads((Path(__file__).parents[1] / "examples/paper_snapshot.json").read_text())
    s.update(trading_day=day, session_closed_at=closes, valuation_at=closes,
             collected_at=collected or (dt(closes) + timedelta(minutes=5)).isoformat())
    return s


@pytest.mark.parametrize("snapshot", [
    snapshot_for("2026-11-26", "2026-11-26T21:00:00Z"),
    snapshot_for("2026-11-27", "2026-11-27T21:00:00Z"),
    snapshot_for("2026-07-02", "2026-07-02T21:00:00Z"),
    snapshot_for("2026-11-27", "2026-11-27T18:00:00Z", "2026-11-27T17:59:59Z"),
])
def test_report_rejects_forged_session_verified_or_wrong_close(snapshot):
    assert snapshot["session_verified"] is True
    with pytest.raises(InvalidSnapshot):
        render(snapshot, now=dt(snapshot["collected_at"]))


def test_report_accepts_published_early_close():
    s = snapshot_for("2026-11-27", "2026-11-27T18:00:00Z")
    assert "2026-11-27" in render(s, now=dt(s["collected_at"]))[1]


@pytest.mark.parametrize("executed,accepted", [
    ("2026-11-27T14:29:59Z", False), ("2026-11-27T18:00:01Z", False),
    ("2026-11-27T14:30:00Z", True), ("2026-11-27T18:00:00Z", True),
])
def test_report_fills_must_be_in_core_session_including_closing_auction(executed, accepted):
    s = snapshot_for("2026-11-27", "2026-11-27T18:00:00Z")
    s["orders"] = [{"order_ref": "example", "symbol": "SPY", "side": "buy", "status": "filled", "quantity": "2", "filled_quantity": "2"}]
    s["fills"] = [{"fill_ref": "example", "order_ref": "example", "quantity": "2", "price_usd": "500", "executed_at": executed}]
    if accepted:
        assert "2주 @ $500.00" in render(s, now=dt(s["collected_at"]))[1]
    else:
        with pytest.raises(InvalidSnapshot):
            render(s, now=dt(s["collected_at"]))


def test_session_collector_selects_previous_session_and_keeps_exact_close_execution():
    bundle = json.loads((Path(__file__).parents[1] / "examples/astral_activity.json").read_text())
    result = bundle["responses"]["trades"]["result"]
    result["rows"][0]["timestamp_utc"] = "2026-11-06T21:00:00Z"
    calls = []
    now = dt("2026-11-08T08:00:00Z")  # Sunday: select Friday, not Sunday.

    async def bridge(tool, arguments):
        calls.append((tool, arguments))
        name = next(k for k, v in bundle["responses"].items() if v["tool"] == tool)
        return bundle["responses"][name]["result"]

    observed = asyncio.run(collect_session_activity(bridge, account_ref=bundle["account_ref"],
        strategy_ref=101, verified_us_symbols=["SPY"], clock=lambda: now))
    assert observed["session"]["trading_day"] == "2026-11-06"
    assert len(observed["observed_trades"]) == 1
    assert observed["start"] == "2026-11-06T14:30:00+00:00"
    assert observed["ledger_reconciled"] is False
    assert "2026-11-06" in render_activity(observed)
    assert len(calls) == 4


def test_explicit_holiday_blocks_before_any_tool_call():
    calls = []

    async def bridge(tool, arguments):
        calls.append(tool)

    with pytest.raises(SessionError):
        asyncio.run(collect_session_activity(bridge, account_ref="example", strategy_ref=101,
            verified_us_symbols=["SPY"], trading_day=date(2026, 11, 26),
            clock=lambda: dt("2026-11-27T20:00:00Z")))
    assert calls == []
