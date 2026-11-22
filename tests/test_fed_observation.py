"""Synthetic feed observations; stdlib tests also collected by pytest CI."""
from dataclasses import asdict, FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
import hashlib
import unittest
from unittest.mock import patch

from astral_stock_agent.news_evidence import evaluate_news
from astral_stock_agent.fed_observation import (
    observe_fed_rss, compare_fed_observations, FedObservationError, ERROR)

NOW = datetime(2026, 1, 8, 15, tzinfo=timezone.utc)
RAW = (b'<rss version="2.0"><channel><title>Synthetic feed</title><item>'
       b'<guid>synthetic-document</guid><link>https://www.federalreserve.gov/synthetic</link>'
       b'<title>Synthetic notice</title><description>Synthetic body</description>'
       b'<pubDate>Thu, 08 Jan 2026 14:00:00 GMT</pubDate></item></channel></rss>')


def observe(raw=RAW, **changes):
    opts = dict(received_at=NOW, as_of=NOW, max_age=timedelta(hours=2), collection_status='complete')
    opts.update(changes)
    return observe_fed_rss(raw, **opts)


class ObservationTests(unittest.TestCase):
    def test_hash_scope_and_no_raw_retention_or_approval(self):
        result = observe()
        self.assertEqual(result.response_sha256, hashlib.sha256(RAW).hexdigest())
        self.assertEqual(result.response_bytes, len(RAW))
        self.assertEqual(result.max_age, timedelta(hours=2))
        self.assertEqual(result.batch.status, 'insufficient')
        self.assertFalse(result.source_authenticity_verified)
        self.assertFalse(result.deployment_approved)
        self.assertNotIn(RAW, asdict(result).values())
        self.assertNotIn('Synthetic notice', repr(result))
        self.assertNotIn('federalreserve.gov', repr(result))
        with self.assertRaises(FrozenInstanceError):
            result.response_bytes = 1

    def test_same_raw_reobserved_without_refreshing_first_supplied_time(self):
        old = observe()
        new = observe(received_at=NOW+timedelta(minutes=15), as_of=NOW+timedelta(minutes=15))
        result = compare_fed_observations(old, new)
        self.assertEqual(result.status, 'same_supplied_response_and_metadata')
        self.assertFalse(result.response_changed)
        self.assertEqual(result.metadata.changes[0].after[0].earliest_supplied_observation, NOW)
        self.assertEqual(old.received_at, NOW)
        self.assertFalse(result.correction_verified)
        self.assertFalse(result.research_eligibility_evaluated)
        self.assertFalse(result.deployment_approved)

    def test_description_and_channel_or_whitespace_changes_are_not_corrections(self):
        for raw in (RAW.replace(b'Synthetic body', b'Synthetic different body'),
                    RAW.replace(b'Synthetic feed', b'Synthetic channel update'),
                    RAW.replace(b'<channel>', b'<channel>\n')):
            with self.subTest(size=len(raw)):
                result = compare_fed_observations(observe(), observe(raw))
                self.assertEqual(result.status, 'response_changed_metadata_unchanged')
                self.assertTrue(result.response_changed)
                self.assertFalse(result.correction_verified)
                self.assertEqual(result.metadata.changes[0].kind, 'reobserved_metadata')

    def test_same_conflicting_response_stays_reviewable_not_inconsistent(self):
        item = RAW[RAW.index(b'<item>'):RAW.index(b'</item>')+7]
        raw = RAW.replace(b'</channel>', item.replace(b'Synthetic notice', b'Conflicting notice')+b'</channel>')
        old = observe(raw)
        result = compare_fed_observations(old, old)
        self.assertEqual(result.status, 'retained_metadata_review_required')
        self.assertFalse(result.response_changed)
        self.assertEqual(result.metadata.changes[0].kind, 'metadata_conflict_review_required')

    def test_title_change_is_review_only_and_missing_doc_is_not_withdrawal(self):
        result = compare_fed_observations(observe(), observe(RAW.replace(b'Synthetic notice', b'Changed notice')))
        self.assertEqual(result.status, 'retained_metadata_review_required')
        self.assertEqual(result.metadata.changes[0].kind, 'metadata_change_review_required')
        empty = observe(b'<rss version="2.0"><channel/></rss>')
        result = compare_fed_observations(observe(), empty)
        self.assertEqual(result.metadata.changes[0].kind, 'absent_from_supplied_batch')
        self.assertFalse(result.correction_verified)

    def test_partial_failed_and_future_filtered_inputs_never_become_same_response(self):
        for status in ('partial', 'failed'):
            with self.subTest(status=status):
                result = compare_fed_observations(observe(), observe(collection_status=status))
                self.assertEqual(result.status, 'comparison_unavailable')
                self.assertIsNone(result.response_changed)
                self.assertEqual(result.metadata.changes, ())
        future = observe(as_of=NOW-timedelta(minutes=1))
        result = compare_fed_observations(future, future)
        self.assertEqual(result.status, 'comparison_unavailable')

    def test_future_receipts_block_empty_and_nonempty_comparisons(self):
        empty = b'<rss version="2.0"><channel/></rss>'
        for raw in (empty, RAW):
            for changed in (False, True):
                supplied = raw.replace(b'<channel', b'<channel ', 1) if changed else raw
                old = observe(raw)
                future = observe(supplied, received_at=NOW+timedelta(hours=1))
                for previous, current, label in (
                        (old, future, 'current'),
                        (future, observe(supplied, received_at=NOW+timedelta(hours=2),
                                         as_of=NOW+timedelta(hours=2)), 'previous')):
                    with self.subTest(empty=raw == empty, changed=changed, label=label):
                        result = compare_fed_observations(previous, current)
                        self.assertEqual(result.status, 'comparison_unavailable')
                        self.assertIsNone(result.response_changed)
                        self.assertEqual(result.metadata.status, 'unavailable')
                        self.assertIn(label+'_receipt_after_as_of', result.metadata.reasons)
                        self.assertEqual(result.metadata.changes, ())
                        self.assertFalse(result.correction_verified)
                        self.assertFalse(result.research_eligibility_evaluated)
                        self.assertFalse(result.deployment_approved)

    def test_receipt_at_cutoff_accepts_empty_and_nonempty_utc_equivalents(self):
        for raw in (b'<rss version="2.0"><channel/></rss>', RAW):
            for offset in (0, 9, -5):
                with self.subTest(empty=raw != RAW, offset=offset):
                    stamp = NOW.astimezone(timezone(timedelta(hours=offset)))
                    value = observe(raw, received_at=stamp, as_of=NOW)
                    result = compare_fed_observations(value, value)
                    self.assertEqual(result.status, 'same_supplied_response_and_metadata')
                    self.assertFalse(result.response_changed)
                    self.assertEqual(result.metadata.reasons, ())

    def test_new_evaluation_can_be_stale_even_when_raw_and_metadata_match(self):
        old, new = observe(), observe(as_of=NOW+timedelta(hours=3))
        self.assertIn('stale', new.batch.assessments[0].reasons)
        result = compare_fed_observations(old, new)
        self.assertEqual(result.status, 'same_supplied_response_and_metadata')
        self.assertFalse(result.research_eligibility_evaluated)

    def test_timezone_normalization_and_no_clock_or_io_needed(self):
        with patch('socket.socket', side_effect=AssertionError('no network')), \
                patch('builtins.open', side_effect=AssertionError('no files')):
            result = observe(received_at=NOW.astimezone(timezone(timedelta(hours=9))))
            self.assertEqual(result.received_at, NOW)
            self.assertEqual(compare_fed_observations(result, result).response_changed, False)

    def test_invalid_raw_receipt_or_future_publication_uses_fixed_error(self):
        cases = [dict(raw=None), dict(raw=b''), dict(raw=b'x'*256001),
                 dict(raw=RAW.replace(b'14:00:00', b'16:00:00')),
                 dict(received_at=NOW.replace(tzinfo=None)), dict(received_at='synthetic')]
        for opts in cases:
            with self.subTest(fields=tuple(opts)):
                with self.assertRaises(FedObservationError) as error:
                    observe(**opts)
                self.assertEqual(str(error.exception), ERROR)

    def test_changed_scope_forged_hash_count_or_mismatched_receipt_rejected(self):
        good = observe()
        changed = observe(RAW.replace(b'Synthetic notice', b'Changed notice'))
        cases = [replace(good, feed_url='https://attacker.invalid/feed'),
                 replace(good, parser_profile='unreviewed-version'),
                 replace(good, response_bytes=True), replace(good, response_bytes=0),
                 replace(good, response_sha256='not-a-hash'),
                 replace(good, max_age=timedelta(0)), replace(good, max_age=timedelta(hours=1)),
                 replace(good, source_authenticity_verified=True),
                 replace(good, received_at=NOW+timedelta(minutes=1)),
                 replace(good, response_bytes=len(RAW)+1),
                 replace(changed, response_sha256=good.response_sha256)]
        for value in cases:
            with self.subTest(type=type(value).__name__):
                with self.assertRaises(FedObservationError) as error:
                    compare_fed_observations(good, value)
                self.assertEqual(str(error.exception), ERROR)

    def test_other_valid_metadata_cannot_claim_the_fixed_parser_profile(self):
        good = observe()
        original = asdict(good.batch.assessments[0].evidence)
        for key in ('published_at', 'observed_at', 'scheduled_for'):
            if original[key] is not None:
                original[key] = original[key].isoformat()
        original['schema'] = 'news_evidence_v1'
        changes = [dict(source_id='synthetic-other-provider'),
                   dict(publisher='Synthetic other publisher'), dict(version_id='invented-version'),
                   dict(release_stage='released', vintage='initial'), dict(quality_flags=[]),
                   dict(summary='Synthetic extra summary')]
        for fields in changes:
            with self.subTest(fields=tuple(fields)):
                records = [dict(original, **fields)]
                other = evaluate_news(records, as_of=NOW, max_age=timedelta(hours=2),
                                      collection_status='complete')
                mismatched = replace(good, batch=other)
                with self.assertRaises(FedObservationError):
                    compare_fed_observations(mismatched, mismatched)

    def test_reverse_receipt_and_evaluation_order_are_rejected(self):
        later = observe(received_at=NOW+timedelta(minutes=1), as_of=NOW+timedelta(minutes=1))
        with self.assertRaises(FedObservationError):
            compare_fed_observations(later, observe())
        with self.assertRaises(FedObservationError):
            compare_fed_observations(observe(as_of=NOW+timedelta(hours=1)), observe())


if __name__ == '__main__':
    unittest.main()
