"""Separate daily, fixed-unit hypothesis; never an intraday source replica."""
from dataclasses import dataclass, field
from datetime import date, timezone
from decimal import Context, Decimal, localcontext
from zoneinfo import ZoneInfo

from .research_data import ResearchDataError
from .research_execution import _decimal
from .research_indicators import daily_indicators
from .research_sessions import UTC_SESSION_DATE


REVERSAL_RULE = 'daily_close_reversal_fixed_units_v1'
PERIODS = ('ema_period', 'rsi_period', 'atr_period', 'atr_mean_period')
RULE_FIELDS = set(PERIODS) | {
    'id', 'units', 'trade_from', 'entry_rsi', 'exit_rsi',
    'stop_loss_fraction', 'take_profit_fraction', 'max_holding_sessions',
}


@dataclass(frozen=True)
class ReversalDecisions:
    changes: tuple = field(repr=False)
    reasons: tuple = field(repr=False)
    source_equivalence_verified: bool = False
    intrabar_protection_verified: bool = False
    deployment_approved: bool = False


def validate_reversal_rule(rule):
    """No thresholds, holding limits, periods or sizing are selected here."""
    try:
        if type(rule) is not dict or set(rule) != RULE_FIELDS or rule['id'] != REVERSAL_RULE:
            raise ValueError
        if any(type(rule[k]) is not int or not 2 <= rule[k] <= 252 for k in PERIODS):
            raise ValueError
        if (type(rule['max_holding_sessions']) is not int
                or not 1 <= rule['max_holding_sessions'] <= 252):
            raise ValueError
        numeric = {k: _decimal(rule[k]) for k in ('units', 'entry_rsi', 'exit_rsi',
                   'stop_loss_fraction', 'take_profit_fraction')}
        if (numeric['units'] <= 0 or not 0 <= numeric['entry_rsi'] < numeric['exit_rsi'] <= 100
                or not 0 < numeric['stop_loss_fraction'] < 1
                or not 0 < numeric['take_profit_fraction'] < 1):
            raise ValueError
        start = date.fromisoformat(rule['trade_from'])
        if start.isoformat() != rule['trade_from']:
            raise ValueError
        return dict(rule, **numeric, trade_from=start)
    except Exception:
        raise ResearchDataError('explicit separate daily reversal rule required') from None


def reversal_close_decisions(history, rule, *, slippage_bps, first_day, last_day,
                              now, label_convention, allow_pending_only=False):
    """Use only each observed close and previously available execution opens.

    Enter fixed units iff RSI < entry_rsi, close < EMA, ATR > its SMA.
    While held, exits have priority: close-based loss, close-based profit,
    holding-session limit, RSI > exit_rsi, or unavailable required indicators.
    All changes execute at the NEXT open with explicit adverse slippage. Loss/
    profit thresholds use that observed entry open plus buy slippage, excluding
    commissions. They are close triggers, NOT stop/limit/OCO orders or maximum
    loss guarantees. Ignore intrabar touches, including bars touching both.
    Count the entry-fill session as holding session1. Never pyramid or short;
    after an exit fill do not re-enter at that same session's close. Keep the
    final signal pending. Cash/fees/fills remain the existing engine's job.
    """
    rule = validate_reversal_rule(rule)
    slip = _decimal(slippage_bps)
    if type(allow_pending_only) is not bool or not 0 <= slip < 10000:
        raise ResearchDataError('explicit daily execution policy required')
    if not first_day <= rule['trade_from'] <= last_day:
        raise ResearchDataError('daily reversal evaluation date outside history')
    features = daily_indicators(history, **{k: rule[k] for k in PERIODS},
        first_day=first_day, last_day=last_day, now=now, label_convention=label_convention)
    zone = timezone.utc if label_convention == UTC_SESSION_DATE else ZoneInfo('America/New_York')
    with localcontext(Context(prec=28)):
        changes, reasons = [], []
        held, pending, age, entry, first_ready = False, Decimal(0), 0, None, None
        size = rule['units']
        for index, bar in enumerate(history.bars):
            exited_today = pending < 0
            if pending > 0:
                held, age = True, 0
                entry = _decimal(bar.open) * (1 + slip / 10000)
            elif pending < 0:
                held, age, entry = False, 0, None
            if held:
                age += 1
            change, reason = Decimal(0), 'flat'
            ready = all(getattr(features, k)[index] is not None for k in ('ema', 'rsi', 'atr', 'atr_sma'))
            close = _decimal(bar.close)
            if bar.timestamp.astimezone(zone).date() < rule['trade_from']:
                reason = 'before_start'
            elif not ready:
                change, reason = (-size, 'missing_indicator_exit') if held else (Decimal(0), 'warmup')
            else:
                if first_ready is None:
                    first_ready = index
                if held:
                    reason = 'hold'
                    if close <= entry * (1 - rule['stop_loss_fraction']):
                        reason = 'close_loss_exit'
                    elif close >= entry * (1 + rule['take_profit_fraction']):
                        reason = 'close_profit_exit'
                    elif age >= rule['max_holding_sessions']:
                        reason = 'holding_limit_exit'
                    elif features.rsi[index] > rule['exit_rsi']:
                        reason = 'rsi_exit'
                    if reason != 'hold':
                        change = -size
                elif exited_today:
                    reason = 'exit_fill_cooldown'
                elif (features.rsi[index] < rule['entry_rsi'] and close < features.ema[index]
                      and features.atr[index] > features.atr_sma[index]):
                    change, reason = size, 'entry'
            pending = change
            changes.append(change)
            reasons.append(reason)
        if first_ready is None or (first_ready == len(changes) - 1 and not allow_pending_only):
            raise ResearchDataError('no prepared daily reversal evaluation session')
        return ReversalDecisions(tuple(changes), tuple(reasons))
