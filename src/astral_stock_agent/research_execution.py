"""Price-only research simulation, with no broker, Paper or reporting authority.

Unit changes are decisions supplied after each daily close. They execute only
at the next scheduled session's open, with adverse slippage and commissions.
This is a calculation model, never evidence of real orders or actual fills.
"""
from dataclasses import dataclass
from decimal import Context, Decimal, DecimalException, localcontext

from .research_data import DailyBar, ResearchDataError
from .research_sessions import require_daily_session_coverage


@dataclass(frozen=True)
class HypotheticalFill:
    signal_index: int
    execution_index: int
    units_change: Decimal
    price: Decimal
    commission: Decimal


@dataclass(frozen=True)
class HypotheticalDailyMark:
    """Scheduled bar-close mark, indexed into the supplied research history."""
    bar_index: int
    cash: Decimal
    units: Decimal
    close: Decimal
    equity: Decimal
    close_drawdown_fraction: Decimal


@dataclass(frozen=True)
class PriceOnlySimulation:
    cash: Decimal
    units: Decimal
    final_marked_equity: Decimal
    net_price_pnl: Decimal
    commissions: Decimal
    fills: tuple[HypotheticalFill, ...]
    kind: str = 'hypothetical_price_only'
    actual_execution_verified: bool = False
    signal_causality_verified: bool = False
    corporate_actions_verified: bool = False
    deployment_approved: bool = False
    daily_marks: tuple[HypotheticalDailyMark, ...] = ()
    max_close_drawdown_fraction: Decimal = Decimal(0)


def _decimal(value):
    if type(value) not in (Decimal, int, str):
        raise ResearchDataError('explicit finite decimal research input required')
    try:
        result = Decimal(value)
        if not result.is_finite() or (result and abs(result.adjusted()) > 12):
            raise ValueError
        return result
    except (ValueError, DecimalException):
        raise ResearchDataError('research number outside calculation bounds') from None


def simulate_next_open(history, unit_changes, *, initial_cash, commission_bps,
                       slippage_bps, first_day, last_day, now, label_convention):
    """Apply close decisions at the following open; never liquidate implicitly.

    All decisions, hypothetical cash and costs must be explicit. Units cannot
    go short and cash cannot go negative; insufficient resources reject the
    simulation rather than inventing partial fills. Each scheduled close is
    marked, including no-trade sessions. Drawdown is relative to the running
    peak (including initial cash), as a fraction, not percent. It does not
    measure intraday or opening-gap drawdown or approve a risk threshold.
    The final close is a mark, not a sell. Dividends, splits, spread variability, liquidity, fractional-unit
    restrictions, taxes and actual broker timing are not modeled. Calendar
    coverage is required. Caller-supplied signals may still contain future
    information: this function cannot certify their origin or strategy quality.
    """
    require_daily_session_coverage(history, first_day=first_day, last_day=last_day,
                                  now=now, label_convention=label_convention)
    if type(unit_changes) is not tuple or len(unit_changes) != len(history.bars):
        raise ResearchDataError('one explicit close decision per bar required')
    cash = starting_cash = _decimal(initial_cash)
    commission = _decimal(commission_bps)
    slip = _decimal(slippage_bps)
    changes = tuple(_decimal(x) for x in unit_changes)
    if cash <= 0 or not 0 <= commission < 10000 or not 0 <= slip < 10000:
        raise ResearchDataError('invalid hypothetical cash or cost assumptions')
    if changes[-1] != 0:
        raise ResearchDataError('last close decision has no next session open')
    # Recheck numeric invariants even for manually constructed dataclasses.
    for bar in history.bars:
        if not isinstance(bar, DailyBar):
            raise ResearchDataError('invalid research bar')
        op, high, low, close = (_decimal(getattr(bar, k)) for k in ('open', 'high', 'low', 'close'))
        if min(op, high, low, close) <= 0 or low > min(op, close) or high < max(op, close):
            raise ResearchDataError('invalid research OHLC')
        if _decimal(bar.volume) < 0:
            raise ResearchDataError('invalid research volume')
    units, fees, fills = Decimal(0), Decimal(0), []
    try:
        # Isolate arithmetic from the caller's ambient Decimal precision.
        with localcontext(Context(prec=28)):
            marks = [HypotheticalDailyMark(0, cash, units, _decimal(history.bars[0].close),
                                           cash, Decimal(0))]
            peak, max_drawdown = starting_cash, Decimal(0)
            for index in range(1, len(history.bars)):
                change = changes[index - 1]
                if change:
                    price = _decimal(history.bars[index].open) * (1 + (slip if change > 0 else -slip) / 10000)
                    fee = abs(change) * price * commission / 10000
                    next_cash = cash - change * price - fee
                    next_units = units + change
                    if next_cash < 0 or next_units < 0:
                        raise ResearchDataError('hypothetical cash or holdings insufficient')
                    cash, units = next_cash, next_units
                    fees += fee
                    fills.append(HypotheticalFill(index - 1, index, change, price, fee))
                close = _decimal(history.bars[index].close)
                equity = cash + units * close
                peak = max(peak, equity)
                drawdown = (peak - equity) / peak
                max_drawdown = max(max_drawdown, drawdown)
                marks.append(HypotheticalDailyMark(index, cash, units, close, equity, drawdown))
            equity = marks[-1].equity
            return PriceOnlySimulation(cash, units, equity, equity - starting_cash, fees, tuple(fills),
                                       daily_marks=tuple(marks), max_close_drawdown_fraction=max_drawdown)
    except DecimalException:
        raise ResearchDataError('research calculation failed') from None
