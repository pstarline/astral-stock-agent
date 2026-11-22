"""Bounded offline Board RSS observation proposal; no raw/body persistence.

The fixed scope identifies only the input expected by the existing parser. It
is not authenticated provider provenance or permission to collect. Caller must
supply genuine successful-receipt times; this module cannot attest those times.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import re
from urllib.parse import urlsplit

from .fed_rss import parse_fed_monetary_rss, MAX_XML_BYTES
from .news_evidence import NewsBatch
from .news_comparison import NewsComparison, compare_news_batches

FEED_URL = 'https://www.federalreserve.gov/feeds/press_monetary.xml'
PARSER_PROFILE = 'board_monetary_rss_observed_shape_v1'
ERROR = 'invalid bounded Board RSS observation'


class FedObservationError(ValueError):
    pass


@dataclass(frozen=True)
class FedObservation:
    received_at: datetime
    response_bytes: int
    response_sha256: str = field(repr=False)
    batch: NewsBatch = field(repr=False)
    max_age: timedelta
    feed_url: str = field(default=FEED_URL, repr=False)
    parser_profile: str = PARSER_PROFILE
    source_authenticity_verified: bool = False
    deployment_approved: bool = False


@dataclass(frozen=True)
class FedObservationComparison:
    status: str
    response_changed: bool | None
    metadata: NewsComparison = field(repr=False)
    correction_verified: bool = False
    research_eligibility_evaluated: bool = False
    deployment_approved: bool = False


def _stamp(value):
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError
    return value.astimezone(timezone.utc)


def _validate(value):
    if (type(value) is not FedObservation or type(value.feed_url) is not str
            or value.feed_url != FEED_URL or type(value.parser_profile) is not str
            or value.parser_profile != PARSER_PROFILE
            or type(value.response_bytes) is not int
            or not 0 < value.response_bytes <= MAX_XML_BYTES
            or type(value.response_sha256) is not str
            or re.fullmatch('[0-9a-f]{64}', value.response_sha256) is None
            or type(value.batch) is not NewsBatch
            or type(value.max_age) is not timedelta
            or not timedelta(0) < value.max_age <= timedelta(days=30)
            or value.source_authenticity_verified is not False
            or value.deployment_approved is not False):
        raise ValueError
    received = _stamp(value.received_at)
    # Reuse closed evidence/hash/size/time validation, never trust ready flags.
    compare_news_batches(value.batch, value.batch)
    if value.batch.status != 'insufficient':
        raise ValueError
    for assessment in value.batch.assessments:
        e = assessment.evidence
        deadline = None if e.published_at is None else e.published_at + value.max_age
        if (assessment.valid_until != deadline
                or assessment.eligible_for_research is not False
                or assessment.release_notice_available is not False
                or e.observed_at != received
                or e.source_id != 'federal_reserve_board_monetary_rss'
                or e.publisher != 'Board of Governors of the Federal Reserve System'
                or e.release_stage != 'unknown' or e.vintage != 'unknown'
                or e.quality_flags != ('unverified',)
                or urlsplit(e.source_url).hostname != 'www.federalreserve.gov'
                or any(x is not None for x in (e.version_id, e.event_id, e.event_link_basis,
                    e.summary, e.event_date, e.reference_period, e.scheduled_for))
                or e.source_status != 'available'):
            raise ValueError
    return received


def observe_fed_rss(raw, *, received_at, as_of, max_age, collection_status):
    """Hash exactly the supplied bounded bytes and parse with the existing path.

    No network, clock, files, retries, schedules or raw-byte retention. pubDate
    remains feed metadata; unknown/unverified does not become research eligible.
    Missing/failed network receipts are not synthesized here: callers must report
    them separately rather than invent bytes/times. A digest is not a version,
    signature, anonymization guarantee or authenticity/rights verification.
    """
    try:
        received = _stamp(received_at)
        batch = parse_fed_monetary_rss(raw, observed_at=received, as_of=as_of,
                                       max_age=max_age, collection_status=collection_status)
        return FedObservation(received, len(raw), hashlib.sha256(raw).hexdigest(), batch, max_age)
    except Exception:
        raise FedObservationError(ERROR) from None


def compare_fed_observations(previous, current):
    """Separate response-byte changes from retained metadata changes only.

    Whitespace/order/channel changes can change the response digest. Therefore a
    changed response with unchanged metadata never confirms a body correction.
    Same bytes do not prove unchanged source articles. Dates/coverage/completeness
    and earliest supplied observation keep all existing comparison limitations.
    This is not durable first-seen history, a storage/collection contract, or a
    strategy input. No input is mutated and no partial result becomes readiness.
    """
    try:
        if _validate(previous) > _validate(current):
            raise ValueError
        metadata = compare_news_batches(previous.batch, current.batch)
        if metadata.status == 'unavailable':
            return FedObservationComparison('comparison_unavailable', None, metadata)
        response_changed = previous.response_sha256 != current.response_sha256
        metadata_changed = any(
            tuple(x.evidence_sha256 for x in c.before) !=
            tuple(x.evidence_sha256 for x in c.after) for c in metadata.changes)
        conflict = any(c.kind == 'metadata_conflict_review_required' for c in metadata.changes)
        if not response_changed and (previous.response_bytes != current.response_bytes
                                     or metadata_changed):
            # Same fixed parser/scope and bytes cannot yield different metadata.
            raise ValueError
        if metadata_changed or conflict:
            status = 'retained_metadata_review_required'
        elif response_changed:
            status = 'response_changed_metadata_unchanged'
        else:
            status = 'same_supplied_response_and_metadata'
        return FedObservationComparison(status, response_changed, metadata)
    except Exception:
        raise FedObservationError(ERROR) from None
