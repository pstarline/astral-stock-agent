import copy

import pytest

from astral_stock_agent.astral import AstralBoundaryError, paper_deployment


def inputs():
    # Synthetic schema fixture; never contains user IDs or balances.
    accounts = {"broker_accounts": [{"account_id": "example", "account_type": "paper",
                "provider": "astral_paper", "account_status": "active", "connection_status": "active",
                "verified": True, "currency": "USD", "active_deployment": {"strategy_id": 1, "active_strategy_id": 2}}]}
    deployments = {"status": "ok", "active_strategies": [{"id": 2, "strategy_id": 1,
                   "broker_account_id": "example", "execution_mode": "astral_paper", "has_crypto": False,
                   "lifecycle_state": "active", "runtime_health": "ok", "ready_to_trade": True, "assets": ["SPY"]}]}
    return accounts, deployments


def select(a, d, symbols=("SPY",)):
    return paper_deployment(a, d, account_ref="example", strategy_ref=1, verified_us_symbols=symbols)


def test_existing_healthy_paper_selected_without_mutation():
    a, d = inputs()
    before = copy.deepcopy((a, d))
    assert select(a, d)["deployment"]["strategy_id"] == 1
    assert (a, d) == before


@pytest.mark.parametrize("field,value", [("execution_mode", "full_broker"), ("has_crypto", True),
    ("ready_to_trade", False), ("runtime_health", "unknown"), ("strategy_id", 99), ("assets", ["BTCUSD"])])
def test_live_crypto_unready_or_unrelated_deployment_rejected(field, value):
    a, d = inputs()
    d["active_strategies"][0][field] = value
    with pytest.raises(AstralBoundaryError):
        select(a, d)


def test_live_account_and_unknown_universe_rejected():
    a, d = inputs()
    with pytest.raises(AstralBoundaryError):
        select(a, d, ())
    a["broker_accounts"][0]["account_type"] = "live"
    with pytest.raises(AstralBoundaryError):
        select(a, d)


def test_account_deployment_disagreement_rejected():
    a, d = inputs()
    a["broker_accounts"][0]["active_deployment"]["active_strategy_id"] = 3
    with pytest.raises(AstralBoundaryError):
        select(a, d)
