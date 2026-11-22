"""Synthetic-only codec boundaries, no original feed or price data."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import patch

from astral_stock_agent.fed_observation import observe_fed_rss, compare_fed_observations
from astral_stock_agent.fed_observation_codec import (
    encode_fed_observation as encode, decode_fed_observation as decode,
    FedObservationCodecError, ERROR)
from astral_stock_agent.news_evidence import MAX_BYTES

T = datetime(2026, 1, 8, 15, tzinfo=timezone.utc)
ITEM = (b'<item><guid>synthetic</guid><title>Synthetic notice</title>'
        b'<link>https://www.federalreserve.gov/synthetic</link>'
        b'<pubDate>Thu, 08 Jan 2026 14:00:00 GMT</pubDate></item>')


def observation(items=ITEM, **options):
    args = dict(received_at=T, as_of=T, max_age=timedelta(hours=2), collection_status='complete')
    args.update(options)
    return observe_fed_rss(b'<rss version="2.0"><channel>'+items+b'</channel></rss>', **args)


class CodecTests(unittest.TestCase):
    def test_roundtrip_preserves_comparison_and_counters(self):
        for items in (b'', ITEM, ITEM+ITEM,
                      ITEM+ITEM.replace(b'Synthetic notice', b'Conflicting notice')):
            for as_of in (T-timedelta(seconds=1), T, T+timedelta(hours=3)):
                for status in ('complete', 'partial', 'failed'):
                    with self.subTest(count=items.count(b'<item>'), as_of=as_of, status=status):
                        value = observation(items, as_of=as_of, collection_status=status)
                        original = repr(value)
                        restored = decode(encode(value))
                        self.assertEqual(restored, value)
                        self.assertEqual(compare_fed_observations(value, value),
                                         compare_fed_observations(restored, restored))
                        self.assertEqual(repr(value), original)
                        self.assertFalse(restored.deployment_approved)
                        self.assertFalse(restored.source_authenticity_verified)
        self.assertEqual(decode(encode(observation(ITEM+ITEM))).batch.duplicates, 1)
        self.assertEqual(decode(encode(observation(ITEM+ITEM, as_of=T-timedelta(seconds=1))))
                         .batch.not_yet_observed, 2)

    def test_canonical_determinism_and_no_io(self):
        value = observation(max_age=timedelta(microseconds=1234567))
        with patch('socket.socket', side_effect=AssertionError), patch('builtins.open', side_effect=AssertionError):
            raw = encode(value)
            self.assertEqual(encode(decode(raw)), raw)
        self.assertNotIn(b'<rss', raw)
        self.assertIs(type(raw), bytes)

    def test_invalid_shapes_sizes_and_json_are_fixed_errors(self):
        for raw in (None, '', b'', b'{', b'[]', b'null', b'{}', b'x'*(MAX_BYTES+1),
                    b'{"schema":"x","schema":"x","observation":{}}',
                    b'{"schema":NaN,"observation":{}}', b'\xff'):
            with self.subTest(kind=type(raw).__name__):
                self.rejected(raw)

    def rejected(self, raw):
        with self.assertRaises(FedObservationCodecError) as error:
            decode(raw)
        self.assertEqual(str(error.exception), ERROR)

    def test_closed_fields_at_every_level(self):
        original = json.loads(encode(observation()))
        paths = [(), ('observation',), ('observation','batch'),
                 ('observation','batch','assessments',0),
                 ('observation','batch','assessments',0,'evidence')]
        for path in paths:
            for operation in ('add', 'remove'):
                doc = json.loads(json.dumps(original)); target = doc
                for key in path:
                    target = target[key]
                if operation == 'add':
                    target['schema' if path and path[-1] == 'evidence' else 'extra'] = 'forbidden'
                else:
                    target.pop(next(iter(target)))
                with self.subTest(path=path, operation=operation):
                    self.rejected(json.dumps(doc).encode())

    def test_tampered_derived_fields_types_times_and_authority(self):
        changes = [(('schema',), 'unknown'), (('observation','feed_url'), 'https://invalid.example/feed'),
                   (('observation','parser_profile'), 'other'), (('observation','received_at'), '2026-01-08T15:00:00'),
                   (('observation','response_bytes'), True), (('observation','max_age'), True),
                   (('observation','max_age'), 1.0), (('observation','max_age'), 0),
                   (('observation','source_authenticity_verified'), True),
                   (('observation','deployment_approved'), True),
                   (('observation','batch','duplicates'), True),
                   (('observation','batch','not_yet_observed'), 101),
                   (('observation','batch','not_yet_observed'), 1),
                   (('observation','batch','assessments',0,'evidence_sha256'), '0'*64),
                   (('observation','batch','assessments',0,'reasons'), []),
                   (('observation','batch','assessments',0,'valid_until'), T.isoformat()),
                   (('observation','batch','assessments',0,'eligible_for_research'), 0)]
        for path, value in changes:
            doc = json.loads(encode(observation())); target = doc
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            with self.subTest(path=path):
                self.rejected(json.dumps(doc).encode())

    def test_encoded_size_and_nested_duplicate_keys(self):
        raw = encode(observation())
        self.rejected(raw.replace(b'"duplicates":0', b'"duplicates":0,"duplicates":0'))
        value = observation()
        e = value.batch.assessments[0].evidence
        # Valid bounded Unicode fields can exceed the encoded JSON limit after
        # ASCII escaping. Reject rather than truncating retained metadata.
        from astral_stock_agent.news_evidence import evaluate_news
        from dataclasses import asdict
        records = []
        for i in range(100):
            record = asdict(e)
            record.update(schema='news_evidence_v1', document_id=str(i), title='界'*300)
            for key in ('observed_at', 'published_at'):
                record[key] = record[key].isoformat()
            records.append(record)
        batch = evaluate_news(records, as_of=T, max_age=value.max_age, collection_status='complete')
        with self.assertRaises(FedObservationCodecError):
            encode(replace(value, batch=batch))

    def test_counter_logical_contradictions(self):
        for value in (observation(b''), observation(b'', as_of=T-timedelta(seconds=1))):
            doc = json.loads(encode(value)); doc['observation']['batch']['duplicates'] = 1
            self.rejected(json.dumps(doc).encode())
        value = observation()
        bad = replace(value, batch=replace(value.batch, assessments=(
            replace(value.batch.assessments[0], reasons=()),)))
        with self.assertRaises(FedObservationCodecError):
            encode(bad)


if __name__ == '__main__':
    unittest.main()
