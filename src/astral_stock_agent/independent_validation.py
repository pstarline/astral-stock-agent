"""Bounded offline checks shared by self and public research candidates.

This is an evidence-producing runner, not an approval gate. Only the existing
explicit local SMA/reversal rules execute. Public intake is read unchanged;
matching a few source fields never establishes source-rule equivalence. Raw
prices, source descriptions and reports are not returned or logged. No I/O,
source-code execution, parameter search, selection or operational authority.
"""
import csv
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Context, Decimal, localcontext
import hashlib
import io
import json
import re

from .independent_reversal import preview_independent_reversal
from .independent_signals import preview_independent_sma
from .research_data import MAX_BYTES, ResearchDataError
from .research_execution import _decimal


MAX_ROWS = 2048
ERROR = 'independent validation blocked; explicit bounded research evidence required'
UNVERIFIED = ('provider_authenticity', 'adjustments_splits_and_dividends',
    'independent_holdout_and_selection_history', 'overfitting_and_survivorship',
    'sample_adequacy_and_regime_coverage', 'position_concentration_and_gap_risk_review',
    'full_signal_causality', 'actual_forward_paper')
RUNNERS = {'independent_sma_candidate_v1': preview_independent_sma,
           'independent_daily_reversal_v1': preview_independent_reversal}
METRICS = ('initial_cash', 'cash', 'equity', 'units', 'cumulative_pnl',
    'realized_pnl', 'unrealized_pnl', 'cumulative_commission',
    'cumulative_simulated_fills', 'pending_close_change', 'max_close_drawdown_fraction')


@dataclass(frozen=True)
class ValidationCheck:
    name: str
    status: str  # passed / held / insufficient, for this named check only
    reason: str
    evidence_sha256: str | None = None


@dataclass(frozen=True)
class ValidationResult:
    status: str
    origin: str
    data_kind: str  # synthetic / historical; neither is forward Paper
    checks: tuple[ValidationCheck, ...]
    input_sha256: str | None = None
    scenarios: tuple = field(default=(), repr=False)
    unresolved: tuple = UNVERIFIED
    scope: str = 'bounded_local_daily_rule_replay_only'
    source_equivalence_verified: bool = False
    actual_execution_verified: bool = False
    deployment_approved: bool = False
    new_orders_allowed: bool = False


def _digest(value):
    raw = json.dumps(value, sort_keys=True, separators=(',', ':'),
                     allow_nan=False).encode()
    if len(raw) > 200_000:
        raise ValueError
    return hashlib.sha256(raw).hexdigest()


def _result(origin, data_kind, checks, digest=None, scenarios=(), extra=()):
    origin = origin if type(origin) is str and origin in ('self', 'public') else 'unconfirmed'
    data_kind = data_kind if type(data_kind) is str and data_kind in ('synthetic', 'historical') else 'unconfirmed'
    unresolved = UNVERIFIED + tuple(extra)
    # Mechanical checks can pass while candidate validation remains insufficient.
    # This runner cannot establish the external/holdout/forward evidence above.
    status = 'held' if any(c.status == 'held' for c in checks) else 'insufficient'
    return ValidationResult(status, origin, data_kind, tuple(checks), digest,
                            tuple(scenarios), unresolved)


def _public_scope(candidate, spec, checks):
    """Consume v1 intake without changing its contract or inventing missing data."""
    if (type(candidate) is not dict
            or candidate.get('schema') != 'public_strategy_review_candidate_v1'
            or candidate.get('origin') != 'public'):
        raise ValueError
    refresh = candidate.get('needs_source_refresh', False)
    if type(refresh) is not bool:
        raise ValueError
    projection = {k: candidate.get(k) for k in ('assets', 'timeframe',
                  'fill_price_mode', 'definition_sha256', 'evidence_sha256')}
    for key in ('definition_sha256', 'evidence_sha256'):
        value = projection[key]
        if value is not None and (type(value) is not str or not re.fullmatch('[0-9a-f]{64}', value)):
            raise ValueError
    assets = projection['assets']
    if (type(assets) is not list or not 1 <= len(assets) <= 100
            or any(type(x) is not str for x in assets)):
        raise ValueError
    if refresh:
        checks.append(ValidationCheck('source_freshness', 'insufficient', 'source_refresh_required'))
        return projection, False
    if spec is not None and assets != [spec['symbol']]:
        checks.append(ValidationCheck('source_scope', 'held', 'source_asset_scope_differs'))
        return projection, False
    if projection['timeframe'] is None or projection['fill_price_mode'] is None:
        checks.append(ValidationCheck('source_execution', 'insufficient',
                                      'source_bar_or_fill_timing_unconfirmed'))
        return projection, False
    if (projection['timeframe'] != '1d'
            or projection['fill_price_mode'] != 'next_session_open'):
        checks.append(ValidationCheck('source_execution', 'held',
                                      'source_daily_next_open_conditions_differ'))
        return projection, False
    checks.append(ValidationCheck('source_execution', 'insufficient',
        'bar_and_fill_projection_match_but_timezone_rules_and_provider_version_unverified',
        _digest(projection)))
    return projection, True


def _summary(manifest, accounting, costs):
    with localcontext(Context(prec=28)):
        pnl = _decimal(accounting['cumulative_pnl'])
        residual = _decimal(accounting['realized_pnl']) + _decimal(accounting['unrealized_pnl']) - pnl
        if abs(residual) > Decimal('1e-20'):
            raise ResearchDataError(ERROR)
        return dict(costs=costs, local_rule_version=manifest['version'],
            calculation_scope={k: manifest[k] for k in ('symbol', 'first_day', 'last_day',
                'label_convention', 'initial_cash')},
            signal_start_day=accounting['trade_from'],
            market='US_equity', bar_size='1d', exchange_timezone='America/New_York',
            execution_model='daily_close_signal_next_session_open_long_cash',
            drawdown_definition='close_to_running_peak_fraction_including_initial_cash',
            metrics={k: accounting[k] for k in METRICS},
            account_return_fraction=str(pnl / _decimal(accounting['initial_cash'])),
            return_denominator='entire_initial_hypothetical_cash',
            fill_count_definition='executed_order_legs_not_completed_round_trips',
            completed_round_trips=None, sharpe=None, win_rate=None, annualized_return=None,
            decisions_sha256=_digest(manifest['close_signals']),
            result_sha256=_digest(dict(manifest=manifest, accounting=accounting)))


def validate_candidate(spec, prices, *, origin, data_kind, verified_us_symbols,
                       now, public_candidate=None, stress_costs=None):
    """Run at most four existing rule/accounting evaluations, without I/O.

    Inputs: an existing explicit SMA/reversal spec and complete private CSV,
    caller-checked US symbols and aware observation time. For public origin,
    pass the unchanged public_strategy_review_candidate_v1 handoff as well.
    spec=None/prices=None records insufficient executable evidence. Arbitrary
    public code is never run. A supplied spec is a LOCAL hypothesis, not proof
    of the source rules. data_kind must explicitly separate synthetic tests
    from historical recalculation; actual forward performance is unsupported.

    Optional stress_costs has exactly commission_bps/slippage_bps, both explicit,
    no smaller than baseline and at least one larger. One baseline replay,
    one exact repeat, one terminal-prefix check and at most one stress run are
    allowed. No rankings or implied source-period/cost equivalence are output.
    Check evidence is hashed; scenarios contain private aggregate accounting,
    never raw bars, source text, raw identifiers, reports or signal rows.
    """
    checks, scenarios, digest, stage = [], [], None, 'input'
    try:
        if (type(origin) is not str or origin not in ('self', 'public')
                or type(data_kind) is not str or data_kind not in ('synthetic', 'historical')
                or not isinstance(now, datetime) or now.tzinfo is None
                or now.utcoffset() is None
                or type(verified_us_symbols) not in (tuple, list, set, frozenset)
                or not 1 <= len(verified_us_symbols) <= 500
                or any(type(x) is not str or not re.fullmatch(r'[A-Z][A-Z0-9.\-]{0,14}', x)
                       for x in verified_us_symbols)):
            raise ValueError
        if origin == 'self' and public_candidate is not None:
            raise ValueError
        if spec is not None:
            if (type(spec) is not dict or spec.get('schema') not in RUNNERS
                    or type(spec.get('price_count')) is not int
                    or not 2 <= spec['price_count'] <= MAX_ROWS
                    or type(prices) is not bytes or not 0 < len(prices) <= MAX_BYTES):
                raise ValueError
            # Snapshot JSON input before any evaluation; do not mutate caller data.
            _digest(spec)
            spec = json.loads(json.dumps(spec, allow_nan=False))
        source = None
        if origin == 'public':
            stage = 'source_scope'
            source, compatible = _public_scope(public_candidate, spec, checks)
            if not compatible:
                return _result(origin, data_kind, checks, extra=('source_reproduction',))
        if spec is None or prices is None:
            checks.append(ValidationCheck('executable_input', 'insufficient',
                                          'explicit_local_rule_and_complete_prices_required'))
            return _result(origin, data_kind, checks, extra=('executable_input',))
        baseline_costs = {k: str(_decimal(spec[k])) for k in ('commission_bps', 'slippage_bps')}
        if stress_costs is not None:
            if type(stress_costs) is not dict or set(stress_costs) != set(baseline_costs):
                raise ValueError
            stress_costs = {k: str(_decimal(stress_costs[k])) for k in baseline_costs}
            if (any(not Decimal(baseline_costs[k]) <= Decimal(stress_costs[k]) < 10000
                    for k in baseline_costs) or stress_costs == baseline_costs):
                raise ValueError
            if not any(Decimal(stress_costs[k]) > Decimal(baseline_costs[k]) for k in baseline_costs):
                raise ValueError
        digest = _digest(dict(spec=spec, origin=origin, data_kind=data_kind,
            source_projection=source, stress_costs=stress_costs,
            verified_us_symbols=sorted(set(verified_us_symbols)),
            observed_at=now.astimezone(timezone.utc).isoformat()))
        runner = RUNNERS[spec['schema']]
        kwargs = dict(verified_us_symbols=verified_us_symbols, now=now)
        stage = 'daily_next_open_accounting'
        manifest, accounting, _ = runner(spec, prices, **kwargs)
        baseline = _summary(manifest, accounting, baseline_costs)
        scenarios.append(baseline)
        checks.append(ValidationCheck('daily_next_open_accounting', 'passed',
            'complete_closed_sessions_explicit_costs_and_accounting_identity',
            baseline['result_sha256']))
        stage = 'exact_replay'
        repeated_manifest, repeated_accounting, _ = runner(spec, prices, **kwargs)
        repeated = _summary(repeated_manifest, repeated_accounting, baseline_costs)
        if repeated != baseline:
            checks.append(ValidationCheck('exact_replay', 'held', 'same_input_result_differs'))
            return _result(origin, data_kind, checks, digest, scenarios)
        checks.append(ValidationCheck('exact_replay', 'passed',
                                      'same_explicit_input_reproduced', baseline['result_sha256']))
        # Remove the terminal session. Earlier decisions must not depend on it.
        # This bounded regression is not a proof of complete signal causality.
        stage = 'terminal_prefix'
        rows = list(csv.reader(io.StringIO(prices.decode('utf-8'), newline='')))
        if len(rows) != spec['price_count'] + 1:
            raise ValueError
        stream = io.StringIO(newline='')
        csv.writer(stream, lineterminator='\n').writerows(rows[:-1])
        prefix_prices = stream.getvalue().encode()
        prefix_spec = dict(spec, price_count=spec['price_count'] - 1,
            price_sha256=hashlib.sha256(prefix_prices).hexdigest(),
            last_day=manifest['close_signals'][-2]['session'])
        prefix, _, _ = runner(prefix_spec, prefix_prices, allow_pending_only=True, **kwargs)
        same = (prefix['close_signals'] == manifest['close_signals'][:-1]
                and prefix['version'] == manifest['version'])
        checks.append(ValidationCheck('terminal_prefix', 'passed' if same else 'held',
            'earlier_decisions_unchanged' if same else 'earlier_decisions_changed',
            _digest(prefix['close_signals'])))
        if stress_costs is None:
            checks.append(ValidationCheck('cost_sensitivity', 'insufficient',
                                          'explicit_stress_costs_not_supplied'))
        else:
            stage = 'cost_sensitivity'
            stressed_spec = dict(spec, **stress_costs)
            m, a, _ = runner(stressed_spec, prices, **kwargs)
            stressed = _summary(m, a, stress_costs)
            scenarios.append(stressed)
            checks.append(ValidationCheck('cost_sensitivity', 'passed',
                'one_explicit_stress_run_not_profitability_or_monotonicity_proof',
                stressed['result_sha256']))
        extra = (('synthetic_results_not_market_performance',) if data_kind == 'synthetic' else ())
        if origin == 'public':
            extra += ('provider_version_and_source_rule_equivalence',)
        return _result(origin, data_kind, checks, digest, scenarios, extra=extra)
    except Exception:
        # Fixed diagnostics only. Never reflect a spec, source, path, URL or raw error.
        checks.append(ValidationCheck(stage, 'held', 'invalid_or_failed_research_input'))
        return _result(origin, data_kind, checks, digest, scenarios)
