import copy
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from astral_stock_agent.cli import main
from astral_stock_agent.reconciliation import ReconciliationError, reconcile_daily
from astral_stock_agent.report import render


@pytest.fixture
def evidence():
    return json.loads((Path(__file__).parents[1] / "examples/daily_reconciliation.json").read_text())


def reconcile(e, symbols=("SPY",)):
    return reconcile_daily(e["previous"], e["current"], e["accounting"],
        verified_us_symbols=symbols,
        now=datetime.fromisoformat(e["current"]["collected_at"].replace("Z", "+00:00")))


def change_cash(e, delta):
    for key in ("cash_usd", "available_cash_usd", "equity_usd"):
        e["current"][key] = str(Decimal(e["current"][key]) + Decimal(delta))


def test_cash_shares_and_net_pnl_agree_without_mutating_books(evidence):
    before = copy.deepcopy(evidence)
    result = reconcile(evidence)
    # $1000 - (2*$100) + $110 - $3 fees = $907 cash.
    # $907 + $120 marked position = $1027; $8 realized + $19 unrealized = $27.
    assert result["daily_total_pnl_usd"] == "27"
    assert result["daily_realized_pnl_usd"] == "8"
    assert result["daily_unrealized_change_usd"] == "19"
    assert result["daily_execution_fees_usd"] == "3"
    assert evidence == before
    body = render(result, now=datetime.fromisoformat(result["collected_at"].replace("Z", "+00:00")))[1]
    assert "당일 총손익 (자금 유출입 제외): $27.00" in body
    assert "실제 거래 기록 아님" in body


@pytest.mark.parametrize("capital", ["500", "-100"])
def test_deposits_and_withdrawals_are_not_returns(evidence, capital):
    evidence["accounting"]["net_capital_flow_usd"] = capital
    change_cash(evidence, capital)
    assert reconcile(evidence)["daily_total_pnl_usd"] == "27"


@pytest.mark.parametrize("income", ["5", "-2"])
def test_other_net_income_is_not_double_counted_as_realized_trading_pnl(evidence, income):
    evidence["accounting"]["net_other_income_usd"] = income
    change_cash(evidence, income)
    result = reconcile(evidence)
    assert Decimal(result["daily_total_pnl_usd"]) == Decimal(27) + Decimal(income)
    assert result["daily_realized_pnl_usd"] == "8"


def test_reverse_history_order_and_partial_execution_rows(evidence):
    original = evidence["current"]["fills"][0]
    partial = copy.deepcopy(original)
    original["quantity"] = "1"
    partial.update(fill_ref="example-second-buy-fill", quantity="1", executed_at="2026-11-06T15:01:00Z")
    evidence["current"]["fills"].append(partial)
    evidence["current"]["fills"].reverse()
    assert reconcile(evidence)["daily_total_pnl_usd"] == "27"


@pytest.mark.parametrize("field", ["net_capital_flow_usd", "net_other_income_usd", "execution_fees_usd"])
@pytest.mark.parametrize("missing", ["absent", None])
def test_unknown_accounting_never_defaults_to_zero(evidence, field, missing):
    if missing == "absent":
        del evidence["accounting"][field]
    else:
        evidence["accounting"][field] = missing
    with pytest.raises(ReconciliationError):
        reconcile(evidence)


@pytest.mark.parametrize("field", ["cumulative_realized_pnl_usd", "unrealized_pnl_usd"])
@pytest.mark.parametrize("book", ["previous", "current"])
def test_unknown_source_pnl_blocks_component_validation(evidence, book, field):
    evidence[book][field] = None
    with pytest.raises(ReconciliationError):
        reconcile(evidence)


@pytest.mark.parametrize("mutation", [
    lambda e: e["accounting"].update(complete=False),
    lambda e: e["accounting"].update(pnl_basis="gross"),
    lambda e: e["accounting"].update(execution_fees_usd="0"),
    lambda e: e["accounting"].update(execution_fees_usd="-1"),
    lambda e: e["accounting"].update(source="astral_mcp"),
    lambda e: e["accounting"].update(account_ref="wrong"),
    lambda e: e["accounting"].update(start_at="2026-11-04T21:00:00Z"),
    lambda e: e["accounting"].update(end_at="2026-11-06T20:00:00Z"),
    lambda e: e["accounting"].update(collected_at="2026-11-06T20:00:00Z"),
    lambda e: e["accounting"].update(collected_at="2026-11-06T22:00:00Z"),
    lambda e: e["previous"].update(run_ref="earlier-run"),
    lambda e: e["previous"].update(deployment_ref="earlier-deployment"),
    lambda e: e["current"].update(valuation_at="2026-11-06T20:45:00Z"),
    lambda e: e["current"].update(daily_realized_pnl_usd="100"),
    lambda e: e["current"]["positions"][0].update(quantity="2"),
    lambda e: e.update(previous=None),
])
def test_inconsistent_or_missing_evidence_rejected(evidence, mutation):
    mutation(evidence)
    before = copy.deepcopy(evidence)
    with pytest.raises(ReconciliationError):
        reconcile(evidence)
    assert evidence == before


def test_gross_pnl_with_missing_fees_is_not_accepted(evidence):
    evidence["accounting"]["execution_fees_usd"] = "0"
    change_cash(evidence, "3")
    with pytest.raises(ReconciliationError):
        reconcile(evidence)


def test_cash_movement_without_matching_journal_rejected(evidence):
    change_cash(evidence, "0.02")
    with pytest.raises(ReconciliationError):
        reconcile(evidence)


def test_missing_previous_trading_day_is_not_multi_day_pnl(evidence):
    previous = evidence["previous"]
    previous.update(trading_day="2026-11-04", session_closed_at="2026-11-04T21:00:00Z",
        valuation_at="2026-11-04T21:00:00Z", collected_at="2026-11-04T21:05:00Z")
    with pytest.raises(ReconciliationError):
        reconcile(evidence)


def test_unverified_universe_rejected(evidence):
    with pytest.raises(ReconciliationError):
        reconcile(evidence, ("AAPL",))


def test_cli_reconciliation_preview_is_labelled_and_cannot_send(evidence, tmp_path, capsys):
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(evidence))
    assert main([str(path), "--reconcile", "--us-symbol", "SPY"]) == 0
    output = capsys.readouterr().out
    assert "$27.00" in output and "실제 거래 기록 아님" in output
    assert main([str(path), "--reconcile", "--us-symbol", "SPY", "--send"]) == 2
    assert main([str(path), "--reconcile", "--activity", "--us-symbol", "SPY"]) == 2
    assert main([str(path), "--reconcile"]) == 2
