"""Provider-independent execution descriptors, never strategy approval.

Five-field equality describes execution timing only, not signal, price, sizing,
cost, source-rule equivalence or independent strategy validation. No I/O.
"""
from dataclasses import dataclass

from .research_data import ResearchDataError


@dataclass(frozen=True)
class ExecutionContract:
    bar_size: str
    fill_timing: str
    timezone: str
    decision_events: tuple[str, ...]
    follow_live: bool

    def __post_init__(self):
        if (self.bar_size not in ('1m', '5m', '15m', '30m', '1h', '4h', '1d')
                or self.fill_timing not in ('same_bar_close', 'next_session_open')
                or self.timezone not in ('UTC', 'America/New_York')
                or type(self.decision_events) is not tuple or not self.decision_events
                or any(x not in ('bar_close', 'bar_open') for x in self.decision_events)
                or len(set(self.decision_events)) != len(self.decision_events)
                or type(self.follow_live) is not bool):
            raise ResearchDataError('invalid execution contract')


# Describes the local research_execution model only.
LOCAL_DAILY_EXECUTION = ExecutionContract('1d', 'next_session_open', 'UTC', ('bar_close',), False)


@dataclass(frozen=True)
class ExecutionParity:
    matches: bool
    mismatched_fields: tuple[str, ...]
    full_strategy_parity_verified: bool = False
    deployment_approved: bool = False


def compare_execution_conditions(observed, expected=LOCAL_DAILY_EXECUTION):
    """Compare five execution fields only; equality is not backtest validation."""
    if not isinstance(observed, ExecutionContract) or not isinstance(expected, ExecutionContract):
        raise ResearchDataError('explicit execution contracts required')
    fields = ('bar_size', 'fill_timing', 'timezone', 'decision_events', 'follow_live')
    mismatch = tuple(k for k in fields if getattr(observed, k) != getattr(expected, k))
    return ExecutionParity(not mismatch, mismatch)


def require_matching_execution(observed, expected=LOCAL_DAILY_EXECUTION):
    result = compare_execution_conditions(observed, expected)
    if not result.matches:
        raise ResearchDataError('execution conditions differ; reproduction comparison blocked')
    return result
