"""Pair two explicit local rule hypotheses on identical research conditions.

No source intake, external platform, ranking, selection, I/O or deployment.
Source-inspired hypotheses must first be expressed as a supported local rule;
this comparison never establishes equivalence to an external strategy.
"""
from dataclasses import dataclass, field
from decimal import Context, Decimal, localcontext

from .independent_validation import validate_candidate, _digest, RUNNERS
from .research_execution import _decimal


SCOPE = ('symbol', 'price_sha256', 'price_count', 'first_day', 'last_day',
         'label_convention', 'initial_cash', 'commission_bps', 'slippage_bps')
NUMERIC_SCOPE = ('initial_cash', 'commission_bps', 'slippage_bps')
LIMITATIONS = ('local_hypotheses_not_source_reproduction',
    'different_rules_may_have_different_exposure', 'no_independent_holdout_evidence',
    'no_sample_adequacy_or_profitability_verdict', 'no_forward_paper_evidence')


@dataclass(frozen=True)
class PairedResearch:
    status: str
    reason: str
    evidence_sha256: str | None = None
    candidates: tuple = field(default=(), repr=False)
    differences: tuple = field(default=(), repr=False)
    limitations: tuple = LIMITATIONS
    scope: str = 'two_local_daily_rules_same_input_conditions'
    selection_approved: bool = False
    deployment_approved: bool = False
    new_orders_allowed: bool = False


def compare_local_candidates(specs, prices, *, data_kind, verified_us_symbols,
                             now, stress_costs=None):
    """Exactly two local specs, one CSV, at most eight bounded evaluations.

    Both candidates start flat on the same signal-start date and use the same
    full history, initial cash, explicit costs and next-session-open engine.
    Rule parameters/sizing may differ; matching conditions do not equalize risk.
    Optional one shared higher-cost scenario uses the existing validator.
    Differences are right minus left, in supplied order, never a winner/ranking.
    Overall status remains insufficient even when arithmetic can be compared.
    The caller must keep private aggregate outputs and evidence context private.
    """
    results = ()
    try:
        if type(specs) not in (tuple, list) or len(specs) != 2:
            raise ValueError
        # Explicit scope admission happens before either candidate is evaluated.
        scopes = []
        for spec in specs:
            if type(spec) is not dict or spec.get('schema') not in RUNNERS:
                raise ValueError
            scope = {k: spec[k] for k in SCOPE}
            for k in NUMERIC_SCOPE:
                scope[k] = _decimal(scope[k])
            scope['trade_from'] = spec['rule']['trade_from']
            scopes.append(scope)
        if scopes[0] != scopes[1]:
            return PairedResearch('held', 'different_explicit_comparison_conditions')
        # Each result retains its own input, rule-version and decision evidence.
        # 'self' here means executable local hypothesis, not source provenance.
        results = tuple(validate_candidate(s, prices, origin='self', data_kind=data_kind,
            verified_us_symbols=verified_us_symbols, now=now, stress_costs=stress_costs)
            for s in specs)
        if any(r.status == 'held' or not r.scenarios for r in results):
            return PairedResearch('held', 'candidate_checks_block_comparison', candidates=results)
        if any(any(c.status == 'held' for c in r.checks) for r in results):
            raise ValueError
        if len(results[0].scenarios) != len(results[1].scenarios):
            raise ValueError
        differences = []
        with localcontext(Context(prec=28)):
            for left, right in zip(*(r.scenarios for r in results)):
                ls, rs = left['calculation_scope'], right['calculation_scope']
                if (any(Decimal(left['costs'][k]) != Decimal(right['costs'][k])
                        for k in ('commission_bps', 'slippage_bps'))
                        or any(ls[k] != rs[k] for k in ls if k != 'initial_cash')
                        or Decimal(ls['initial_cash']) != Decimal(rs['initial_cash'])
                        or left['signal_start_day'] != right['signal_start_day']):
                    raise ValueError
                differences.append(dict(costs=dict(left['costs']),
                    definition='right_minus_left_not_ranking',
                    cumulative_pnl=str(Decimal(right['metrics']['cumulative_pnl'])
                                       - Decimal(left['metrics']['cumulative_pnl'])),
                    account_return_fraction=str(Decimal(right['account_return_fraction'])
                                                - Decimal(left['account_return_fraction'])),
                    max_close_drawdown_fraction=str(
                        Decimal(right['metrics']['max_close_drawdown_fraction'])
                        - Decimal(left['metrics']['max_close_drawdown_fraction']))))
        evidence = _digest(dict(inputs=[r.input_sha256 for r in results],
                                differences=differences, contract='paired_local_rules_v1'))
        return PairedResearch('insufficient', 'same_conditions_arithmetic_only', evidence,
                              results, tuple(differences))
    except Exception:
        # Never reflect raw specs, source text, prices, paths or exceptions.
        return PairedResearch('held', 'invalid_or_failed_paired_research', candidates=results)
