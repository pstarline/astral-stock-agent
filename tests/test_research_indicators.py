"""Hand arithmetic, seed boundaries and future-data isolation on synthetic bars."""
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal, localcontext

import pytest

from astral_stock_agent.research_data import DailyBar, DailyHistory, ResearchDataError
from astral_stock_agent.research_indicators import daily_indicators
from astral_stock_agent.research_sessions import UTC_SESSION_DATE


def history(closes=(10, 12, 11, 13, 12, 14)):
    days = (2, 5, 6, 7, 8, 9)
    return DailyHistory('SPY', tuple(
        DailyBar(datetime(2026, 1, day, tzinfo=timezone.utc), Decimal(c),
                 Decimal(c + 1), Decimal(c - 1), Decimal(c), Decimal(100))
        for day, c in zip(days, closes)), 'synthetic')


def calculate(h=None, **changes):
    h = h or history()
    args = dict(ema_period=2, rsi_period=2, atr_period=2, atr_mean_period=2,
                first_day=h.bars[0].timestamp.date(), last_day=h.bars[-1].timestamp.date(),
                now=datetime(2026, 1, 10, tzinfo=timezone.utc), label_convention=UTC_SESSION_DATE)
    args.update(changes)
    return daily_indicators(h, **args)


def test_hand_calculated_seeds_and_wilder_updates():
    result = calculate()
    assert result.ema[:3] == (None, Decimal(11), Decimal(11))
    assert abs(result.ema[3] - Decimal(37) / 3) < Decimal('1e-25')
    assert result.rsi[:2] == (None, None)
    assert abs(result.rsi[2] - Decimal(200) / 3) < Decimal('1e-25')
    assert abs(result.rsi[3] - Decimal(600) / 7) < Decimal('1e-25')
    assert result.atr[:4] == (None, None, Decimal('2.5'), Decimal('2.75'))
    assert result.atr_sma[:4] == (None, None, None, Decimal('2.625'))
    assert not result.source_equivalence_verified
    assert not result.deployment_approved
    assert '2.625' not in repr(result)


@pytest.mark.parametrize('closes,expected', [((10, 11, 12, 13), 100),
                                          ((13, 12, 11, 10), 0),
                                          ((10, 10, 10, 10), None)])
def test_rsi_one_sided_and_undefined_flat(closes, expected):
    result = calculate(history(closes))
    assert result.rsi[-1] == expected


def test_full_warmup_required_without_filling():
    result = calculate(history((10, 12)), ema_period=3)
    assert result.ema == result.rsi == result.atr == result.atr_sma == (None, None)


def test_future_values_and_extension_do_not_change_prefix():
    h = history()
    full = calculate(h)
    changed = replace(h, bars=h.bars[:-1] + (replace(h.bars[-1], open=Decimal(30),
        high=Decimal(31), low=Decimal(29), close=Decimal(30)),))
    altered = calculate(changed)
    for field in ('ema', 'rsi', 'atr', 'atr_sma'):
        assert getattr(full, field)[:-1] == getattr(altered, field)[:-1]
        for length in (2, 3, 4, 5):
            prefix = calculate(replace(h, bars=h.bars[:length]))
            assert getattr(prefix, field) == getattr(full, field)[:length]


def test_gap_true_range_uses_previous_close():
    h = history((10, 20, 21))
    # First TR is max(2, 11, 9)=11, not the intrabar range2; next TR=2.
    assert calculate(h).atr[-1] == Decimal('6.5')


def test_decimal_context_and_input_are_preserved():
    h = history()
    expected = calculate(h)
    with localcontext() as ctx:
        ctx.prec = 3
        ctx.rounding = 'ROUND_DOWN'
        assert calculate(h) == expected
    assert h == history()


@pytest.mark.parametrize('field', ['ema_period', 'rsi_period', 'atr_period', 'atr_mean_period'])
@pytest.mark.parametrize('value', [True, 1, 253, '2', 2.0])
def test_invalid_periods(field, value):
    with pytest.raises(ResearchDataError):
        calculate(**{field: value})


@pytest.mark.parametrize('change', [dict(close=Decimal('NaN')), dict(high=Decimal(1)),
                                    dict(low=Decimal(0)), dict(volume=Decimal(-1))])
def test_malformed_prices_are_not_hidden_by_smoothing(change):
    h = history()
    h = replace(h, bars=h.bars[:-1] + (replace(h.bars[-1], **change),))
    with pytest.raises(ResearchDataError):
        calculate(h)


def test_missing_intraday_or_unclosed_bars_block_calculation():
    h = history()
    with pytest.raises(ResearchDataError):
        calculate(replace(h, bars=h.bars[:2] + h.bars[3:]))
    intraday = replace(h.bars[0], timestamp=h.bars[0].timestamp.replace(hour=12))
    with pytest.raises(ResearchDataError):
        calculate(replace(h, bars=(intraday,) + h.bars[1:]))
    with pytest.raises(ResearchDataError):
        calculate(now=datetime(2026, 1, 9, 14, tzinfo=timezone.utc))
