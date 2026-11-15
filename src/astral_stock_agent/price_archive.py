"""Private fixed-start daily history, extended from overlapping compact windows.

No network, keys, strategy adoption, Paper accounting or message delivery.
The storage guard reuses the Paper ledger's private write-once POSIX primitives;
the caller supplies a separate durable directory, never the Paper ledger path.
"""
from dataclasses import dataclass, field
from datetime import date
import hashlib
import json

from .independent_ledger import IndependentPaperLedger, MAX_BYTES, _canonical, _unique
from .market_data import DailyMarketData
from .research_data import read_daily_csv
from .research_sessions import UTC_SESSION_DATE, require_daily_session_coverage
from .sessions import latest_closed_session


class PriceArchiveError(ValueError):
    pass


@dataclass(frozen=True)
class ArchivedPrices:
    symbol: str
    first_day: date
    last_day: date
    count: int
    csv_sha256: str
    created: bool
    csv_bytes: bytes = field(repr=False)


class PriceArchive:
    def __init__(self, directory):
        self._storage = IndependentPaperLedger(directory)

    @staticmethod
    def _history(csv_bytes, *, symbol, first, last, count, digest, now):
        history = read_daily_csv(csv_bytes, symbol=symbol, expected_sha256=digest,
            expected_count=count, start=first.isoformat()+'T00:00:00Z',
            end=last.isoformat()+'T23:59:59Z')
        require_daily_session_coverage(history, first_day=first, last_day=last,
                                      now=now, label_convention=UTC_SESSION_DATE)
        return history

    def append(self, data, *, verified_us_symbols, now, bootstrap=False):
        """Append the latest session, requiring exact prior-session continuity.

        Bootstrap pins the initial history start and scope. Later windows must
        overlap the archive and agree on every shared OHLCV value. Missing old
        rows before the rolling window are retained. Revisions, gaps and an
        ambiguous interrupted write block progression without overwriting.
        """
        try:
            if type(bootstrap) is not bool or type(data) is not DailyMarketData:
                raise ValueError
            if (type(verified_us_symbols) not in (tuple, list, set, frozenset)
                    or data.symbol not in verified_us_symbols
                    or data.provider != 'alphavantage_daily_compact_v1'
                    or data.price_basis != 'raw_as_traded'
                    or data.label_convention != UTC_SESSION_DATE
                    or data.provider_authenticity_verified is not False
                    or data.corporate_actions_verified is not False
                    or data.last_day != latest_closed_session(now).day
                    or not 1 <= data.count <= 100):
                raise ValueError
            incoming = self._history(data.csv_bytes, symbol=data.symbol, first=data.first_day,
                last=data.last_day, count=data.count, digest=data.csv_sha256, now=now)
            if incoming != data.history:
                raise ValueError
            scope = dict(symbol=data.symbol, provider=data.provider, price_basis=data.price_basis,
                         label_convention=data.label_convention)
            store = self._storage
            scope_path = store._path('price-archive-scope-v1')
            previous_hash = None
            if bootstrap:
                scope_record = dict(version=1, scope=scope, first_day=data.first_day.isoformat(),
                                    bootstrap_session=data.last_day.isoformat())
                merged = data.csv_bytes
                first = data.first_day
            else:
                scope_record = json.loads(store._read(scope_path), object_pairs_hook=_unique)
                if scope_record['version'] != 1 or scope_record['scope'] != scope:
                    raise ValueError
                first = date.fromisoformat(scope_record['first_day'])
                previous_day = latest_closed_session(latest_closed_session(now).opens_at).day
                previous_raw = store._read(store._path('price-archive:'+previous_day.isoformat()))
                previous = json.loads(previous_raw, object_pairs_hook=_unique)
                old_csv = previous['csv'].encode()
                old = self._history(old_csv, symbol=data.symbol, first=first, last=previous_day,
                    count=previous['count'], digest=previous['csv_sha256'], now=now)
                if previous['scope'] != scope or previous['session'] != previous_day.isoformat():
                    raise ValueError
                old_by_time = {bar.timestamp: bar for bar in old.bars}
                shared = [bar for bar in incoming.bars if bar.timestamp in old_by_time]
                if not shared or any(old_by_time[bar.timestamp] != bar for bar in shared):
                    raise ValueError
                # No insertion of earlier history or missing overlapping dates.
                if incoming.bars[0].timestamp < old.bars[0].timestamp:
                    raise ValueError
                lines = data.csv_bytes.splitlines(keepends=True)
                merged = old_csv + b''.join(line for line, bar in zip(lines[1:], incoming.bars)
                                           if bar.timestamp > old.bars[-1].timestamp)
                previous_hash = hashlib.sha256(previous_raw).hexdigest()
            digest = hashlib.sha256(merged).hexdigest()
            count = len(merged.splitlines()) - 1
            self._history(merged, symbol=data.symbol, first=first, last=data.last_day,
                          count=count, digest=digest, now=now)
            record = dict(version=1, scope=scope, session=data.last_day.isoformat(),
                          csv=merged.decode(), csv_sha256=digest, count=count,
                          response_sha256=data.response_sha256, previous_sha256=previous_hash)
            raw = _canonical(record)
            if len(raw) > MAX_BYTES:
                raise ValueError
            store._write_once(scope_path, _canonical(scope_record), 'price archive scope conflict')
            created = store._write_once(store._path('price-archive:'+data.last_day.isoformat()),
                                       raw, 'price archive conflict')
            return ArchivedPrices(data.symbol, first, data.last_day, count, digest, created, merged)
        except Exception:
            raise PriceArchiveError('private price history unavailable or changed; reconcile before retry') from None
