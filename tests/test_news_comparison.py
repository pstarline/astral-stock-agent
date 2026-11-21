"""Unittest synthetic acceptance cases; collected by the existing pytest CI."""
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

from astral_stock_agent.news_evidence import evaluate_news
from astral_stock_agent.news_comparison import compare_news_batches, NewsComparisonError, ERROR

NOW = datetime(2026, 1, 8, 15, tzinfo=timezone.utc)


def item(**changes):
    result = dict(schema='news_evidence_v1', source_id='synthetic-agency',
        publisher='Synthetic Agency', document_id='synthetic-document', version_id=None,
        event_id=None, event_link_basis=None, source_url='https://agency.invalid/synthetic',
        title='Synthetic notice', summary=None, published_at='2026-01-08T14:00:00Z',
        observed_at='2026-01-08T14:01:00Z', event_date=None, reference_period=None,
        scheduled_for=None, release_stage='unknown', vintage='unknown',
        source_status='available', quality_flags=['unverified'])
    result.update(changes)
    return result


def batch(records=None, **changes):
    options = dict(as_of=NOW, max_age=timedelta(hours=2), collection_status='complete')
    options.update(changes)
    return evaluate_news([item()] if records is None else records, **options)


class ComparisonTests(unittest.TestCase):
    def test_reobserved_keeps_earliest_supplied_time_without_approving(self):
        old = batch()
        new = batch([item(observed_at='2026-01-08T15:10:00Z')], as_of=NOW+timedelta(minutes=15))
        result = compare_news_batches(old, new)
        self.assertEqual(result.status, 'compared_metadata_only')
        change = result.changes[0]
        self.assertEqual(change.kind, 'reobserved_metadata')
        self.assertEqual(change.after, change.before)
        self.assertEqual(change.after[0].earliest_supplied_observation,
                         datetime(2026, 1, 8, 14, 1, tzinfo=timezone.utc))
        self.assertFalse(result.research_eligibility_evaluated)
        self.assertFalse(result.deployment_approved)
        self.assertEqual((old.status, new.status), ('insufficient', 'insufficient'))

    def test_changed_metadata_is_not_an_automatic_revision(self):
        new = batch([item(title='Synthetic changed title')])
        change = compare_news_batches(batch(), new).changes[0]
        self.assertEqual(change.kind, 'metadata_change_review_required')
        self.assertEqual(new.assessments[0].evidence.version_id, None)
        self.assertEqual(new.assessments[0].evidence.vintage, 'unknown')

    def test_new_and_absent_are_only_statements_about_supplied_batches(self):
        new = batch([item(document_id='synthetic-two')])
        kinds = {c.kind for c in compare_news_batches(batch(), new).changes}
        self.assertEqual(kinds, {'newly_supplied_metadata', 'absent_from_supplied_batch'})
        self.assertEqual(compare_news_batches(batch(), batch([])).changes[0].kind,
                         'absent_from_supplied_batch')
        self.assertEqual(compare_news_batches(batch([]), batch([])).changes, ())

    def test_conflict_and_versions_stay_review_only(self):
        for records in ([item(), item(title='Synthetic conflict')],
                        [item(version_id='v1'), item(version_id='v2')]):
            with self.subTest(records=len(records)):
                new = batch(records)
                result = compare_news_batches(batch(), new)
                self.assertEqual(result.changes[0].kind, 'metadata_conflict_review_required')
                self.assertEqual(len(result.changes[0].after), 2)

    def test_partial_failed_and_filtered_future_are_not_unchanged(self):
        for side in ('previous', 'current'):
            for state in ('partial', 'failed'):
                with self.subTest(side=side, state=state):
                    args = [batch(), batch()]
                    args[side == 'current'] = batch(collection_status=state)
                    result = compare_news_batches(*args)
                    self.assertEqual(result.status, 'unavailable')
                    self.assertEqual(result.changes, ())
                    self.assertIn(side+'_collection_'+state, result.reasons)
        future = batch([item(observed_at='2026-01-08T16:00:00Z')])
        result = compare_news_batches(batch(), future)
        self.assertIn('current_future_observations_filtered', result.reasons)

    def test_other_sources_are_not_silently_compared(self):
        result = compare_news_batches(batch(), batch([item(source_id='other-synthetic')]))
        self.assertEqual(result.reasons, ('source_scope_changed',))

    def test_later_supplied_earlier_time_does_not_rewrite_before(self):
        old = batch()
        new = batch([item(observed_at='2026-01-08T14:00:00Z')])
        change = compare_news_batches(old, new).changes[0]
        self.assertEqual(change.before[0].earliest_supplied_observation.minute, 1)
        self.assertEqual(change.after[0].earliest_supplied_observation.minute, 0)
        self.assertEqual(old.assessments[0].evidence.observed_at.minute, 1)

    def test_reproducible_order_immutability_and_private_repr(self):
        records = [item(document_id='synthetic-two'), item()]
        old, new = batch(records), batch(list(reversed(records)))
        before = deepcopy((old, new))
        self.assertEqual(compare_news_batches(old, new), compare_news_batches(new, old))
        result = compare_news_batches(old, new)
        self.assertEqual((old, new), before)
        self.assertNotIn('Synthetic notice', repr(result)+repr(result.changes[0]))
        self.assertNotIn('agency.invalid', repr(result))
        with self.assertRaises(FrozenInstanceError):
            result.status = 'approved'

    def test_freshness_is_not_cached_by_equal_digest(self):
        old, new = batch(), batch(as_of=NOW+timedelta(hours=3))
        self.assertEqual(old.assessments[0].evidence_sha256, new.assessments[0].evidence_sha256)
        self.assertIn('stale', new.assessments[0].reasons)
        result = compare_news_batches(old, new)
        self.assertEqual(result.changes[0].kind, 'reobserved_metadata')
        self.assertFalse(result.research_eligibility_evaluated)

    def test_comparison_works_with_network_and_file_operations_denied(self):
        old, new = batch(), batch()
        with patch('socket.socket', side_effect=AssertionError('network forbidden')), \
                patch('builtins.open', side_effect=AssertionError('files forbidden')):
            self.assertEqual(compare_news_batches(old, new).changes[0].kind, 'reobserved_metadata')

    def test_bad_shape_forgery_and_future_evidence_fail_with_fixed_error(self):
        valid = batch()
        bad_e = replace(valid.assessments[0].evidence, title='Synthetic tampered')
        bad_a = replace(valid.assessments[0], evidence=bad_e)
        cases = [None, {}, replace(valid, as_of=NOW.replace(tzinfo=None)),
                 replace(valid, duplicates=True), replace(valid, not_yet_observed=-1),
                 replace(valid, collection_status='guessed'),
                 replace(valid, assessments=list(valid.assessments)),
                 replace(valid, source_authenticity_verified=True),
                 replace(valid, assessments=(bad_a,)),
                 replace(valid, assessments=valid.assessments*101),
                 replace(valid, as_of=NOW-timedelta(hours=2))]
        for value in cases:
            with self.subTest(type=type(value).__name__):
                with self.assertRaises(NewsComparisonError) as error:
                    compare_news_batches(valid, value)
                self.assertEqual(str(error.exception), ERROR)
        with self.assertRaises(NewsComparisonError):
            compare_news_batches(batch(as_of=NOW+timedelta(minutes=1)), valid)


if __name__ == '__main__':
    unittest.main()
