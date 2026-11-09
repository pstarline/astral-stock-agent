"""Compare complete daily research curves; never approve a strategy or deploy.

Curve equality is a diagnostic, not proof of matching signals, executions,
costs, corporate actions or data provenance. No raw rows/IDs leave the audit.
"""
from dataclasses import dataclass
from decimal import Context, Decimal, localcontext

from .research_data import DailyHistory, ResearchDataError, _timestamp
from .research_execution import PriceOnlySimulation, _decimal


CURVE_TOLERANCE_USD = Decimal('0.0000001')


@dataclass(frozen=True)
class EquityCurveAudit:
    compared_points: int
    mismatched_points: int
    max_absolute_gap_usd: Decimal
    curves_match: bool
    full_strategy_parity_verified: bool = False
    actual_execution_verified: bool = False
    deployment_approved: bool = False


def compare_backtest_curve(history, simulation, response, *, expected_backtest_id,
                           expected_strategy_id, first_compared_index):
    """Compare every post-warmup label without reindexing or dropping dates.

    Caller explicitly chooses a local comparison boundary; it must equal the
    completed run's first eligible label. This does not align the two engines'
    warmup or change local decisions. The full supplied local daily path must
    cover the external run's raw history. Only a complete, healthy, successful
    daily result for the exact requested run is admitted. Float serialization
    noise up to 1e-7 USD is tolerated, independently of ambient Decimal context.
    """
    try:
        if (not isinstance(history, DailyHistory) or not isinstance(simulation, PriceOnlySimulation)
                or not isinstance(response, dict) or response.get('isError')
                or not isinstance(expected_backtest_id, str) or not expected_backtest_id
                or type(expected_strategy_id) is not int or expected_strategy_id <= 0
                or type(first_compared_index) is not int
                or not 0 <= first_compared_index < len(history.bars) <= 50_000
                or len(simulation.daily_marks) != len(history.bars)):
            raise ValueError
        result = response.get('structuredContent', response)
        if (result['backtest_id'] != expected_backtest_id
                or type(result['strategy_id']) is not int
                or result['strategy_id'] != expected_strategy_id
                or result['backtest_status'] != 'succeeded'
                or result['health_state'] != 'ok' or result['degraded'] is not False
                or any(result.get(k, False) is not False
                       for k in ('results_truncated', 'response_truncated'))):
            raise ValueError
        results = result['results']
        if (results['portfolio_value_downsampled'] is not False
                or results['health_state'] != 'ok' or results['degraded'] is not False):
            raise ValueError
        labels = tuple(_timestamp(b.timestamp.isoformat()) for b in history.bars)
        if any(a >= b for a, b in zip(labels, labels[1:])):
            raise ValueError
        period = results['backtest_period']
        if (period['bar_size'] != '1d' or period['history_capped'] is not False
                or _timestamp(period['raw_data_start']) != labels[0]
                or _timestamp(period['raw_data_end']) != labels[-1]
                or _timestamp(period['first_eligible_trade_at']) != labels[first_compared_index]):
            raise ValueError
        raw_curve = results['portfolio_value']
        if not isinstance(raw_curve, dict) or len(raw_curve) != len(labels) - first_compared_index:
            raise ValueError
        observed = {}
        for key, value in raw_curve.items():
            stamp = _timestamp(key)
            if stamp in observed or type(value) not in (str, int, float, Decimal):
                raise ValueError
            observed[stamp] = _decimal(str(value))
            if observed[stamp] <= 0:
                raise ValueError
        if set(observed) != set(labels[first_compared_index:]):
            raise ValueError
        expected = {}
        for i, mark in enumerate(simulation.daily_marks):
            if type(mark.bar_index) is not int or mark.bar_index != i:
                raise ValueError
            value = _decimal(mark.equity)
            if value <= 0:
                raise ValueError
            expected[labels[i]] = value
        with localcontext(Context(prec=28)):
            gaps = [abs(expected[stamp] - value) for stamp, value in observed.items()]
            mismatches = sum(gap > CURVE_TOLERANCE_USD for gap in gaps)
            return EquityCurveAudit(len(gaps), mismatches, max(gaps), mismatches == 0)
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        raise ResearchDataError('complete matching-scope backtest curve required') from None
