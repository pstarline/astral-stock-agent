"""Reproduce a private SMA curve comparison from complete recorded evidence.

No network, credentials, orders, persistence or deployment authority. Research
parameters are caller assumptions, not verified settings of the external run.
"""
from datetime import date

from .research_audit import compare_backtest_curve
from .research_data import ResearchDataError, read_daily_artifact
from .research_trend import compare_sma_candidate


def review_sma_evidence(manifest, price_bytes, *, verified_us_symbols, now):
    """Compose existing data, session, next-open and complete-curve checks.

    Dates and comparison boundaries must be explicit. Never shift external
    labels, infer a cost assumption, trim history or execute a terminal signal
    to make curves match. Even equality cannot certify external strategy rules,
    prices, costs, corporate actions or actual Paper execution.
    """
    try:
        if type(manifest['version']) is not int or manifest['version'] != 1:
            raise ValueError
        spec = manifest['research']
        if spec['symbol'] not in set(verified_us_symbols):
            raise ValueError
        history = read_daily_artifact(price_bytes, manifest['price_response'],
            symbol=spec['symbol'], start=spec['start'], end=spec['end'])
        comparison = compare_sma_candidate(history, window=spec['window'], units=spec['units'],
            trade_from=date.fromisoformat(spec['trade_from']), initial_cash=spec['initial_cash'],
            commission_bps=spec['commission_bps'], slippage_bps=spec['slippage_bps'],
            first_day=date.fromisoformat(spec['first_day']), last_day=date.fromisoformat(spec['last_day']),
            now=now, label_convention=spec['label_convention'])
        expected = manifest['expected_run']
        boundary = expected['first_compared_index']
        # Comparison may omit warmup labels, never local decisions or fills.
        # Requiring the same first eligible close prevents a hidden pre-run
        # position from being treated as an identical starting condition.
        if type(boundary) is not int or boundary != comparison.decisions.first_eligible_index:
            raise ValueError
        audit = compare_backtest_curve(history, comparison.candidate, manifest['backtest_response'],
            expected_backtest_id=expected['backtest_id'], expected_strategy_id=expected['strategy_id'],
            first_compared_index=boundary)
        return {'kind': 'hypothetical_sma_curve_review',
                'compared_points': audit.compared_points,
                'mismatched_points': audit.mismatched_points,
                'max_absolute_gap_usd': str(audit.max_absolute_gap_usd),
                'curves_match': audit.curves_match,
                'hypothetical_fill_count': len(comparison.candidate.fills),
                'pending_final_decision': comparison.decisions.pending_final_change != 0,
                'external_rules_and_costs_verified': False,
                'full_strategy_parity_verified': False, 'actual_execution_verified': False,
                'deployment_approved': False, 'new_orders_allowed': False}
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        raise ResearchDataError('complete research evidence and explicit matching scope required') from None
