"""Read-only admission checks over official MCP structuredContent.

The Work agent supplies authenticated tool results. No HTTP endpoint, API key,
or borrowed ChatGPT OAuth token is assumed to be available inside Python/CI.
"""


class AstralBoundaryError(ValueError):
    pass


def paper_account(accounts, *, account_ref):
    """Resolve one intended verified USD Paper account; returned row is private."""
    if not isinstance(accounts, dict) or accounts.get("status") == "needs_correction":
        raise AstralBoundaryError("invalid account response")
    if accounts.get("account_status_error") or accounts.get("broker_health_error"):
        raise AstralBoundaryError("account state unavailable")
    candidates = [a for a in accounts.get("broker_accounts", []) if a.get("account_id") == account_ref]
    if len(candidates) != 1:
        raise AstralBoundaryError("paper account unresolved")
    account = candidates[0]
    if (account.get("account_type") != "paper" or account.get("provider") != "astral_paper"
            or account.get("account_status") != "active" or account.get("connection_status") != "active"
            or account.get("verified") is not True or account.get("currency") != "USD"):
        raise AstralBoundaryError("active verified USD paper account required")
    return account


def paper_deployment(accounts, deployments, *, account_ref, strategy_ref, verified_us_symbols):
    """Select one existing US-equity paper deployment; never create/replace one.

    verified_us_symbols must come from an independently checked US-equity
    universe. Symbol spelling alone cannot establish asset class.
    Account UUIDs and resulting rows are PRIVATE operational data.
    """
    if not isinstance(deployments, dict) or deployments.get("status") != "ok":
        raise AstralBoundaryError("MCP response unsuccessful")
    account = paper_account(accounts, account_ref=account_ref)
    candidates = [d for d in deployments.get("active_strategies", [])
                  if str(d.get("strategy_id")) == str(strategy_ref) and d.get("broker_account_id") == account_ref]
    if len(candidates) != 1:
        raise AstralBoundaryError("intended deployment absent; existing activity must not be replaced")
    deployment = candidates[0]
    if (deployment.get("execution_mode") != "astral_paper" or deployment.get("has_crypto") is not False
            or deployment.get("lifecycle_state") != "active" or deployment.get("runtime_health") != "ok"
            or deployment.get("ready_to_trade") is not True):
        raise AstralBoundaryError("deployment not healthy US paper")
    assets = deployment.get("assets")
    if not isinstance(assets, list) or not assets or not set(assets).issubset(set(verified_us_symbols)):
        raise AstralBoundaryError("US-equity universe not verified")
    occupied = account.get("active_deployment") or {}
    if str(occupied.get("strategy_id")) != str(strategy_ref) or occupied.get("active_strategy_id") != deployment.get("id"):
        raise AstralBoundaryError("account and deployment disagree")
    return {"account": account, "deployment": deployment}
