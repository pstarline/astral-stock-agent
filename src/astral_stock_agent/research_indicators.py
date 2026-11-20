"""Explicit daily indicator inputs, without strategy or execution authority.

This independently defined seed contract is NOT Astral indicator parity. A
15-minute/on-close strategy cannot be reproduced by feeding it daily values.
Use a fixed history start for comparisons: recursive seeds depend on it.
"""
from dataclasses import dataclass, field
from decimal import Context, Decimal, localcontext

from .research_data import ResearchDataError
from .research_execution import _decimal
from .research_sessions import require_daily_session_coverage


@dataclass(frozen=True)
class DailyIndicators:
    # Recursive means contain prices; suppress them in diagnostic repr/logs.
    ema: tuple = field(repr=False)
    rsi: tuple = field(repr=False)
    atr: tuple = field(repr=False)
    atr_sma: tuple = field(repr=False)
    method: str = 'daily_seeded_indicators_v1'
    source_equivalence_verified: bool = False
    deployment_approved: bool = False


def _smooth(values, period, *, ema=False):
    """Seed with the first period values; only causal suffix updates follow."""
    result = [None] * len(values)
    if len(values) < period:
        return tuple(result)
    value = sum(values[:period], Decimal(0)) / period
    result[period - 1] = value
    alpha = Decimal(2) / (period + 1) if ema else Decimal(1) / period
    for index in range(period, len(values)):
        value = value + alpha * (values[index] - value)
        result[index] = value
    return tuple(result)


def daily_indicators(history, *, ema_period, rsi_period, atr_period,
                     atr_mean_period, first_day, last_day, now, label_convention):
    """Validate closed daily coverage and return label-aligned Decimal series.

    EMA starts at index n-1 using an n-close SMA. RSI/ATR use n actual
    changes/true ranges, so first values occur at index n. TR needs the prior
    close; the initial bar is never substituted for that missing observation.
    Subsequent RSI gain/loss and ATR use alpha=1/n Wilder smoothing. ATR-SMA
    requires its full explicit window of available ATR values. Warmup and RSI
    with both average gain/loss zero remain None, never zero-filled/ffilled.
    Periods are caller inputs; no source rules, thresholds, positions or costs
    are chosen. This function has no network, storage, report or order access.
    """
    periods = (ema_period, rsi_period, atr_period, atr_mean_period)
    if any(type(p) is not int or not 2 <= p <= 252 for p in periods):
        raise ResearchDataError('explicit daily indicator periods required')
    require_daily_session_coverage(history, first_day=first_day, last_day=last_day,
                                   now=now, label_convention=label_convention)
    with localcontext(Context(prec=28)):
        closes, ranges, gains, losses = [], [], [], []
        for bar in history.bars:
            op, high, low, close = (_decimal(getattr(bar, k))
                                    for k in ('open', 'high', 'low', 'close'))
            if (min(op, low, close) <= 0 or low > min(op, close)
                    or high < max(op, close) or _decimal(bar.volume) < 0):
                raise ResearchDataError('valid daily indicator OHLCV required')
            if closes:
                previous = closes[-1]
                ranges.append(max(high - low, abs(high - previous), abs(low - previous)))
                gains.append(max(close - previous, Decimal(0)))
                losses.append(max(previous - close, Decimal(0)))
            closes.append(close)
        ema_values = _smooth(closes, ema_period, ema=True)
        avg_gain, avg_loss = _smooth(gains, rsi_period), _smooth(losses, rsi_period)
        rsi_values = [None]
        for gain, loss in zip(avg_gain, avg_loss):
            rsi_values.append(None if gain is None or gain + loss == 0
                              else Decimal(100) * gain / (gain + loss))
        atr_values = (None,) + _smooth(ranges, atr_period)
        means = [None] * len(closes)
        for index in range(atr_period + atr_mean_period - 1, len(closes)):
            window = atr_values[index - atr_mean_period + 1:index + 1]
            means[index] = sum(window, Decimal(0)) / atr_mean_period
        return DailyIndicators(ema_values, tuple(rsi_values), atr_values, tuple(means))
