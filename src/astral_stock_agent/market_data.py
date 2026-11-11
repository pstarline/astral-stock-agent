"""Official Alpha Vantage compact daily input; private research previews only.

Only raw-as-traded TIME_SERIES_DAILY is requested. No premium/full endpoint,
source fallback, retries, persistence, broker authority or message delivery.
"""
import csv
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
import hashlib
import io
import json
import os
import re
from urllib.parse import urlencode
from urllib.request import Request, build_opener

from .delivery import _NoRedirect, validate_telegram_text
from .independent_signals import RULE_FIELDS, SMA_RULE, preview_independent_sma
from .research_data import HEADERS, DailyHistory, read_daily_csv
from .research_sessions import UTC_SESSION_DATE, require_daily_session_coverage
from .sessions import latest_closed_session
from .research_execution import _decimal


MAX_RESPONSE_BYTES = 1_000_000
ROW_FIELDS = ('1. open', '2. high', '3. low', '4. close', '5. volume')
META_FIELDS = {'1. Information', '2. Symbol', '3. Last Refreshed', '4. Output Size', '5. Time Zone'}
CONFIG_FIELDS = {'schema', 'symbol', 'initial_cash', 'commission_bps', 'slippage_bps', 'rule'}


class MarketDataError(ValueError):
    pass


@dataclass(frozen=True)
class DailyMarketData:
    symbol: str
    first_day: date
    last_day: date
    count: int
    response_sha256: str
    csv_sha256: str
    csv_bytes: bytes = field(repr=False)
    history: DailyHistory = field(repr=False)
    provider: str = 'alphavantage_daily_compact_v1'
    price_basis: str = 'raw_as_traded'
    label_convention: str = UTC_SESSION_DATE
    provider_authenticity_verified: bool = False
    corporate_actions_verified: bool = False


def _scope(symbol, verified_us_symbols):
    if (type(symbol) is not str or not re.fullmatch(r'[A-Z][A-Z0-9.\-]{0,14}', symbol)
            or type(verified_us_symbols) not in (list, tuple, set, frozenset)
            or symbol not in verified_us_symbols):
        raise MarketDataError('explicit independently checked US equity scope required')


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise MarketDataError('ambiguous daily response')
        result[key] = value
    return result


def normalize_alpha_daily(raw, *, symbol, verified_us_symbols, now):
    """Validate a complete recorded response and preserve every compact row.

    Provider dates denote Eastern-market session dates. Encoding that same
    date at UTC midnight is an internal label, not a midnight execution claim.
    SHA/count/shape checks do not authenticate an arbitrary supplied response.
    No dropped bad rows, fill-forward, zero substitutions or adjusted prices.
    """
    try:
        _scope(symbol, verified_us_symbols)
        expected_day = latest_closed_session(now).day
        if type(raw) is not bytes or not 0 < len(raw) <= MAX_RESPONSE_BYTES:
            raise ValueError
        response = json.loads(raw, object_pairs_hook=_unique)
        if type(response) is not dict or set(response) != {'Meta Data', 'Time Series (Daily)'}:
            raise ValueError
        meta, series = response['Meta Data'], response['Time Series (Daily)']
        if (type(meta) is not dict or set(meta) != META_FIELDS
                or meta['1. Information'] != 'Daily Prices (open, high, low, close) and Volumes'
                or meta['2. Symbol'] != symbol or meta['4. Output Size'] != 'Compact'
                or meta['5. Time Zone'] != 'US/Eastern'
                or meta['3. Last Refreshed'] != expected_day.isoformat()
                or type(series) is not dict or not 1 <= len(series) <= 100):
            raise ValueError
        buffer = io.StringIO(newline='')
        writer = csv.writer(buffer, lineterminator='\n')
        writer.writerow(HEADERS)
        days = []
        for label, row in sorted(series.items()):
            day = date.fromisoformat(label)
            if day.isoformat() != label or type(row) is not dict or set(row) != set(ROW_FIELDS):
                raise ValueError
            for key in ROW_FIELDS:
                value = row[key]
                pattern = r'[0-9]{1,20}' if key == '5. volume' else r'[0-9]{1,18}(?:\.[0-9]{1,18})?'
                if type(value) is not str or not re.fullmatch(pattern, value):
                    raise ValueError
            writer.writerow((label + 'T00:00:00Z', *(row[key] for key in ROW_FIELDS)))
            days.append(day)
        if days[-1] != expected_day:
            raise ValueError
        data = buffer.getvalue().encode()
        digest = hashlib.sha256(data).hexdigest()
        history = read_daily_csv(data, symbol=symbol, expected_sha256=digest,
                                 expected_count=len(days), start=days[0].isoformat()+'T00:00:00Z',
                                 end=days[-1].isoformat()+'T23:59:59Z')
        require_daily_session_coverage(history, first_day=days[0], last_day=days[-1],
                                       now=now, label_convention=UTC_SESSION_DATE)
        return DailyMarketData(symbol, days[0], days[-1], len(days),
                               hashlib.sha256(raw).hexdigest(), digest, data, history)
    except Exception:
        # Provider error/rate-limit text may include URLs or private key values.
        raise MarketDataError('complete current raw US daily data required') from None


def fetch_alpha_daily(*, symbol, api_key, verified_us_symbols, clock=None):
    """One bounded HTTPS GET; caller provides an already authorized private key.

    The documented defaults are JSON/compact. The response MUST confirm Compact;
    no caller can request full/adjusted/realtime, choose an arbitrary host, or
    supply extra parameters. Public CI is blocked before clock/network access.
    """
    try:
        if os.getenv('GITHUB_ACTIONS'):
            raise ValueError
        _scope(symbol, verified_us_symbols)
        if type(api_key) is not str or not re.fullmatch(r'[A-Za-z0-9]{1,128}', api_key):
            raise ValueError
        if api_key == 'demo' and symbol != 'IBM':
            raise ValueError
        clock = clock or (lambda: datetime.now(timezone.utc))
        started = clock()
        latest_closed_session(started)
        query = urlencode(dict(function='TIME_SERIES_DAILY', symbol=symbol, apikey=api_key))
        request = Request('https://www.alphavantage.co/query?' + query,
                          headers={'Accept': 'application/json'}, method='GET')
        with build_opener(_NoRedirect()).open(request, timeout=15) as response:
            if response.status != 200:
                raise ValueError
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        finished = clock()
        if not timedelta(0) <= finished - started <= timedelta(minutes=1):
            raise ValueError
        return normalize_alpha_daily(raw, symbol=symbol,
                                     verified_us_symbols=verified_us_symbols, now=finished)
    except Exception:
        raise MarketDataError('daily market data unavailable or unverified; no automatic retry') from None


def collect_sma_preview(config, *, api_key, verified_us_symbols, clock=None):
    """Collect once and compose the existing price-only SMA research preview.

    Compact is a rolling window, NOT a fixed forward-run archive. Nothing is
    recorded in the Paper ledger or queued/sent. Parameters are never selected.
    """
    try:
        if os.getenv('GITHUB_ACTIONS'):
            raise ValueError
        if (type(config) is not dict or set(config) != CONFIG_FIELDS
                or config['schema'] != 'alpha_sma_preview_v1'):
            raise ValueError
        # Reject statically invalid configuration BEFORE consuming a daily API
        # request. Coverage/warmup/resources are checked again with actual data.
        _scope(config['symbol'], verified_us_symbols)
        rule = config['rule']
        if (type(rule) is not dict or set(rule) != RULE_FIELDS or rule['id'] != SMA_RULE
                or type(rule['window']) is not int or not 2 <= rule['window'] <= 99
                or _decimal(rule['units']) <= 0 or _decimal(config['initial_cash']) <= 0
                or not 0 <= _decimal(config['commission_bps']) < 10000
                or not 0 <= _decimal(config['slippage_bps']) < 10000):
            raise ValueError
        trade_from = date.fromisoformat(rule['trade_from'])
        if trade_from.isoformat() != rule['trade_from']:
            raise ValueError
        clock = clock or (lambda: datetime.now(timezone.utc))
        data = fetch_alpha_daily(symbol=config['symbol'], api_key=api_key,
                                verified_us_symbols=verified_us_symbols, clock=clock)
        spec = {key: config[key] for key in ('symbol', 'initial_cash', 'commission_bps',
                                            'slippage_bps', 'rule')}
        spec.update(schema='independent_sma_candidate_v1', price_sha256=data.csv_sha256,
                    price_count=data.count, first_day=data.first_day.isoformat(),
                    last_day=data.last_day.isoformat(), label_convention=data.label_convention)
        finished = clock()
        if latest_closed_session(finished).day != data.last_day:
            raise ValueError
        manifest, accounting, body = preview_independent_sma(
            spec, data.csv_bytes, verified_us_symbols=verified_us_symbols, now=finished)
        accounting.update(price_provider=data.provider, price_basis=data.price_basis,
                          provider_response_sha256=data.response_sha256)
        body += ('\n시세: Alpha Vantage TIME_SERIES_DAILY / raw as traded / Compact'
                 + '\n시세 최신일: ' + data.last_day.isoformat()
                 + ' / 최근 관측 창 재계산 · 영속 forward 운영 아님')
        validate_telegram_text(body)
        accounting['report_sha256'] = hashlib.sha256(body.encode()).hexdigest()
        return data, manifest, accounting, body
    except Exception:
        raise MarketDataError('private candidate configuration or current daily data unverified') from None
