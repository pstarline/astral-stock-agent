"""Independent price-only simulated daily accounting; no Astral dependency.

This preview accepts explicit close signals and CSV prices. It is not a market
feed, a validated strategy, an operational ledger, or evidence of broker fills.
"""
from datetime import date, timezone
from zoneinfo import ZoneInfo
from decimal import Context, Decimal, localcontext

from .delivery import validate_telegram_text
from .research_data import ResearchDataError, read_daily_csv
from .research_execution import _decimal, simulate_next_open
from .research_sessions import UTC_SESSION_DATE, NY_SESSION_DATE


MANIFEST_FIELDS = {
    'schema', 'strategy', 'version', 'symbol', 'price_sha256', 'price_count',
    'first_day', 'last_day', 'label_convention', 'initial_cash',
    'commission_bps', 'slippage_bps', 'close_signals',
}


def _label(value):
    # Only a local descriptive name/version, never account IDs or source code.
    if type(value) is not str or not 1 <= len(value) <= 80 or any(
            not (c.isalnum() or c in ' ._-') for c in value):
        raise ResearchDataError('simple strategy label and version required')
    return value


def preview_independent_paper(manifest, prices, *, verified_us_symbols, now):
    """Compute latest completed-session accounting from a complete fixed run.

    Every signal is supplied explicitly, matched by session date, and executes
    at the following open only. The final signal is pending. No optimization,
    zero-cost default, fabricated Astral envelope, or source-authenticity claim.
    This replays history; extending it is NOT a persistent forward Paper run.
    """
    if (type(manifest) is not dict or set(manifest) != MANIFEST_FIELDS
            or manifest['schema'] != 'independent_price_simulation_v1'):
        raise ResearchDataError('explicit independent simulation manifest required')
    strategy, version = _label(manifest['strategy']), _label(manifest['version'])
    symbol = manifest['symbol']
    if (type(verified_us_symbols) not in (tuple, list, set, frozenset)
            or symbol not in verified_us_symbols):
        raise ResearchDataError('independently checked US equity scope required')
    try:
        first, last = (date.fromisoformat(manifest[k]) for k in ('first_day', 'last_day'))
    except (TypeError, ValueError):
        raise ResearchDataError('explicit session dates required') from None
    convention = manifest['label_convention']
    if convention not in (UTC_SESSION_DATE, NY_SESSION_DATE):
        raise ResearchDataError('explicit supported daily label convention required')
    history = read_daily_csv(prices, symbol=symbol, expected_sha256=manifest['price_sha256'],
                             expected_count=manifest['price_count'],
                             start=first.isoformat() + 'T00:00:00Z',
                             end=last.isoformat() + 'T23:59:59Z')
    zone = timezone.utc if convention == UTC_SESSION_DATE else ZoneInfo('America/New_York')
    days = tuple(b.timestamp.astimezone(zone).date().isoformat() for b in history.bars)
    signals = manifest['close_signals']
    if (type(signals) is not list or len(signals) != len(days)
            or any(type(s) is not dict or set(s) != {'session', 'units_change'}
                   or s['session'] != day for s, day in zip(signals, days))):
        raise ResearchDataError('one ordered explicit close signal per session required')
    changes = tuple(_decimal(s['units_change']) for s in signals)
    result = simulate_next_open(history, changes[:-1] + (Decimal(0),),
                               initial_cash=manifest['initial_cash'],
                               commission_bps=manifest['commission_bps'],
                               slippage_bps=manifest['slippage_bps'],
                               first_day=first, last_day=last, now=now,
                               label_convention=convention)
    with localcontext(Context(prec=28)):
        initial = _decimal(manifest['initial_cash'])
        basis, units, realized, before_realized = (Decimal(0),) * 4
        for fill in result.fills:
            if fill.execution_index == len(days) - 1:
                before_realized = realized
            if fill.units_change > 0:
                basis += fill.units_change * fill.price + fill.commission
            else:
                removed = basis * (-fill.units_change) / units
                realized += (-fill.units_change) * fill.price - fill.commission - removed
                basis -= removed
            units += fill.units_change
            if fill.execution_index < len(days) - 1:
                before_realized = realized
        latest = result.daily_marks[-1]
        previous_equity = result.daily_marks[-2].equity if len(days) > 1 else initial
        day_realized = realized - before_realized
        day_pnl = latest.equity - previous_equity
        unrealized = units * history.bars[-1].close - basis
        todays_fills = tuple(f for f in result.fills if f.execution_index == len(days) - 1)
        accounting = {
            'kind': 'independent_price_only_simulation', 'session': days[-1],
            'strategy': strategy, 'version': version, 'symbol': symbol,
            'initial_cash': str(initial), 'cash': str(result.cash), 'units': str(units),
            'equity': str(latest.equity), 'daily_pnl': str(day_pnl),
            'cumulative_pnl': str(latest.equity - initial),
            'realized_pnl': str(realized), 'unrealized_pnl': str(unrealized),
            'daily_realized_pnl': str(day_realized),
            'daily_unrealized_change': str(day_pnl - day_realized),
            'daily_commission': str(sum((f.commission for f in todays_fills), Decimal(0))),
            'cumulative_commission': str(result.commissions),
            'daily_simulated_fills': len(todays_fills),
            'cumulative_simulated_fills': len(result.fills),
            'pending_close_change': str(changes[-1]),
            'max_close_drawdown_fraction': str(result.max_close_drawdown_fraction),
            'price_sha256': history.sha256, 'provider_authenticity_verified': False,
            'corporate_actions_verified': False, 'signal_causality_verified': False,
            'actual_execution_verified': False, 'deployment_approved': False,
        }
        money = lambda name: format(Decimal(accounting[name]), '.2f') + ' USD'
        body = '\n'.join([
            '[자체 모의매매 계산 미리보기 · 실제 주문/체결 아님]',
            '거래일: ' + days[-1] + ' (미국 정규장 종가 기준)',
            '계산 기간: ' + days[0] + ' ~ ' + days[-1],
            '전략: ' + strategy + ' / 버전: ' + version + ' / 종목: ' + symbol,
            '모형: 종가 신호 → 다음 거래일 시가 / 평균원가법',
            '가상 초기 현금: ' + money('initial_cash'),
            '가상 평가액: ' + money('equity') + ' / 현금: ' + money('cash'),
            '가상 보유: ' + str(units) + '주',
            '당일 손익: ' + money('daily_pnl'),
            '당일 실현: ' + money('daily_realized_pnl') + ' / 미실현 증감: ' + money('daily_unrealized_change'),
            '누적 손익: ' + money('cumulative_pnl'),
            '누적 실현: ' + money('realized_pnl') + ' / 현재 미실현: ' + money('unrealized_pnl'),
            '가상 체결: 당일 ' + str(len(todays_fills)) + '건 / 누적 ' + str(len(result.fills)) + '건',
            '가상 수수료: 당일 ' + money('daily_commission') + ' / 누적 ' + money('cumulative_commission'),
            '비용 가정: 수수료 ' + str(_decimal(manifest['commission_bps'])) + 'bp / 슬리피지 '
            + str(_decimal(manifest['slippage_bps'])) + 'bp',
            '종가 기준 최대낙폭: ' + format(result.max_close_drawdown_fraction * 100, '.2f') + '%',
            '종가 후 미실행 수량 변화: ' + str(changes[-1]) + '주 (다음 시가 미확인)',
            '실제 주문·체결·취소: 해당 없음 / 운영 위험 한도: 미설정',
            '미검증: 시세 출처·조정 방식·신호 인과성 / 배당·분할·세금·유동성 미반영',
            '과거 입력 재계산이며 영속 Paper 운영·전략 검증·Telegram 수신의 증명이 아님',
        ])
    validate_telegram_text(body)
    return accounting, body
