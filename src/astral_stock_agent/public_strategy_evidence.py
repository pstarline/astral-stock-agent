"""Proposed, unconnected public-source research record; no executable strategy.

Callers supply their own lawful summaries, never full sources. Declared terms
and rule statements are observations, not rights/authenticity/equivalence checks.
No retired-platform formats, collection, persistence or validation runner here.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import unicodedata
from urllib.parse import urlsplit


ERROR = 'public research evidence rejected; check explicit source and findings'
TOPICS = ('entry', 'exit', 'sizing', 'position', 'protection', 'bar_interval',
          'timezone', 'execution')
SOURCE_FIELDS = {'kind', 'url', 'publication_id', 'author', 'version', 'published_at',
                 'observed_at', 'license_name', 'license_url', 'permission_status',
                 'permission_basis'}


class PublicEvidenceError(ValueError):
    pass


@dataclass(frozen=True)
class RuleFinding:
    topic: str
    state: str
    summary: str | None = field(repr=False)
    locators: tuple[str, ...] = field(repr=False)
    limitations: tuple[str, ...] = field(repr=False)


@dataclass(frozen=True)
class PublicEvidence:
    source: tuple = field(repr=False)
    findings: tuple[RuleFinding, ...] = field(repr=False)
    evidence_sha256: str
    # Neither complete statements nor a permission declaration grants authority.
    validation_status: str = field(default='needs_independent_review', init=False)
    source_authenticity_verified: bool = field(default=False, init=False)
    source_equivalence_verified: bool = field(default=False, init=False)
    selection_approved: bool = field(default=False, init=False)
    deployment_approved: bool = field(default=False, init=False)
    new_orders_allowed: bool = field(default=False, init=False)


def _text(value, limit, optional=False):
    if value is None and optional:
        return None
    if (type(value) is not str or not value.strip() or len(value) > limit
            or any(unicodedata.category(c).startswith('C') for c in value)):
        raise ValueError
    return value


def _url(value, optional=False):
    if value is None and optional:
        return None
    _text(value, 2048)
    parsed = urlsplit(value)
    host = parsed.hostname
    if (parsed.scheme != 'https' or not host or not host.isascii() or '.' not in host
            or host.endswith('.') or any(c.isspace() for c in value)
            or parsed.username is not None or parsed.password is not None
            or parsed.port is not None or parsed.query or parsed.fragment
            or ':' in host or all(c.isdigit() or c == '.' for c in host)
            or '\\' in value):
        raise ValueError
    return value


def _stamp(value):
    _text(value, 48)
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError
    return result.astimezone(timezone.utc)


def build_public_evidence(record, *, as_of):
    """Snapshot exactly eight explicit findings, with missing facts left unknown.

    This v1 proposal is NOT the historical discovery candidate or an input to
    validation/selection. Unknown topics require a limitation, not invented text.
    The digest identifies normalized observed content, excluding retrieval time;
    it is not a provider version, signature or durable duplicate-suppression log.
    URL syntax is bounded, not a guarantee that caller text is safe to publish.
    Keep returned records private until rights/privacy review; repr hides text.
    """
    try:
        if (not isinstance(as_of, datetime) or as_of.tzinfo is None
                or as_of.utcoffset() is None or type(record) is not dict
                or set(record) != {'schema', 'source', 'findings'}
                or record['schema'] != 'public_research_evidence_v1'):
            raise ValueError
        raw = record['source']
        if type(raw) is not dict or set(raw) != SOURCE_FIELDS:
            raise ValueError
        if raw['kind'] not in ('paper', 'github_project', 'author_document'):
            raise ValueError
        source = {k: _text(raw[k], 200, optional=True)
                  for k in ('publication_id', 'author', 'version', 'license_name')}
        source.update(kind=raw['kind'], url=_url(raw['url']),
                      license_url=_url(raw['license_url'], optional=True),
                      permission_basis=_text(raw['permission_basis'], 500, optional=True))
        permission = raw['permission_status']
        if permission not in ('unconfirmed', 'restricted', 'declared_permitted'):
            raise ValueError
        if permission != 'unconfirmed' and (source['license_url'] is None
                                           or source['permission_basis'] is None):
            raise ValueError
        source['permission_status'] = permission
        observed = _stamp(raw['observed_at'])
        published = _stamp(raw['published_at']) if raw['published_at'] is not None else None
        if observed > as_of.astimezone(timezone.utc) or (published and published > observed):
            raise ValueError
        source.update(observed_at=observed.isoformat(),
                      published_at=published.isoformat() if published else None)
        rows = record['findings']
        if type(rows) is not list or len(rows) != len(TOPICS):
            raise ValueError
        findings, seen = [], set()
        for row in rows:
            if type(row) is not dict or set(row) != {'topic', 'state', 'summary', 'locators', 'limitations'}:
                raise ValueError
            topic, state = row['topic'], row['state']
            if topic not in TOPICS or topic in seen or state not in ('declared', 'interpreted', 'unconfirmed', 'conflicting'):
                raise ValueError
            seen.add(topic)
            groups = []
            for key in ('locators', 'limitations'):
                items = row[key]
                if type(items) is not list or len(items) > 4:
                    raise ValueError
                values = tuple(_text(x, 200) for x in items)
                if len(set(values)) != len(values):
                    raise ValueError
                groups.append(values)
            locators, limitations = groups
            summary = _text(row['summary'], 500, optional=True)
            if state == 'unconfirmed':
                if summary is not None or not limitations:
                    raise ValueError
            elif summary is None or not locators:
                raise ValueError
            if state in ('interpreted', 'conflicting') and not limitations:
                raise ValueError
            if state == 'conflicting' and len(locators) < 2:
                raise ValueError
            findings.append(RuleFinding(topic, state, summary, locators, limitations))
        findings = tuple(sorted(findings, key=lambda x: x.topic))
        material = dict(schema=record['schema'], source={k: v for k, v in source.items()
                        if k != 'observed_at'}, findings=[dict(topic=x.topic, state=x.state,
                        summary=x.summary, locators=x.locators, limitations=x.limitations)
                        for x in findings])
        digest = hashlib.sha256(json.dumps(material, sort_keys=True,
            separators=(',', ':'), allow_nan=False).encode()).hexdigest()
        return PublicEvidence(tuple(sorted(source.items())), findings, digest)
    except Exception:
        raise PublicEvidenceError(ERROR) from None
