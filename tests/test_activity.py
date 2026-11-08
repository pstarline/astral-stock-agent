import asyncio
import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from astral_stock_agent.activity import collect_activity, normalize_activity, render_activity
from astral_stock_agent.cli import main
from astral_stock_agent.report import InvalidSnapshot, render


NOW = datetime(2026, 11, 6, 21, 5, tzinfo=timezone.utc)


@pytest.fixture
def bundle():
    return json.loads((Path(__file__).parents[1] / "examples/astral_activity.json").read_text())


def normalize(bundle, **kwargs):
    return normalize_activity(bundle, verified_us_symbols=["SPY"], now=kwargs.get("now", NOW))


def result(bundle, name):
    return bundle["responses"][name]["result"]


def test_raw_paper_intent_live_label_is_not_live_trading(bundle):
    before = copy.deepcopy(bundle)
    observation = normalize(bundle)
    assert observation["mode"] == "astral_paper"
    assert observation["orders"][0]["status"] == "FILLED"
    assert observation["orders"][0]["filled_quantity"] is None
    assert observation["observed_trades"][0]["quantity"] == "2"
    assert observation["ledger_reconciled"] is False
    assert observation["complete_daily_snapshot"] is False
    assert "metadata" not in json.dumps(observation)
    assert "not included" not in json.dumps(observation)
    assert bundle == before


def test_no_guessed_order_fill_link_or_deduplication(bundle):
    trades = result(bundle, "trades")
    trades["rows"].append(copy.deepcopy(trades["rows"][0]))
    trades["row_count"] = 2
    observation = normalize(bundle)
    assert len(observation["observed_trades"]) == 2
    assert not {"order_ref", "fill_ref"} & set(observation["observed_trades"][0])
    assert "주문-체결 연결: 미검증" in render_activity(observation)
    with pytest.raises(InvalidSnapshot):
        render(observation, now=NOW)


@pytest.mark.parametrize("name", ["orders", "trades"])
@pytest.mark.parametrize("mutation", [
    lambda x: x.update(truncated=True),
    lambda x: x.update(rows_omitted_from_model=1),
    lambda x: x.update(has_more=True),
    lambda x: x.update(next_cursor="more"),
    lambda x: x.update(delivery="download"),
])
def test_truncated_or_download_only_results_blocked(bundle, name, mutation):
    mutation(result(bundle, name))
    with pytest.raises(InvalidSnapshot):
        normalize(bundle)


@pytest.mark.parametrize("name,field,count", [("orders", "orders", "count"), ("trades", "rows", "row_count")])
def test_query_cap_and_count_mismatch_blocked(bundle, name, field, count):
    r = result(bundle, name)
    r[count] = 2
    with pytest.raises(InvalidSnapshot):
        normalize(bundle)
    r[field] = [copy.deepcopy(r[field][0]) for _ in range(200)]
    r[count] = 200
    with pytest.raises(InvalidSnapshot):
        normalize(bundle)


@pytest.mark.parametrize("field,value", [("provider", "other"), ("broker_account_id", "other-account"),
    ("strategy_id", 202), ("symbol", "BTCUSD"), ("quantity", True), ("quantity", "NaN")])
def test_wrong_or_invalid_order_data_rejected(bundle, field, value):
    result(bundle, "orders")["orders"][0][field] = value
    with pytest.raises(InvalidSnapshot):
        normalize(bundle)


@pytest.mark.parametrize("field,value", [("source", "live"), ("source_id", "202"),
    ("currency", "EUR"), ("contract_multiplier", 100), ("price", "Infinity"),
    ("qty", -1), ("value", 999), ("timestamp_utc", "2026-11-06T21:06:00Z")])
def test_trade_scope_decimal_and_time_rejected(bundle, field, value):
    result(bundle, "trades")["rows"][0][field] = value
    with pytest.raises(InvalidSnapshot):
        normalize(bundle)


def test_utc_interval_end_exclusive_and_outside_rows_excluded(bundle):
    bundle["start"] = "2026-11-06T15:00:02Z"
    bundle["end"] = "2026-11-06T15:00:03Z"
    assert len(normalize(bundle)["observed_trades"]) == 1
    bundle["end"] = "2026-11-06T15:00:02Z"
    bundle["start"] = "2026-11-06T15:00:01Z"
    assert normalize(bundle)["observed_trades"] == []


def test_collection_staleness_span_and_history_window_checked(bundle):
    with pytest.raises(InvalidSnapshot):
        normalize(bundle, now=NOW + timedelta(minutes=6))
    bundle["responses"]["accounts"]["received_at"] = "2026-11-06T21:03:00Z"
    with pytest.raises(InvalidSnapshot):
        normalize(bundle)
    bundle["responses"]["accounts"]["received_at"] = NOW.isoformat()
    bundle["start"] = "2026-11-03T00:00:00Z"
    with pytest.raises(InvalidSnapshot):
        normalize(bundle)


def test_scope_query_tampering_and_duplicate_intents_blocked(bundle):
    bundle["responses"]["trades"]["arguments"]["scope"] = "all"
    with pytest.raises(InvalidSnapshot):
        normalize(bundle)
    bundle["responses"]["trades"]["arguments"]["scope"] = "strategy"
    r = result(bundle, "orders")
    r["orders"] *= 2
    r["count"] = 2
    with pytest.raises(InvalidSnapshot):
        normalize(bundle)


def test_empty_message_is_not_an_empty_trade_ledger(bundle):
    bundle["responses"]["trades"]["result"] = {"message": "No trades"}
    with pytest.raises(InvalidSnapshot):
        normalize(bundle)


def test_explicit_empty_rows_are_observations_not_no_trade_claim(bundle):
    result(bundle, "orders").update(orders=[], count=0)
    result(bundle, "trades").update(rows=[], row_count=0)
    output = render_activity(normalize(bundle))
    assert "조회된 거래 기록: 0건" in output
    assert "당일 전체 체결이 없었다고 확정하지 않습니다" in output


def test_collect_only_scoped_read_tools_and_unwraps_structured_content(bundle):
    calls = []

    async def bridge(tool, arguments):
        name = next(k for k, entry in bundle["responses"].items() if entry["tool"] == tool)
        assert arguments == bundle["responses"][name]["arguments"]
        calls.append(tool)
        return {"structuredContent": result(bundle, name)}

    observation = asyncio.run(collect_activity(bridge, account_ref=bundle["account_ref"],
        strategy_ref=101, verified_us_symbols=["SPY"],
        start=datetime.fromisoformat(bundle["start"].replace("Z", "+00:00")),
        end=datetime.fromisoformat(bundle["end"].replace("Z", "+00:00")), clock=lambda: NOW))
    assert observation["source"] == "astral_mcp"
    assert len(calls) == 4
    assert calls[:2] == ["astral_broker_accounts_list", "astral_deployments_list"]


def test_non_us_deployment_stops_before_order_history_queries(bundle):
    result(bundle, "deployments")["active_strategies"][0]["has_crypto"] = True
    calls = []

    async def bridge(tool, arguments):
        name = next(k for k, entry in bundle["responses"].items() if entry["tool"] == tool)
        calls.append(tool)
        return result(bundle, name)

    with pytest.raises(InvalidSnapshot, match="US paper deployment unavailable"):
        asyncio.run(collect_activity(bridge, account_ref=bundle["account_ref"], strategy_ref=101,
            verified_us_symbols=["SPY"], start=NOW - timedelta(hours=2), end=NOW, clock=lambda: NOW))
    assert len(calls) == 2


def test_upstream_failure_not_empty_success_or_secret_error():
    async def bridge(tool, arguments):
        raise RuntimeError("secret-token")

    with pytest.raises(InvalidSnapshot) as error:
        asyncio.run(collect_activity(bridge, account_ref="example", strategy_ref=101,
            verified_us_symbols=["SPY"], start=NOW - timedelta(hours=2), end=NOW, clock=lambda: NOW))
    assert "secret-token" not in str(error.value)


def test_cli_observation_preview_and_send_blocked(bundle, tmp_path, capsys):
    path = tmp_path / "raw.json"
    path.write_text(json.dumps(bundle))
    assert main([str(path), "--activity", "--us-symbol", "SPY"]) == 0
    assert "실제 거래 기록 아님" in capsys.readouterr().out
    assert main([str(path), "--activity", "--us-symbol", "SPY", "--send"]) == 2
    assert main([str(path), "--activity"]) == 2
