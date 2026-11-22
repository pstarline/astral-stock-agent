"""One declared frozen evaluation window, not certification of independence.

Pre-window prices warm indicators only. No training-period positions/PnL are
carried into the fresh hypothetical account. No fitting, search, I/O or orders.
Caller-supplied freeze times/hashes are not authenticated preregistration.
"""
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from .independent_validation import validate_candidate, _digest, RUNNERS, MAX_ROWS
from .research_data import read_daily_csv, MAX_BYTES, ResearchDataError
from .research_sessions import (UTC_SESSION_DATE, NY_SESSION_DATE,
                                require_daily_session_coverage)
from .sessions import session_on


ERROR = 'frozen window requires explicit bounded local hypothesis and chronology'
FROZEN_FIELDS = ('schema', 'symbol', 'first_day', 'last_day', 'label_convention',
                 'initial_cash', 'commission_bps', 'slippage_bps', 'rule')


@dataclass(frozen=True)
class FrozenWindowResult:
    status: str
    reason: str
    evidence_sha256: str | None = None
    evaluation_start: str | None = None
    evaluation_end: str | None = None
    result: object = field(default=None, repr=False)
    semantics: str = 'single_declared_window_fresh_flat_account_with_prior_warmup'
    selection_independence_verified: bool = field(default=False, init=False)
    deployment_approved: bool = field(default=False, init=False)
    new_orders_allowed: bool = field(default=False, init=False)


def hypothesis_sha256(spec):
    """Bind rule, dates and economics, excluding not-yet-known price bytes.

    This digest is our local hypothesis fingerprint, never a provider version
    or proof that the caller actually recorded it before evaluating the window.
    Data checksum/count remain bound by the downstream calculation evidence.
    """
    try:
        if type(spec) is not dict or spec.get('schema') not in RUNNERS:
            raise ValueError
        return _digest(dict(contract='declared_frozen_window_v1',
                            hypothesis={k: spec[k] for k in FROZEN_FIELDS}))
    except Exception:
        raise ResearchDataError(ERROR) from None


def evaluate_frozen_window(spec, prices, *, selection_end, evaluation_start,
                           frozen_at, frozen_hypothesis_sha256, data_kind,
                           verified_us_symbols, now, stress_costs=None):
    """Evaluate one unchanged hypothesis using the existing bounded validator.

    Dates are explicit ISO session dates. Selection ends on the session directly
    before evaluation starts; no overlapping window or implicit embargo. The
    declared freeze must be after selection close and strictly before evaluation
    open. At least two evaluation sessions are required. The rule's trade_from
    MUST equal evaluation_start; prior closes prepare indicators but cannot
    create positions or returns. This is a flat reset, not continuous Paper.

    No selection results are computed, no parameters fitted, no automatic folds
    or retries. At most four existing rule evaluations (optional shared stress).
    Even a successful computation remains insufficient: actual selection history,
    unseen data and preregistration/independence are unverified. Keep evidence
    context and aggregate results private. There is no operational approval.
    """
    try:
        if (hypothesis_sha256(spec) != frozen_hypothesis_sha256
                or type(prices) is not bytes or not 0 < len(prices) <= MAX_BYTES
                or type(spec.get('price_count')) is not int
                or not 3 <= spec['price_count'] <= MAX_ROWS):
            raise ValueError
        end, start = date.fromisoformat(selection_end), date.fromisoformat(evaluation_start)
        if (end.isoformat() != selection_end or start.isoformat() != evaluation_start
                or not date.fromisoformat(spec['first_day']) <= end < start
                or not start < date.fromisoformat(spec['last_day'])
                or spec['rule']['trade_from'] != evaluation_start):
            raise ValueError
        if any(not isinstance(t, datetime) or t.tzinfo is None or t.utcoffset() is None
               for t in (frozen_at, now)):
            raise ValueError
        selected, first = session_on(end), session_on(start)
        if (selected is None or first is None
                or not selected.closes_at <= frozen_at < first.opens_at
                or frozen_at > now):
            raise ValueError
        convention = spec['label_convention']
        if convention not in (UTC_SESSION_DATE, NY_SESSION_DATE):
            raise ValueError
        history = read_daily_csv(prices, symbol=spec['symbol'],
            expected_sha256=spec['price_sha256'], expected_count=spec['price_count'],
            start=spec['first_day']+'T00:00:00Z', end=spec['last_day']+'T23:59:59Z')
        require_daily_session_coverage(history, first_day=date.fromisoformat(spec['first_day']),
            last_day=date.fromisoformat(spec['last_day']), now=now, label_convention=convention)
        zone = timezone.utc if convention == UTC_SESSION_DATE else ZoneInfo('America/New_York')
        labels = [b.timestamp.astimezone(zone).date() for b in history.bars]
        index = labels.index(start)
        if index < 1 or labels[index-1] != end or len(labels)-index < 2:
            raise ValueError
        result = validate_candidate(spec, prices, origin='self', data_kind=data_kind,
            verified_us_symbols=verified_us_symbols, now=now, stress_costs=stress_costs)
        if result.status == 'held' or not result.scenarios:
            return FrozenWindowResult('held', 'local_validation_blocked', result=result)
        evidence = _digest(dict(contract='declared_frozen_window_v1',
            hypothesis=frozen_hypothesis_sha256, selection_end=selection_end,
            evaluation_start=evaluation_start, evaluation_end=spec['last_day'],
            frozen_at=frozen_at.astimezone(timezone.utc).isoformat(),
            result_input=result.input_sha256,
            results=[s['result_sha256'] for s in result.scenarios]))
        return FrozenWindowResult('insufficient',
            'window_computed_but_selection_history_and_independence_unverified',
            evidence, evaluation_start, spec['last_day'], result)
    except Exception:
        raise ResearchDataError(ERROR) from None
