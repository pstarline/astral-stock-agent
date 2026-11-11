"""Read-only operational prerequisites, not a daily performance report.

A missing deployment is not evidence of zero orders, fills or profit. This
module cannot grant order authority or replace any existing strategy.
"""
import asyncio
from datetime import datetime, timedelta, timezone

from .activity import LIMIT, _result, _rows
from .astral import AstralBoundaryError, paper_account, paper_deployment
from .report import InvalidSnapshot, text, timestamp


TOOLS = {'accounts': 'astral_broker_accounts_list', 'deployments': 'astral_deployments_list'}


def normalize_preflight(bundle, *, verified_us_symbols, now):
    try:
        if type(bundle['version']) is not int or bundle['version'] != 1:
            raise ValueError
        if bundle['source'] not in ('fixture', 'astral_mcp') or now.tzinfo is None:
            raise ValueError
        account_ref = text(bundle['account_ref'])
        responses, times = {}, []
        for name, tool in TOOLS.items():
            row = bundle['responses'][name]
            if row['tool'] != tool or row['arguments'] != {}:
                raise ValueError
            received = timestamp(row['received_at'])
            if not timedelta(0) <= now - received <= timedelta(minutes=5):
                raise ValueError
            times.append(received)
            responses[name] = _result(row['result'])
        if max(times) - min(times) > timedelta(minutes=1):
            raise ValueError
        accounts, deployments = responses['accounts'], responses['deployments']
        account_rows = _rows(accounts, 'broker_accounts', 'count')
        if any(not isinstance(row.get('account_id'), str) or not row['account_id'] for row in account_rows):
            raise ValueError
        account = paper_account(accounts, account_ref=account_ref)
        if not isinstance(account.get('supported_asset_types'), list) or 'stock' not in account['supported_asset_types']:
            raise ValueError
        active = deployments.get('active_strategies')
        if (deployments.get('status') != 'ok' or not isinstance(active, list) or len(active) >= LIMIT
                or any(not isinstance(row, dict) or not isinstance(row.get('broker_account_id'), str)
                       or not row['broker_account_id'] or 'id' not in row or 'strategy_id' not in row for row in active)):
            raise ValueError
        scoped = [row for row in active if row.get('broker_account_id') == account_ref]
        strategy_ref = bundle['strategy_ref']
        verified = False
        if strategy_ref is None:
            if scoped or account.get('active_deployment'):
                reasons = ('configured_strategy_missing',)
            elif account.get('available_for_strategy_deployment') is True:
                reasons = ('no_active_deployment',)
            else:
                reasons = ('account_availability_unverified',)
        else:
            if type(strategy_ref) not in (str, int) or not str(strategy_ref).isdigit() or int(strategy_ref) <= 0:
                raise ValueError
            try:
                paper_deployment(accounts, deployments, account_ref=account_ref,
                                 strategy_ref=strategy_ref, verified_us_symbols=verified_us_symbols)
                verified = True
                reasons = ('daily_ledger_and_delivery_unverified',)
            except AstralBoundaryError:
                reasons = ('intended_us_paper_deployment_unverified',)
        # No identifiers, names, balances, raw errors or source rows leave here.
        return {'kind': 'paper_preflight', 'source': bundle['source'],
                'checked_at': max(times).isoformat(), 'account_verified': True,
                'us_paper_deployment_verified': verified, 'blocking_reasons': list(reasons),
                'new_orders_allowed': False, 'ledger_reconciled': False,
                'complete_daily_snapshot': False, 'telegram_ready': False}
    except (KeyError, TypeError, AttributeError, ValueError, OverflowError, AstralBoundaryError):
        raise InvalidSnapshot('Paper prerequisites unavailable or unverified') from None


async def collect_preflight(call_tool, *, account_ref, strategy_ref, verified_us_symbols, clock=None):
    """Use an authenticated callback for exactly two whitelisted read operations.

    No credentials are obtained here. Orders, trades, strategy mutation and
    Telegram are absent. The Work caller supplies actual MCP results.
    """
    clock = clock or (lambda: datetime.now(timezone.utc))

    async def read(name):
        try:
            result = await call_tool(TOOLS[name], {})
            return name, {'tool': TOOLS[name], 'arguments': {},
                          'received_at': clock().isoformat(), 'result': result}
        except Exception:
            raise InvalidSnapshot('Paper prerequisite read failed') from None

    try:
        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(read(name)) for name in TOOLS]
    except Exception:
        # Settle sibling reads and async cleanup before reporting failure.
        raise InvalidSnapshot('Paper prerequisite read failed') from None
    rows = [task.result() for task in tasks]
    return normalize_preflight({'version': 1, 'source': 'astral_mcp', 'account_ref': account_ref,
                               'strategy_ref': strategy_ref, 'responses': dict(rows)},
                              verified_us_symbols=verified_us_symbols, now=clock())


def render_preflight(observation):
    prefix = '[검증용 예제 · 실제 거래 기록 아님]\n' if observation['source'] == 'fixture' else ''
    reasons = {
        'no_active_deployment': '대상 계좌에 활성 배포가 확인되지 않음',
        'configured_strategy_missing': '계좌에 활동이 있으나 의도한 전략 설정이 없음',
        'account_availability_unverified': '계좌의 배포 가능 상태 미확인',
        'intended_us_paper_deployment_unverified': '의도한 미국 주식 Paper 배포 미검증',
        'daily_ledger_and_delivery_unverified': '일일 원장 대조와 실제 보고 수신 미검증',
    }
    return '\n'.join([prefix + '[미국 주식 Paper 실행 전 점검]',
                      '계좌 기본 조건: 확인',
                      '미국 주식 Paper 배포: ' + ('확인' if observation['us_paper_deployment_verified'] else '미확인'),
                      *['진행 장애: ' + reasons[key] for key in observation['blocking_reasons']],
                      '신규 주문 권한 없음 / Telegram 전송 불가',
                      '이 점검은 거래·체결·손익이 0이라는 뜻이 아닙니다.'])
