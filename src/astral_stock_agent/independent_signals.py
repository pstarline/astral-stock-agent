"""Price-derived original SMA candidate → independent daily preview.

No source-code execution, optimization, strategy selection, storage, network,
orders or deployment. Explicit parameters are hypothetical research inputs.
"""
from datetime import date, timezone
import hashlib
import json
from zoneinfo import ZoneInfo

from .delivery import validate_telegram_text
from .independent_paper import MANIFEST_FIELDS, preview_independent_paper
from .research_data import ResearchDataError, read_daily_csv
from .research_execution import _decimal
from .research_sessions import (NY_SESSION_DATE, UTC_SESSION_DATE,
                                require_daily_session_coverage)
from .research_trend import sma_close_decisions


SMA_RULE = 'close_gt_trailing_sma_fixed_units_v1'
SPEC_FIELDS = (MANIFEST_FIELDS - {'strategy', 'version', 'close_signals'}) | {'rule'}
RULE_FIELDS = {'id', 'window', 'units', 'trade_from'}


def preview_independent_sma(spec, prices, *, verified_us_symbols, now):
    """Return validated generated manifest, accounting and a candidate preview.

    Reuse the existing trailing-close SMA generator, including equality=flat,
    explicit warmup/evaluation boundary and fixed target units. A version binds
    the implemented rule, parameters and label convention. It is not an
    Astral version, a profitability claim or permission for forward execution.
    Provider authenticity/adjustments and full signal causality stay unverified.
    """
    if (type(spec) is not dict or set(spec) != SPEC_FIELDS
            or spec['schema'] != 'independent_sma_candidate_v1'):
        raise ResearchDataError('explicit independent SMA candidate spec required')
    rule = spec['rule']
    if (type(rule) is not dict or set(rule) != RULE_FIELDS or rule['id'] != SMA_RULE):
        raise ResearchDataError('supported explicit candidate rule required')
    if (type(verified_us_symbols) not in (tuple, list, set, frozenset)
            or type(spec['symbol']) is not str or spec['symbol'] not in verified_us_symbols):
        raise ResearchDataError('independently checked US equity scope required')
    try:
        first, last = (date.fromisoformat(spec[k]) for k in ('first_day', 'last_day'))
        trade_from = date.fromisoformat(rule['trade_from'])
    except (TypeError, ValueError):
        raise ResearchDataError('explicit candidate session dates required') from None
    convention = spec['label_convention']
    if convention not in (UTC_SESSION_DATE, NY_SESSION_DATE):
        raise ResearchDataError('explicit supported daily label convention required')
    history = read_daily_csv(prices, symbol=spec['symbol'], expected_sha256=spec['price_sha256'],
                             expected_count=spec['price_count'],
                             start=first.isoformat() + 'T00:00:00Z',
                             end=last.isoformat() + 'T23:59:59Z')
    # Reject gaps/future sessions before generating any rule decisions. The
    # downstream generic preview repeats its independent input checks.
    require_daily_session_coverage(history, first_day=first, last_day=last,
                                   now=now, label_convention=convention)
    decisions = sma_close_decisions(history, window=rule['window'], units=rule['units'],
                                   trade_from=trade_from, label_convention=convention)
    units = _decimal(rule['units'])
    version_spec = dict(id=SMA_RULE, window=rule['window'], units=str(units),
                        trade_from=trade_from.isoformat(), label_convention=convention,
                        execution='next_session_open_long_cash_v1')
    version = 'sma1-' + hashlib.sha256(json.dumps(version_spec, sort_keys=True,
                                                   separators=(',', ':')).encode()).hexdigest()
    zone = timezone.utc if convention == UTC_SESSION_DATE else ZoneInfo('America/New_York')
    manifest = {k: spec[k] for k in SPEC_FIELDS - {'rule', 'schema'}}
    manifest.update(schema='independent_price_simulation_v1',
                    strategy='Independent SMA candidate', version=version,
                    close_signals=[dict(session=bar.timestamp.astimezone(zone).date().isoformat(),
                                        units_change=str(change))
                                   for bar, change in zip(history.bars, decisions.changes)])
    accounting, body = preview_independent_paper(manifest, prices,
                                                verified_us_symbols=verified_us_symbols, now=now)
    accounting.update(signal_method=SMA_RULE, candidate_only=True,
                      sma_window=rule['window'], target_units=str(units),
                      trade_from=trade_from.isoformat())
    explanation = ('규칙: 종가 > 당일 포함 최근 ' + str(rule['window']) + '개 종가 SMA이면 '
                   + str(units) + '주 보유, 같거나 낮으면 미보유\n'
                   + '신호 시작일: ' + trade_from.isoformat()
                   + ' / 다음 거래일 시가 가상 체결 / 자동 채택되지 않은 자체 연구 후보')
    body += '\n' + explanation
    validate_telegram_text(body)
    # Bind the final candidate explanation as well as the generic report. A
    # downstream checkpoint must never receive a hash for the shorter body.
    accounting['report_sha256'] = hashlib.sha256(body.encode()).hexdigest()
    return manifest, accounting, body
