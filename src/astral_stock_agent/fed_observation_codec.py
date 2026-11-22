"""Private bounded offline observation codec; no source attestation or I/O.

Only retained metadata is reconstructible. Original duplicate/future counters
are preserved declarations, not recomputed evidence of original completeness.
No raw XML, current-time refresh, storage, network or approval is provided.
"""
from dataclasses import asdict, fields, replace
from datetime import date, datetime, timedelta
import json

from .fed_observation import FedObservation, _validate, _stamp
from .news_evidence import (MAX_BYTES, MAX_ITEMS, NewsAssessment, NewsBatch, NewsEvidence, _normalize,
                            evaluate_news)

ERROR = 'invalid bounded private Fed observation encoding'
SCHEMA = 'fed_observation_json_v1'


class FedObservationCodecError(ValueError):
    pass


def _closed(value, cls):
    if type(value) is not dict or set(value) != {f.name for f in fields(cls)}:
        raise ValueError
    return value


def _instant(value):
    if type(value) is not str or len(value) > 64:
        raise ValueError
    return _stamp(datetime.fromisoformat(value))


def _integer(value):
    if type(value) is not int:
        raise ValueError
    return value


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _reject(value):
    raise ValueError


def _default(value):
    if type(value) in (datetime, date):
        return value.isoformat()
    raise ValueError


def decode_fed_observation(raw):
    """Restore one private declaration; reject inconsistent derived assessments.

    Raw response bytes are absent: their digest, counts, source authenticity,
    rights and historical first-seen cannot be independently authenticated here.
    Filtered future entries and collapsed duplicates are never synthesized.
    """
    try:
        if type(raw) is not bytes or not 0 < len(raw) <= MAX_BYTES:
            raise ValueError
        document = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_reject)
        if (type(document) is not dict or set(document) != {'schema', 'observation'}
                or document['schema'] != SCHEMA):
            raise ValueError
        value = dict(_closed(document['observation'], FedObservation))
        value['received_at'] = _instant(value['received_at'])
        micros = _integer(value['max_age'])
        if not 0 < micros <= 30 * 86400 * 1000000:
            raise ValueError
        value['max_age'] = timedelta(microseconds=micros)
        batch = dict(_closed(value['batch'], NewsBatch))
        batch['as_of'] = _instant(batch['as_of'])
        for key in ('duplicates', 'not_yet_observed'):
            _integer(batch[key])
        if type(batch['assessments']) is not list or len(batch['assessments']) > MAX_ITEMS:
            raise ValueError
        records, assessments = [], []
        for item in batch['assessments']:
            assessment = dict(_closed(item, NewsAssessment))
            evidence = assessment['evidence']
            _closed(evidence, NewsEvidence)
            evidence = _normalize(dict(evidence, schema='news_evidence_v1'))
            # _normalize's closed schema also rejects unknown evidence fields.
            records.append(dict(item['evidence'], schema='news_evidence_v1'))
            assessment['evidence'] = evidence
            if (type(assessment['eligible_for_research']) is not bool
                    or type(assessment['release_notice_available']) is not bool
                    or type(assessment['evidence_sha256']) is not str
                    or type(assessment['reasons']) is not list
                    or any(type(x) is not str for x in assessment['reasons'])):
                raise ValueError
            assessment['reasons'] = tuple(assessment['reasons'])
            if assessment['valid_until'] is not None:
                assessment['valid_until'] = _instant(assessment['valid_until'])
            assessments.append(NewsAssessment(**assessment))
        batch['assessments'] = tuple(assessments)
        restored = NewsBatch(**batch)
        value['batch'] = restored
        observation = FedObservation(**value)
        _validate(observation)
        future_receipt = observation.received_at > restored.as_of
        if ((not future_receipt and restored.not_yet_observed)
                or (future_receipt and (restored.assessments or restored.duplicates))
                or (restored.duplicates and not restored.assessments)):
            raise ValueError
        checked = evaluate_news(records, as_of=restored.as_of, max_age=value['max_age'],
                                collection_status=restored.collection_status)
        # Retained evidence cannot recover collapsed or filtered entries. Preserve
        # original bounded counters; validate only reproducible derived fields.
        if replace(checked, duplicates=restored.duplicates,
                   not_yet_observed=restored.not_yet_observed) != restored:
            raise ValueError
        return observation
    except Exception:
        raise FedObservationCodecError(ERROR) from None


def encode_fed_observation(observation):
    """Encode private metadata; this output is not sanitized for publication."""
    try:
        _validate(observation)
        value = asdict(observation)
        age = observation.max_age
        value['max_age'] = (age.days * 86400 + age.seconds) * 1000000 + age.microseconds
        raw = json.dumps({'schema': SCHEMA, 'observation': value}, default=_default,
                         sort_keys=True, ensure_ascii=True, allow_nan=False,
                         separators=(',', ':')).encode('utf-8')
        if decode_fed_observation(raw) != observation:
            raise ValueError
        return raw
    except Exception:
        raise FedObservationCodecError(ERROR) from None
