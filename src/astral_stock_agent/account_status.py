"""Limited observation alerts explicitly distinct from trading performance."""
import hashlib
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from .account_observation import normalize_account_observation
from .report import InvalidSnapshot, text


def account_status_key(source, account_ref, now):
    """Same immutable namespace for file previews and private collected runs."""
    try:
        if source not in ('fixture', 'astral_mcp') or not isinstance(now, datetime) or now.utcoffset() is None:
            raise ValueError
        day = now.astimezone(ZoneInfo('Asia/Seoul')).date().isoformat()
        return hashlib.sha256(json.dumps(['paper_account_status_v1', source,
                                         text(account_ref), day]).encode()).hexdigest()
    except (ValueError, TypeError, AttributeError, OverflowError):
        raise InvalidSnapshot('invalid account status scope or clock') from None


def render_account_status(bundle, *, verified_us_symbols, now):
    """Validate fresh official envelopes; return an independent daily outbox key.

    The date is the Korean notification date, never a trading-day claim. A
    changed observation on that date conflicts with the immutable queued body;
    it must not silently overwrite or resend. Fixtures remain preview-only at
    the delivery boundary. No unknown accounting field is promoted to zero.
    """
    observation = normalize_account_observation(bundle, verified_us_symbols=verified_us_symbols, now=now)
    return _render_observation(observation, account_ref=bundle['account_ref'], now=now)


def _render_observation(observation, *, account_ref, now):
    # Internal only: callers must normalize original official responses first.
    day = now.astimezone(ZoneInfo('Asia/Seoul')).date().isoformat()
    prefix = '[검증용 예제 · 실제 거래 기록 아님]\n' if observation['source'] == 'fixture' else ''
    def amount(field):
        value = observation[field]
        return '미확인' if value is None else value + ' USD'
    prerequisite = observation['prerequisites']
    deployment = '확인' if prerequisite['us_paper_deployment_verified'] else '미확인'
    if prerequisite['blocking_reasons'] == ['no_active_deployment']:
        deployment = '대상 계좌에 활성 배포 없음 (조회 시점)'
    body = '\n'.join([
        prefix + '[Astral Paper 상태 알림 · 매매 성과 보고 아님]',
        '알림 기준일: ' + day + ' (한국 시간 · 거래일 아님)',
        '계좌 응답 수신 시각: ' + observation['balance_received_at'],
        '모드: 모의매매 / 계좌 기본 조건: 확인',
        '의도한 미국 주식 전략 배포: ' + deployment,
        '전략명: 이 조회 범위에서 확인하지 않음',
        '공식 응답의 가상 현금: ' + amount('observed_cash_usd'),
        '미배정 가상 현금: ' + amount('unassigned_cash_usd'),
        '예약 현금: ' + amount('reserved_cash_usd'),
        '잔액 기준 시각·종가 기준 거래일·평가액: 미확인',
        '당일·누적 손익 (실현/미실현): 미확인',
        '신규 주문·체결·보유 종목: 미확인',
        '위험 한도·원장 대조·장애 복구 상태: 미검증',
        '신규 주문 권한 없음 / 완전한 일일 매매 보고 아님',
    ])
    return account_status_key(observation['source'], account_ref, now), body
