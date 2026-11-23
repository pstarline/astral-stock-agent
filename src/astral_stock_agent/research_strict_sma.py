"""Separate SPY/SMA15 hypothesis: strict exits, equality retains prior target.

Original research adaptation, not replication of Backtrader quickstart09
e20cea18b88e1693b65e408f8778cc076ab21a67 (ORCL/10 units/zero commission).
Source rights/parity, actual data, selection independence and performance remain
unverified. No runner registration, I/O, selection, Paper or order authority.
"""
from dataclasses import dataclass, field
from datetime import date
from decimal import Context, Decimal, localcontext
import hashlib
import json

from .research_data import DailyHistory, ResearchDataError
from .research_execution import PriceOnlySimulation, _decimal, simulate_next_open
from .research_sessions import require_daily_session_coverage
from .research_trend import CloseDecisions, sma_close_decisions

RULE_ID = 'spy_sma15_strict_exit_hold_equality_research_v1'
ERROR = 'bounded strict SMA15 research inputs required'
UNRESOLVED = ('source_rights_and_rule_parity', 'provider_authenticity_and_adjustments',
              'selection_history_and_independent_holdout', 'walk_forward_and_regimes',
              'sample_adequacy_and_survivorship', 'actual_forward_paper')


@dataclass(frozen=True)
class StrictSMAComparison:
    input_sha256: str
    data_kind: str
    strict_decisions: CloseDecisions = field(repr=False)
    else_flat_decisions: CloseDecisions = field(repr=False)
    strict: PriceOnlySimulation = field(repr=False)
    else_flat: PriceOnlySimulation = field(repr=False)
    rule_id: str = RULE_ID
    status: str = 'insufficient'
    unresolved: tuple = UNRESOLVED
    source_equivalence_verified: bool = False
    selection_independence_verified: bool = False
    deployment_approved: bool = False
    new_orders_allowed: bool = False


def strict_sma15_decisions(history, *, trade_from, label_convention):
    """Fixed one-unit long/flat targets; includes today, initially flat.

    Signal deltas describe target changes after close, not actual executions.
    Uses the existing generator's input/warmup/eligibility checks and result
    contract. This helper alone does not certify calendar coverage or prices.
    """
    try:
        if (type(history) is not DailyHistory or history.symbol != 'SPY'
                or type(history.bars) is not tuple or not 16 <= len(history.bars) <= 2048
                or type(trade_from) is not date):
            raise ValueError
        baseline = sma_close_decisions(history, window=15, units='1', trade_from=trade_from,
                                       label_convention=label_convention)
        closes = tuple(_decimal(b.close) for b in history.bars)
        changes, held = [], Decimal(0)
        with localcontext(Context(prec=28)):
            for index, close in enumerate(closes):
                target = Decimal(0)
                if index >= baseline.first_eligible_index:
                    average = sum(closes[index-14:index+1]) / 15
                    target = (Decimal(1) if close > average else
                              Decimal(0) if close < average else held)
                changes.append(target - held)
                held = target
        return CloseDecisions(tuple(changes), baseline.reference_changes,
                              baseline.first_eligible_index)
    except Exception:
        raise ResearchDataError(ERROR) from None


def compare_strict_sma15(history, *, trade_from, first_day, last_day, now,
                         label_convention, data_kind):
    """Two calculations only, fixed 10000 USD/one share/5bp fee + 5bp slip.

    Compare with existing >else-flat at the SAME 15 bars, dates, prices and
    costs. No ranking, SMA20 comparison, source execution, OOS/WFO or forward
    claim. Full session coverage is checked before decisions; final pending
    deltas are retained separately and never fabricated into terminal fills.
    Results contain private synthetic/historical accounting, not public output.
    """
    try:
        if type(data_kind) is not str or data_kind not in ('synthetic', 'historical'):
            raise ValueError
        coverage = dict(first_day=first_day, last_day=last_day, now=now,
                        label_convention=label_convention)
        require_daily_session_coverage(history, **coverage)
        strict = strict_sma15_decisions(history, trade_from=trade_from,
                                        label_convention=label_convention)
        baseline = sma_close_decisions(history, window=15, units='1', trade_from=trade_from,
                                       label_convention=label_convention)
        costs = dict(initial_cash='10000', commission_bps='5', slippage_bps='5')
        # Bind actual normalized inputs, not the caller's unauthenticated history.sha256.
        inputs = dict(rule=RULE_ID, symbol='SPY', window=15, units='1', costs=costs,
                      trade_from=trade_from.isoformat(), first_day=first_day.isoformat(),
                      last_day=last_day.isoformat(), label_convention=label_convention,
                      data_kind=data_kind, execution='daily_close_next_session_open_long_cash',
                      bars=[dict(timestamp=b.timestamp.isoformat(), **{
                          k: str(_decimal(getattr(b, k)))
                          for k in ('open', 'high', 'low', 'close', 'volume')})
                          for b in history.bars])
        digest = hashlib.sha256(json.dumps(inputs, sort_keys=True,
                            separators=(',', ':'), allow_nan=False).encode()).hexdigest()
        left = simulate_next_open(history, strict.executable_changes, **costs, **coverage)
        right = simulate_next_open(history, baseline.executable_changes, **costs, **coverage)
        return StrictSMAComparison(digest, data_kind, strict, baseline, left, right)
    except Exception:
        raise ResearchDataError(ERROR) from None
