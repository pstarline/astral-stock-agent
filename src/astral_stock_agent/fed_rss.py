"""Offline parser for the observed Board monetary-policy RSS shape.

Source: https://www.federalreserve.gov/feeds/feeds.htm (Monetary Policy).
No fetching, persistence or article following. Feed dates are publisher metadata,
not authenticated original-release times. Descriptions are deliberately discarded.
Output stays unverified/unknown and cannot become usable research automatically.
"""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import re
from urllib.parse import urlsplit
from xml.etree import ElementTree

from .news_evidence import evaluate_news

MAX_XML_BYTES = 256_000
MAX_ITEMS = 100
ERROR = 'invalid bounded Board RSS metadata'
ITEM_FIELDS = frozenset(('category', 'description', 'guid', 'link', 'pubDate', 'title'))
DATE = re.compile(r'(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun), \d{2} [A-Z][a-z]{2} \d{4} '
                  r'\d{2}:\d{2}:\d{2} (?:GMT|[+-]\d{4})\Z')


class FedRssError(ValueError):
    pass


def _published(value):
    if value is None:
        return None
    if not DATE.fullmatch(value) or value.endswith('-0000'):
        raise ValueError
    stamp = parsedate_to_datetime(value)
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError
    if stamp.strftime('%a') != value[:3]:
        raise ValueError
    return stamp.astimezone(timezone.utc).isoformat()


def parse_fed_monetary_rss(raw, *, observed_at, as_of, max_age, collection_status):
    """Map bounded UTF-8 RSS bytes into the EXISTING news evidence assessment.

    Caller supplies observation/evaluation times, freshness and collection status;
    this function reads no clock/network/file. Item dates are not channel dates.
    GUID stays a provider document identifier, never a version or event identity.
    Missing pubDate is retained as unknown; missing identity/title/link fails.
    Unknown release stage/vintage and unverified quality always block eligibility.
    This feed snapshot proves neither historical completeness nor current truth.
    """
    try:
        if (type(raw) is not bytes or not 0 < len(raw) <= MAX_XML_BYTES
                or type(observed_at) is not datetime or observed_at.tzinfo is None
                or observed_at.utcoffset() is None):
            raise ValueError
        xml = raw.decode('utf-8-sig')
        if ('\x00' in xml or '<!DOCTYPE' in xml.upper() or '<!ENTITY' in xml.upper()
                or re.search(r'<\?(?!xml\s)', xml, re.I)):
            raise ValueError
        declaration = re.match(r'<\?xml\s+[^?]*\?>', xml)
        if declaration:
            encoding = re.search(r'encoding\s*=\s*[\'"]([^\'"]+)[\'"]', declaration[0])
            if encoding and encoding[1].lower() not in ('utf-8', 'utf8'):
                raise ValueError
        root = ElementTree.fromstring(xml)
        # Flat observed shape only. Unknown provider shapes fail, never guess.
        if root.tag != 'rss' or root.attrib != {'version': '2.0'}:
            raise ValueError
        if len(root) != 1 or root[0].tag != 'channel' or root[0].attrib:
            raise ValueError
        channel = root[0]
        if len(channel) > MAX_ITEMS + 20:
            raise ValueError
        records = []
        for element in channel:
            if element.tag != 'item':
                # Ignore only bounded flat channel metadata, not nested payloads.
                if element.tag not in ('title', 'link', 'description', 'language',
                        'lastBuildDate', 'pubDate', 'copyright', 'docs', 'ttl') or len(element):
                    raise ValueError
                continue
            if element.attrib or len(records) >= MAX_ITEMS or len(element) > len(ITEM_FIELDS):
                raise ValueError
            fields = {}
            for child in element:
                if child.tag not in ITEM_FIELDS or child.tag in fields or len(child):
                    raise ValueError
                if child.attrib and not (child.tag == 'guid'
                        and child.attrib in ({'isPermaLink': 'true'}, {'isPermaLink': 'false'})):
                    raise ValueError
                fields[child.tag] = child.text
            if any(not fields.get(k) for k in ('guid', 'link', 'title')):
                raise ValueError
            link = urlsplit(fields['link'])
            if link.hostname != 'www.federalreserve.gov':
                raise ValueError
            records.append(dict(schema='news_evidence_v1',
                source_id='federal_reserve_board_monetary_rss',
                publisher='Board of Governors of the Federal Reserve System',
                document_id=fields['guid'], version_id=None, event_id=None,
                event_link_basis=None, source_url=fields['link'], title=fields['title'],
                summary=None, published_at=_published(fields.get('pubDate')),
                observed_at=observed_at.isoformat(), event_date=None,
                reference_period=None, scheduled_for=None, release_stage='unknown',
                vintage='unknown', source_status='available', quality_flags=['unverified']))
        return evaluate_news(records, as_of=as_of, max_age=max_age,
                             collection_status=collection_status)
    except Exception:
        raise FedRssError(ERROR) from None
