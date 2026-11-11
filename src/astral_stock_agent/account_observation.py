"""Instant official account observations, never reconciled trading performance.

Uses the existing preflight scope/receipt validation. Cash is the provider's
account field, not equity, buying power or a closing balance. No order/fill/P&L
value is inferred from absence of a deployment.
"""
import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .activity import _result
from .astral import paper_account
from .preflight import TOOLS, normalize_preflight, render_preflight
from .report import InvalidSnapshot, money, timestamp


def _optional_amount(account, field):
    value = account.get(field)
    if value is None:
        return None
    if type(value) not in (str, int, float):
        raise InvalidSnapshot('invalid observed cash')
    return str(money(str(value), nonnegative=True))


def normalize_account_observation(bundle, *, verified_us_symbols, now):
    try:
        prerequisite = normalize_preflight(bundle, verified_us_symbols=verified_us_symbols, now=now)
        accounts = _result(bundle['responses']['accounts']['result'])
        account = paper_account(accounts, account_ref=bundle['account_ref'])
        # These fields have distinct provider meanings. No sum or substitution
        # is assumed, especially when strategy capital has been assigned.
        amounts = {key: _optional_amount(account, field) for key, field in (
            ('observed_cash_usd', 'cash'), ('unassigned_cash_usd', 'unassigned_cash'),
            ('reserved_cash_usd', 'reserved_cash'),
        )}
        return {
            'kind': 'paper_account_observation', 'source': bundle['source'],
            'checked_at': prerequisite['checked_at'],
            'balance_received_at': bundle['responses']['accounts']['received_at'],
            'balance_as_of': None, 'valuation_as_of': None, 'trading_day': None,
            'currency': 'USD', 'mode': 'astral_paper', **amounts,
            'equity_usd': None, 'daily_realized_pnl_usd': None,
            'daily_unrealized_pnl_usd': None, 'cumulative_pnl_usd': None,
            'orders': None, 'fills': None, 'positions': None,
            'prerequisites': prerequisite,
            'ledger_reconciled': False, 'complete_daily_snapshot': False,
            'new_orders_allowed': False, 'telegram_ready': False,
        }
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        raise InvalidSnapshot('official paper account observation unavailable') from None


async def collect_account_observation(call_tool, *, account_ref, strategy_ref,
                                      verified_us_symbols, clock=None):
    """Read two official tools via an already-authorized callback, once each.

    No authentication acquisition, order/trade queries, retries, persistence or
    delivery. Returned amounts remain private. The caller owns the connection.
    On failure/cancellation, settle both callback tasks before returning. The
    callback must cooperate with cancellation and close its own transport.
    """
    clock = clock or (lambda: datetime.now(timezone.utc))

    async def read(name):
        try:
            result = await call_tool(TOOLS[name], {})
            return name, {'tool': TOOLS[name], 'arguments': {},
                          'received_at': clock().isoformat(), 'result': result}
        except Exception:
            raise InvalidSnapshot('official paper account read failed') from None

    try:
        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(read(name)) for name in TOOLS]
    except Exception:
        # TaskGroup cancels and awaits siblings, including async cleanup. Do
        # not expose an ExceptionGroup or provider/cleanup exception details.
        raise InvalidSnapshot('official paper account read failed') from None
    rows = [task.result() for task in tasks]
    bundle = {'version': 1, 'source': 'astral_mcp', 'account_ref': account_ref,
              'strategy_ref': strategy_ref, 'responses': dict(rows)}
    return normalize_account_observation(bundle, verified_us_symbols=verified_us_symbols, now=clock())


def render_account_observation(bundle, *, verified_us_symbols, now):
    """Validate original source envelopes before previewing private cash fields."""
    result = normalize_account_observation(bundle, verified_us_symbols=verified_us_symbols, now=now)
    received = timestamp(result['balance_received_at']).astimezone(ZoneInfo('Asia/Seoul')).isoformat()
    def amount(key):
        value = result[key]
        return '미확인' if value is None else value + ' USD'
    prefix = '[검증용 예제 · 실제 거래 기록 아님]\n' if result['source'] == 'fixture' else ''
    return '\n'.join([
        prefix + '[Astral Paper 계좌 조회 · 거래 성과 보고 아님]',
        '계좌 응답 수신 시각: ' + received,
        '운영 모드: 모의매매 / 통화: USD',
        '공식 응답의 현금: ' + amount('observed_cash_usd'),
        '미배정 가상 현금: ' + amount('unassigned_cash_usd'),
        '예약 현금: ' + amount('reserved_cash_usd'),
        '잔액 기준 시각·종가 기준 거래일: 미확인',
        '평가액·당일 실현/미실현 손익·누적 손익: 미확인',
        '신규 주문·체결·보유 종목: 이 조회로 확인하지 않음',
        render_preflight(result['prerequisites']).removeprefix(prefix) if prefix else render_preflight(result['prerequisites']),
        '현금을 평가액으로 바꾸거나 미확인 거래·손익을 0으로 표시하지 않습니다.',
    ])
