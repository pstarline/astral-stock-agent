"""Bounded offline news metadata proposal for role 1/2 review.

No provider, network, persistence, market classification, strategy, order or
reporting integration. Caller declarations are not source authentication. Only
minimal headlines/summaries belong here, never full articles or credentials.
Times use the existing aware-timestamp parser; no market/session code is copied.
Economic numeric values and consensus estimates are deliberately not supported
by this first metadata contract. Scheduled notices cannot become release values.
"""
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import re
import unicodedata
from urllib.parse import urlsplit

from .research_data import _timestamp


MAX_ITEMS = 100
MAX_BYTES = 256_000
ERROR = 'news evidence invalid; explicit bounded provenance and aware times required'
FIELDS = frozenset(('schema', 'source_id', 'publisher', 'document_id', 'version_id',
    'event_id', 'event_link_basis', 'source_url', 'title', 'summary', 'published_at',
    'observed_at', 'event_date', 'reference_period', 'scheduled_for', 'release_stage',
    'vintage', 'source_status', 'quality_flags'))
QUALITY_FLAGS = frozenset(('unverified', 'delayed', 'missing', 'corrected', 'conflicting'))


class NewsEvidenceError(ValueError):
    pass


@dataclass(frozen=True, repr=False)
class NewsEvidence:
    """Private, minimal caller evidence; all external content hidden from repr."""
    source_id: str
    publisher: str
    document_id: str
    version_id: str | None
    event_id: str | None
    event_link_basis: str | None
    source_url: str
    title: str
    summary: str | None
    published_at: datetime | None
    observed_at: datetime
    event_date: date | None
    reference_period: str | None
    scheduled_for: datetime | None
    release_stage: str
    vintage: str
    source_status: str
    quality_flags: tuple[str, ...]

    def __repr__(self):
        return 'NewsEvidence(private metadata)'


@dataclass(frozen=True)
class NewsAssessment:
    evidence: NewsEvidence = field(repr=False)
    evidence_sha256: str
    eligible_for_research: bool
    release_notice_available: bool
    valid_until: datetime | None
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class NewsBatch:
    status: str
    as_of: datetime
    assessments: tuple[NewsAssessment, ...] = field(repr=False)
    duplicates: int
    not_yet_observed: int
    collection_status: str
    source_authenticity_verified: bool = False
    deployment_approved: bool = False


def _text(value, limit, *, optional=False):
    if value is None and optional:
        return None
    if (type(value) is not str or not value.strip() or len(value) > limit
            or any(unicodedata.category(c).startswith('C') for c in value)):
        raise ValueError
    return value


def _stamp(value, *, optional=False):
    if value is None and optional:
        return None
    # The shared parser rejects naive dates/times and preserves their instant.
    _text(value, 64)
    return _timestamp(value)


def _url(value):
    _text(value, 1000)
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or not parsed.hostname
            or parsed.netloc != parsed.hostname or parsed.query or parsed.fragment
            or not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', parsed.hostname)
            or '.' not in parsed.hostname):
        raise ValueError
    # No URL is fetched here. A syntactically valid link is not verified provenance.
    return value


def _normalize(raw):
    if type(raw) is not dict or set(raw) != FIELDS or raw['schema'] != 'news_evidence_v1':
        raise ValueError
    source_id = _text(raw['source_id'], 100)
    publisher = _text(raw['publisher'], 200)
    document_id = _text(raw['document_id'], 200)
    version_id = _text(raw['version_id'], 100, optional=True)
    event_id = _text(raw['event_id'], 200, optional=True)
    basis = _text(raw['event_link_basis'], 400, optional=True)
    if (event_id is None) != (basis is None):
        raise ValueError
    published = _stamp(raw['published_at'], optional=True)
    observed = _stamp(raw['observed_at'])
    scheduled = _stamp(raw['scheduled_for'], optional=True)
    if published is not None and published > observed:
        raise ValueError
    event_day = raw['event_date']
    if event_day is not None:
        _text(event_day, 10)
        event_day = date.fromisoformat(event_day)
        if event_day.isoformat() != raw['event_date']:
            raise ValueError
    stage, vintage, status = (raw[k] for k in ('release_stage', 'vintage', 'source_status'))
    if (type(stage) is not str or stage not in ('scheduled', 'released', 'unknown')
            or type(vintage) is not str or vintage not in ('initial', 'revised', 'unknown')
            or type(status) is not str or status not in ('available', 'missing', 'failed')
            or (stage != 'released' and vintage != 'unknown')):
        raise ValueError
    flags = raw['quality_flags']
    if (type(flags) not in (list, tuple) or len(flags) > len(QUALITY_FLAGS)
            or any(type(x) is not str or x not in QUALITY_FLAGS for x in flags)
            or len(set(flags)) != len(flags)):
        raise ValueError
    return NewsEvidence(source_id, publisher, document_id, version_id, event_id,
        basis, _url(raw['source_url']), _text(raw['title'], 300),
        _text(raw['summary'], 800, optional=True), published, observed, event_day,
        _text(raw['reference_period'], 100, optional=True), scheduled, stage, vintage,
        status, tuple(sorted(flags)))


def _content(evidence):
    # Observation time is not document identity: a repeated fetch is not news.
    result = dict(vars(evidence))
    result.pop('observed_at')
    for key, value in result.items():
        if isinstance(value, (datetime, date)):
            result[key] = value.isoformat()
    return json.dumps(result, sort_keys=True, ensure_ascii=True, separators=(',', ':'))


def _key(evidence):
    # An absent version stays absent. Content digests never become provider versions.
    return evidence.source_id, evidence.document_id, evidence.version_id or ''


def evaluate_news(records, *, as_of, max_age, collection_status):
    """Return deterministic offline research eligibility, never verified facts.

    All fields are explicit, including None for unknown optional evidence. An
    unknown timezone is rejected, never repaired. Caller must supply complete,
    partial or failed collection status and a positive research freshness limit.
    A record exists for historical analysis only once observed AND published.
    Freshness expires at publication + max_age (exclusive), not the last fetch.

    Exact repeated observations collapse to the earliest visible observation.
    Conflicting content under one document/version is retained and blocked.
    Multiple visible versions are retained and blocked for revision review;
    this module cannot authenticate or automatically choose a correction.
    Explicit cross-source event links are preserved without merging articles or
    counting news frequency. Semantic cross-source conflict detection is absent;
    callers must mark known conflicts, not assume unflagged content agrees.

    Partial/failed/empty inputs cannot masquerade as normal market conditions.
    Eligibility means metadata meets these limited checks only; it does not
    verify a source, a headline's truth, economic values or strategy suitability.
    Input/output content must remain private unless publication rights are known.
    """
    try:
        if (type(records) not in (list, tuple) or len(records) > MAX_ITEMS
                or type(as_of) is not datetime or as_of.tzinfo is None
                or as_of.utcoffset() is None or type(max_age) is not timedelta
                or not timedelta(0) < max_age <= timedelta(days=30)
                or type(collection_status) is not str
                or collection_status not in ('complete', 'partial', 'failed')):
            raise ValueError
        cutoff = as_of.astimezone(timezone.utc)
        visible, future, byte_count = [], 0, 0
        for raw in records:
            evidence = _normalize(raw)
            # Validate bounded flat fields BEFORE serialization; never traverse
            # arbitrary raw nested payloads, even for an input-size estimate.
            byte_count += len(_content(evidence).encode()) + len(evidence.observed_at.isoformat())
            if byte_count > MAX_BYTES:
                raise ValueError
            if evidence.observed_at > cutoff:
                future += 1
            else:
                visible.append(evidence)
        unique = {}
        for evidence in sorted(visible, key=lambda e: e.observed_at):
            unique.setdefault((_key(evidence), _content(evidence)), evidence)
        duplicates = len(visible) - len(unique)
        identities, documents = {}, {}
        for key, content in unique:
            identities.setdefault(key, set()).add(content)
            documents.setdefault(key[:2], set()).add(key[2])
        assessments = []
        for (key, content), evidence in sorted(unique.items(), key=lambda item: item[0]):
            reasons = []
            if collection_status != 'complete':
                reasons.append('collection_' + collection_status)
            if evidence.source_status != 'available':
                reasons.append('source_' + evidence.source_status)
            reasons.extend('quality_' + x for x in evidence.quality_flags)
            if evidence.published_at is None:
                reasons.append('publication_time_unknown')
                deadline = None
            else:
                deadline = evidence.published_at + max_age
                if cutoff >= deadline:
                    reasons.append('stale')
            if evidence.release_stage == 'unknown':
                reasons.append('release_stage_unknown')
            if evidence.release_stage == 'released' and evidence.vintage == 'unknown':
                reasons.append('vintage_unknown')
            if evidence.release_stage == 'scheduled' and evidence.scheduled_for is None:
                reasons.append('scheduled_time_unknown')
            if (evidence.release_stage == 'scheduled' and evidence.scheduled_for is not None
                    and evidence.scheduled_for <= cutoff):
                reasons.append('scheduled_release_unconfirmed')
            if evidence.vintage == 'revised':
                reasons.append('revision_history_unconfirmed')
            if len(identities[key]) > 1:
                reasons.append('document_conflict')
            if len(documents[key[:2]]) > 1:
                reasons.append('revision_review_required')
            eligible = not reasons
            assessments.append(NewsAssessment(evidence,
                hashlib.sha256(content.encode()).hexdigest(), eligible,
                eligible and evidence.release_stage == 'released', deadline, tuple(reasons)))
        ready = bool(assessments) and all(a.eligible_for_research for a in assessments)
        return NewsBatch('ready_for_research' if ready else 'insufficient', cutoff,
                         tuple(assessments), duplicates, future, collection_status)
    except Exception:
        # Do not reflect external text, source URLs, unknown fields or exceptions.
        raise NewsEvidenceError(ERROR) from None
