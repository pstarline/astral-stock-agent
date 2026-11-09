"""Fixed SMA research candidate and same-unit holding reference; never deploys.

This is an original, deliberately simple research hypothesis, not a copy of the
Explore strategy. Parameters are explicit, never optimized by this module.
"""
from dataclasses import dataclass
from datetime import date, timezone
from decimal import Context, Decimal, localcontext
from zoneinfo import ZoneInfo

from .research_data import DailyHistory, ResearchDataError
from .research_contracts import ExecutionParity, require_matching_execution
from .research_execution import PriceOnlySimulation, _decimal, simulate_next_open
from .research_sessions import NY_SESSION_DATE, UTC_SESSION_DATE, require_daily_session_coverage


@dataclass(frozen=True)
class CloseDecisions:
    changes: tuple[Decimal, ...]
    reference_changes: tuple[Decimal, ...]
    first_eligible_index: int

    @property
    def pending_final_change(self):
        return self.changes[-1]

    @property
    def executable_changes(self):
        # Preserve the final decision above, but never fabricate its next open.
        return self.changes[:-1] + (Decimal(0),)


@dataclass(frozen=True)
class TrendComparison:
    decisions: CloseDecisions
    candidate: PriceOnlySimulation
    reference: PriceOnlySimulation
    window: int
    units: Decimal
    trade_from: date
    kind: str = 'hypothetical_price_only_comparison'
    deployment_approved: bool = False
    execution_conditions: ExecutionParity | None = None


def sma_close_decisions(history, *, window, units, trade_from, label_convention):
    """After close: hold fixed units iff close > trailing SMA (including today).

    Warmup uses preceding observed closes, never future closes. Equality is
    flat. The same-unit reference starts at the first eligible close and holds.
    A trade_from boundary prevents pre-evaluation entries while retaining prior
    warmup observations. This generator alone does not certify market data.
    """
    if (not isinstance(history, DailyHistory) or type(window) is not int
            or not 2 <= window <= 252 or len(history.bars) < window + 1):
        raise ResearchDataError('valid research window and sufficient history required')
    size = _decimal(units)
    if size <= 0 or type(trade_from) is not date:
        raise ResearchDataError('positive research units and evaluation date required')
    if label_convention not in (UTC_SESSION_DATE, NY_SESSION_DATE):
        raise ResearchDataError('explicit supported daily label convention required')
    zone = timezone.utc if label_convention == UTC_SESSION_DATE else ZoneInfo('America/New_York')
    closes, days = [], []
    for bar in history.bars:
        stamp = bar.timestamp
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ResearchDataError('timezone-aware research label required')
        value = _decimal(bar.close)
        if value <= 0:
            raise ResearchDataError('positive research close required')
        closes.append(value)
        days.append(stamp.astimezone(zone).date())
    if not days[0] <= trade_from <= days[-1]:
        raise ResearchDataError('evaluation date outside supplied history')
    changes, reference, held, first = [], [], Decimal(0), None
    with localcontext(Context(prec=28)):
        for index, close in enumerate(closes):
            eligible = index >= window - 1 and days[index] >= trade_from
            if eligible and first is None:
                first = index
            target = size if eligible and close > sum(closes[index-window+1:index+1]) / window else Decimal(0)
            changes.append(target - held)
            held = target  # prior decision is modeled as executed before next close
            reference.append(size if eligible and first == index else Decimal(0))
    if first is None or first == len(closes) - 1:
        raise ResearchDataError('no eligible close with a next session open')
    return CloseDecisions(tuple(changes), tuple(reference), first)


def compare_sma_candidate(history, *, window, units, trade_from, initial_cash,
                          commission_bps, slippage_bps, first_day, last_day, now,
                          label_convention, external_execution=None):
    """Run a candidate and reference under identical explicit execution costs.

    Supplied external execution conditions must match before any calculation.
    Matching this subset does not certify signals, costs or full strategy parity.
    Without external conditions the result records no external check.
    Full scheduled coverage is required, including warmup. No parameter search,
    approval decision, Paper result, or full-strategy causality claim is made.
    """
    condition_check = require_matching_execution(external_execution) if external_execution is not None else None
    coverage = dict(first_day=first_day, last_day=last_day, now=now, label_convention=label_convention)
    require_daily_session_coverage(history, **coverage)
    decisions = sma_close_decisions(history, window=window, units=units,
                                   trade_from=trade_from, label_convention=label_convention)
    costs = dict(initial_cash=initial_cash, commission_bps=commission_bps, slippage_bps=slippage_bps)
    candidate = simulate_next_open(history, decisions.executable_changes, **costs, **coverage)
    reference = simulate_next_open(history, decisions.reference_changes, **costs, **coverage)
    return TrendComparison(decisions, candidate, reference, window, _decimal(units), trade_from,
                           execution_conditions=condition_check)
