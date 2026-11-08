"""Verify complete daily CSV artifacts before independent strategy research.

Checks transfer integrity, OHLCV shape and cross-symbol timestamp alignment.
It cannot establish provider authenticity, exchange-calendar completeness,
corporate-action adjustments, signal causality or permission to deploy.
Daily timestamps are bar labels, never evidence of executable fill times.
"""

import csv
import hashlib
import io
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo


HEADERS = ("timestamp", "open", "high", "low", "close", "volume")
MAX_BYTES = 8_000_000
MAX_BARS = 50_000


class ResearchDataError(ValueError):
    pass


@dataclass(frozen=True)
class DailyBar:
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


@dataclass(frozen=True)
class DailyHistory:
    symbol: str
    bars: tuple[DailyBar, ...]
    sha256: str


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        return parsed.astimezone(timezone.utc)
    except (AttributeError, TypeError, ValueError):
        raise ResearchDataError("timezone-aware bar timestamp required") from None


def _number(value, *, zero_allowed=False):
    try:
        parsed = Decimal(value)
        if not parsed.is_finite() or parsed < 0 or (not zero_allowed and parsed == 0):
            raise ValueError
        return parsed
    except (InvalidOperation, TypeError, ValueError):
        raise ResearchDataError("invalid OHLCV number") from None


def read_daily_artifact(data, response, *, symbol, start, end):
    """Accept full downloaded bytes and that price_get structured response.

    The hash/count assertions come from the caller's authenticated response;
    they are not cryptographic proof of the market-data source. Never use
    model-visible bars when bars_downsampled is true: this reads the CSV only.
    The response and private artifact URLs are never retained in the result.
    """
    if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,14}", symbol):
        raise ResearchDataError("invalid configured symbol")
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_BYTES:
        raise ResearchDataError("artifact byte size outside research limit")
    if not isinstance(response, dict):
        raise ResearchDataError("price response required")
    if (response.get("status") != "ok" or response.get("symbol") != symbol
            or response.get("tf") != "1d" or response.get("delivery") != "download"
            or response.get("limit_capped") is not False or response.get("has_more") is not False):
        raise ResearchDataError("incomplete or wrong-scope daily response")
    metadata = response.get("series_metadata", {})
    if not isinstance(metadata, dict) or any(metadata.get(k) != v for k, v in {
        "asset_type": "stock", "price_kind": "ohlcv", "synthetic_ohlc": False,
        "volume_available": True,
    }.items()):
        raise ResearchDataError("unsupported daily price metadata")
    # Booleans must be actual booleans, not JSON numbers that compare equal.
    if metadata["synthetic_ohlc"] is not False or metadata["volume_available"] is not True:
        raise ResearchDataError("unsupported daily price metadata")
    artifact = response.get("artifact")
    if not isinstance(artifact, dict) or artifact.get("format") != "csv":
        raise ResearchDataError("CSV artifact metadata required")
    count = response.get("count")
    if type(count) is not int or not 0 < count <= MAX_BARS:
        raise ResearchDataError("invalid daily row count")
    # source_count is optional in official responses; the artifact row count
    # and response count remain mandatory. If supplied, it must also agree.
    counts = [artifact.get("row_count")]
    if "source_count" in response:
        counts.append(response["source_count"])
    for value in counts:
        if type(value) is not int or value != count:
            raise ResearchDataError("daily row counts disagree")
    if type(artifact.get("size_bytes")) is not int or artifact["size_bytes"] != len(data):
        raise ResearchDataError("artifact byte count mismatch")
    digest = hashlib.sha256(data).hexdigest()
    if artifact.get("sha256") != digest:
        raise ResearchDataError("artifact checksum mismatch")
    left, right = _timestamp(start), _timestamp(end)
    if left >= right:
        raise ResearchDataError("invalid research time range")
    try:
        reader = csv.DictReader(io.StringIO(data.decode("utf-8"), newline=""), strict=True)
        if tuple(reader.fieldnames or ()) != HEADERS:
            raise ResearchDataError("unexpected CSV columns")
        bars, days = [], set()
        for row in reader:
            if len(bars) >= count or set(row) != set(HEADERS) or any(v is None for v in row.values()):
                raise ResearchDataError("CSV row shape or count mismatch")
            stamp = _timestamp(row["timestamp"])
            day = stamp.astimezone(ZoneInfo("America/New_York")).date()
            if not left <= stamp <= right or (bars and stamp <= bars[-1].timestamp) or day in days:
                raise ResearchDataError("duplicate, unordered or out-of-range daily bar")
            op, high, low, close = (_number(row[k]) for k in HEADERS[1:5])
            volume = _number(row["volume"], zero_allowed=True)
            if low > min(op, close) or high < max(op, close) or low > high:
                raise ResearchDataError("inconsistent OHLC prices")
            bars.append(DailyBar(stamp, op, high, low, close, volume))
            days.add(day)
        if len(bars) != count:
            raise ResearchDataError("CSV row count mismatch")
    except (UnicodeError, csv.Error):
        raise ResearchDataError("unreadable daily CSV") from None
    return DailyHistory(symbol, tuple(bars), digest)


def alignment_report(histories):
    """Report missing observed labels without dropping/forward-filling any row.

    Shared gaps in ALL series remain undetectable here. An aligned result
    still needs exchange-calendar, adjustment and causal execution validation.
    """
    histories = tuple(histories)
    if not 1 <= len(histories) <= 20 or any(not isinstance(x, DailyHistory) or not x.bars for x in histories):
        raise ResearchDataError("nonempty verified daily histories required")
    if len({x.symbol for x in histories}) != len(histories):
        raise ResearchDataError("duplicate research symbol")
    labels = {x.symbol: {b.timestamp for b in x.bars} for x in histories}
    union = set.union(*labels.values())
    missing = {symbol: [t.isoformat() for t in sorted(union - stamps)] for symbol, stamps in labels.items()}
    return {
        "aligned": not any(missing.values()),
        "missing_bar_labels": missing,
        "observed_union_count": len(union),
        "calendar_verified": False,
        "corporate_actions_verified": False,
        "deployment_approved": False,
    }


def require_aligned_history(histories):
    report = alignment_report(histories)
    if not report["aligned"]:
        raise ResearchDataError("daily histories have missing or inconsistent bar labels")
    return report
