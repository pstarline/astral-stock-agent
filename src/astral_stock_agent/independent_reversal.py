"""Daily reversal hypothesis → existing next-open accounting/report preview.

Caller-owned explicit research configuration only; no source copying, parameter
search, network, persistence, delivery, worker activation or order authority.
"""
from datetime import date, timezone
import hashlib
import json
from zoneinfo import ZoneInfo

from .delivery import validate_telegram_text
from .independent_paper import MANIFEST_FIELDS, preview_independent_paper
from .research_data import ResearchDataError, read_daily_csv
from .research_execution import _decimal
from .research_reversal import (PERIODS, REVERSAL_RULE, reversal_close_decisions,
                                validate_reversal_rule)
from .research_sessions import UTC_SESSION_DATE, NY_SESSION_DATE


SPEC_FIELDS = (MANIFEST_FIELDS - {'strategy', 'version', 'close_signals'}) | {'rule'}


def preview_independent_reversal(spec, prices, *, verified_us_symbols, now,
                                 allow_pending_only=False):
    """A new daily hypothesis, not an Astral backtest or forward Paper run."""
    if (type(spec) is not dict or set(spec) != SPEC_FIELDS
            or spec['schema'] != 'independent_daily_reversal_v1'
            or type(verified_us_symbols) not in (tuple, list, set, frozenset)
            or type(spec['symbol']) is not str or spec['symbol'] not in verified_us_symbols):
        raise ResearchDataError('explicit private daily reversal scope required')
    rule = validate_reversal_rule(spec['rule'])
    try:
        first, last = (date.fromisoformat(spec[k]) for k in ('first_day', 'last_day'))
        if first.isoformat() != spec['first_day'] or last.isoformat() != spec['last_day']:
            raise ValueError
        cash, commission, slip = (_decimal(spec[k]) for k in
                                 ('initial_cash', 'commission_bps', 'slippage_bps'))
        if cash <= 0 or not 0 <= commission < 10000 or not 0 <= slip < 10000:
            raise ValueError
    except Exception:
        raise ResearchDataError('explicit daily reversal dates and costs required') from None
    convention = spec['label_convention']
    if convention not in (UTC_SESSION_DATE, NY_SESSION_DATE):
        raise ResearchDataError('explicit supported daily label convention required')
    history = read_daily_csv(prices, symbol=spec['symbol'], expected_sha256=spec['price_sha256'],
        expected_count=spec['price_count'], start=first.isoformat()+'T00:00:00Z',
        end=last.isoformat()+'T23:59:59Z')
    decisions = reversal_close_decisions(history, spec['rule'], slippage_bps=slip,
        first_day=first, last_day=last, now=now, label_convention=convention,
        allow_pending_only=allow_pending_only)
    # Slippage changes observed entry levels and therefore future close exits.
    # Bind it to the signal version; commission/cash change accounting only.
    version_rule = {k: (v.isoformat() if k == 'trade_from' else str(v)
                       if k not in (*PERIODS, 'id', 'max_holding_sessions') else v)
                    for k, v in rule.items()}
    contract = dict(rule=version_rule, execution='daily_next_open_close_triggers_v1',
                    slippage_bps=str(slip), labels=convention,
                    indicators='daily_seeded_indicators_v1')
    version = 'dr1-' + hashlib.sha256(json.dumps(contract, sort_keys=True,
        separators=(',', ':')).encode()).hexdigest()
    zone = timezone.utc if convention == UTC_SESSION_DATE else ZoneInfo('America/New_York')
    manifest = {k: spec[k] for k in SPEC_FIELDS - {'rule', 'schema'}}
    manifest.update(schema='independent_price_simulation_v1',
        strategy='Independent daily reversal candidate', version=version,
        close_signals=[dict(session=bar.timestamp.astimezone(zone).date().isoformat(),
                           units_change=str(change))
                       for bar, change in zip(history.bars, decisions.changes)])
    accounting, body = preview_independent_paper(manifest, prices,
        verified_us_symbols=verified_us_symbols, now=now)
    accounting.update(signal_method=REVERSAL_RULE, candidate_only=True,
        source_equivalence_verified=False, intrabar_protection_verified=False,
        trade_from=rule['trade_from'].isoformat(), latest_decision=decisions.reasons[-1])
    body += ('\n별도 일봉 반전 연구 후보 / 원본 15분봉 재현 아님'
             '\n손익·보유기간 청산은 종가 판단 후 다음 시가 / 장중 보호 주문 없음'
             '\n갭으로 가상 손실이 청산 기준을 초과할 수 있음 / 자동 채택·운영 승인 없음')
    validate_telegram_text(body)
    accounting['report_sha256'] = hashlib.sha256(body.encode()).hexdigest()
    return manifest, accounting, body
