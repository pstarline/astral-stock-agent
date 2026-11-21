"""Private, offline diagnostics over two supplied news metadata batches.

Not a response/body comparator, persistent observation history or shared intake
contract. Caller retains source/coverage scope and full evaluation context. No
readiness/strategy/market-fact decision is made. Supplied times are not attested.
"""
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
import hashlib
import json

from .news_evidence import (NewsBatch, NewsAssessment, NewsEvidence, MAX_ITEMS,
                            MAX_BYTES, _normalize, _content)

ERROR = 'invalid bounded news comparison inputs'


class NewsComparisonError(ValueError):
    pass


@dataclass(frozen=True)
class ObservedMetadata:
    evidence_sha256: str = field(repr=False)
    earliest_supplied_observation: datetime


@dataclass(frozen=True)
class MetadataChange:
    identity_sha256: str = field(repr=False)
    kind: str
    before: tuple[ObservedMetadata, ...] = field(repr=False)
    after: tuple[ObservedMetadata, ...] = field(repr=False)


@dataclass(frozen=True)
class NewsComparison:
    status: str
    previous_as_of: datetime
    current_as_of: datetime
    reasons: tuple[str, ...]
    changes: tuple[MetadataChange, ...] = field(repr=False)
    research_eligibility_evaluated: bool = False
    deployment_approved: bool = False


def _batch(value):
    if (type(value) is not NewsBatch or type(value.as_of) is not datetime
            or value.as_of.tzinfo is None or value.as_of.utcoffset() is None
            or type(value.collection_status) is not str
            or value.collection_status not in ('complete', 'partial', 'failed')
            or type(value.status) is not str
            or value.status not in ('ready_for_research', 'insufficient')
            or type(value.assessments) is not tuple or len(value.assessments) > MAX_ITEMS
            or type(value.not_yet_observed) is not int or value.not_yet_observed < 0
            or type(value.duplicates) is not int or value.duplicates < 0
            or len(value.assessments) + value.duplicates + value.not_yet_observed > MAX_ITEMS
            or value.source_authenticity_verified is not False
            or value.deployment_approved is not False):
        raise ValueError
    cutoff = value.as_of.astimezone(timezone.utc)
    groups, byte_count = {}, 0
    for assessment in value.assessments:
        if type(assessment) is not NewsAssessment or type(assessment.evidence) is not NewsEvidence:
            raise ValueError
        evidence = assessment.evidence
        raw = dict(vars(evidence), schema='news_evidence_v1')
        for key in ('published_at', 'observed_at', 'scheduled_for'):
            if raw[key] is not None:
                if type(raw[key]) is not datetime:
                    raise ValueError
                raw[key] = raw[key].isoformat()
        if raw['event_date'] is not None:
            if type(raw['event_date']) is not date:
                raise ValueError
            raw['event_date'] = raw['event_date'].isoformat()
        normalized = _normalize(raw)
        if normalized != evidence or evidence.observed_at > cutoff:
            raise ValueError
        content = _content(normalized)
        digest = hashlib.sha256(content.encode()).hexdigest()
        if type(assessment.evidence_sha256) is not str or assessment.evidence_sha256 != digest:
            raise ValueError
        byte_count += len(content.encode()) + len(evidence.observed_at.isoformat())
        if byte_count > MAX_BYTES:
            raise ValueError
        # Versions remain content metadata; do not manufacture a new event ID.
        group = groups.setdefault((evidence.source_id, evidence.document_id), {})
        stamp = evidence.observed_at.astimezone(timezone.utc)
        group[digest] = min(group.get(digest, stamp), stamp)
    return cutoff, groups


def _observations(group, prior=None):
    prior = prior or {}
    return tuple(ObservedMetadata(digest, min(stamp, prior.get(digest, stamp)))
                 for digest, stamp in sorted(group.items()))


def compare_news_batches(previous, current):
    """Compare retained metadata only, never bodies, economic events or eligibility.

    A new digest is review evidence, not a provider version or correction. An
    absent document means absent from the supplied batch only, not withdrawn or
    deleted. Earliest times are limited to these two supplied observations, not
    historical first existence or durable first-seen history. Before values use
    ONLY the earlier batch; later evidence cannot rewrite that earlier view.

    Partial/failed collection or filtered future observations yields unavailable,
    never 'unchanged'. Freshness/readiness must still be evaluated with original
    as_of/max_age/collection status and reasons; the content digest cannot cache
    eligibility. No evidence flags, input batches or operational state are changed.
    """
    try:
        left_time, left = _batch(previous)
        right_time, right = _batch(current)
        if left_time > right_time:
            raise ValueError
        reasons = []
        for label, batch in (('previous', previous), ('current', current)):
            if batch.collection_status != 'complete':
                reasons.append(label + '_collection_' + batch.collection_status)
            if batch.not_yet_observed:
                reasons.append(label + '_future_observations_filtered')
        left_sources = {key[0] for key in left}
        right_sources = {key[0] for key in right}
        if left_sources and right_sources and left_sources != right_sources:
            reasons.append('source_scope_changed')
        if reasons:
            return NewsComparison('unavailable', left_time, right_time, tuple(reasons), ())
        changes = []
        for key in sorted(left.keys() | right.keys()):
            before, after = left.get(key, {}), right.get(key, {})
            if len(before) > 1 or len(after) > 1:
                kind = 'metadata_conflict_review_required'
            elif not before:
                kind = 'newly_supplied_metadata'
            elif not after:
                kind = 'absent_from_supplied_batch'
            elif before.keys() == after.keys():
                kind = 'reobserved_metadata'
            else:
                kind = 'metadata_change_review_required'
            identity = hashlib.sha256(json.dumps(key, ensure_ascii=True,
                separators=(',', ':')).encode()).hexdigest()
            changes.append(MetadataChange(identity, kind, _observations(before),
                                          _observations(after, before)))
        return NewsComparison('compared_metadata_only', left_time, right_time, (), tuple(changes))
    except Exception:
        raise NewsComparisonError(ERROR) from None
