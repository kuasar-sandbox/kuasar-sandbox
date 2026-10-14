#!/usr/bin/env python3
"""Actual Actions partial-rerun shape and conservative producer regressions."""
import copy
import json
from pathlib import Path
import unittest

import actions_jobs


class ActionsProducerTests(unittest.TestCase):
    def setUp(self):
        data = json.loads(Path(__file__).with_name('fixtures').joinpath('actions-carried-success.json').read_text())
        self.a, self.b, self.carried_a, self.new_b = data['jobs']
        self.non_producers = data['non_producer_views']
        self.run_id = self.a['run_id']

    def latest(self, rows, attempt=2):
        return actions_jobs.latest_producers(rows, run_id=self.run_id, run_attempt=attempt)

    def test_real_success_view_resolves_original_id_and_attempt(self):
        selected = self.latest([self.new_b, self.carried_a, self.b, self.a])
        self.assertEqual(selected[self.a['name']], self.a)
        self.assertEqual(selected[self.new_b['name']], self.new_b)
        self.assertGreater(self.carried_a['created_at'], self.a['completed_at'])
        self.assertNotEqual(self.a['id'], self.carried_a['id'])
        self.assertEqual(self.a['steps'], self.carried_a['steps'])

    def test_multiple_carried_views_keep_one_actual_producer(self):
        third = dict(self.carried_a, id=self.carried_a['id'] + 100, run_attempt=3,
                     created_at='2026-10-14T11:00:00Z')
        self.assertEqual(self.latest([third, self.carried_a, self.a], 3)[self.a['name']], self.a)

    def test_missing_or_ambiguous_origin_is_rejected(self):
        for rows in ([self.carried_a], [self.a, self.a, self.carried_a],
                     [self.a, self.carried_a, dict(self.carried_a, id=999)]):
            with self.subTest(rows=len(rows)), self.assertRaises(ValueError):
                self.latest(rows)

    def test_carried_execution_must_match_every_remaining_field(self):
        changes = [('head_sha', 'a' * 40), ('runner_id', 123), ('runner_name', 'other'),
                   ('runner_group_name', 'other'), ('labels', ['other']),
                   ('completed_at', '2026-10-14T10:40:51Z'), ('steps', []), ('unknown_field', 'new')]
        for key, value in changes:
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.latest([self.a, dict(self.carried_a, **{key: value})])
        changed = copy.deepcopy(self.carried_a)
        changed['steps'][0]['name'] += ' changed'
        with self.assertRaises(ValueError):
            self.latest([self.a, changed])

    def test_real_skipped_views_are_current_without_becoming_successful_producers(self):
        selected = self.latest([self.a, self.carried_a, *self.non_producers])
        current = max(self.non_producers, key=lambda j: j['run_attempt'])
        self.assertEqual(selected[current['name']], current)
        self.assertEqual(selected[current['name']]['conclusion'], 'skipped')
        self.assertEqual(selected[self.a['name']], self.a)

    def test_non_success_view_cannot_revive_the_previous_success(self):
        for status, conclusion in [('in_progress', None), ('completed', 'failure'),
                                   ('completed', 'cancelled'), ('completed', 'skipped')]:
            with self.subTest(conclusion=conclusion):
                current = dict(self.carried_a, status=status, conclusion=conclusion)
                self.assertEqual(self.latest([self.a, current])[self.a['name']], current)

    def test_original_runner_group_cannot_be_replaced_by_a_sentinel(self):
        with self.assertRaises(ValueError):
            self.latest([dict(self.a, runner_group_id=123), self.carried_a])

    def test_missing_execution_fields_do_not_authorize_carry(self):
        for field in actions_jobs.EXECUTION_FIELDS:
            with self.subTest(field=field), self.assertRaises(ValueError):
                origin = copy.deepcopy(self.a)
                del origin[field]
                self.latest([origin, self.carried_a])

    def test_new_real_producer_supersedes_success_even_when_not_successful(self):
        for status, conclusion in [('in_progress', None), ('completed', 'failure'),
                                   ('completed', 'cancelled'), ('completed', 'success')]:
            with self.subTest(conclusion=conclusion):
                newest = dict(self.new_b, status=status, conclusion=conclusion)
                if status == 'completed':
                    newest['completed_at'] = '2026-10-14T11:20:00Z'
                self.assertEqual(self.latest([self.b, newest])[self.b['name']], newest)

    def test_stale_success_cannot_revive_after_an_intervening_failed_execution(self):
        failed = dict(self.a, id=999, run_attempt=2, created_at='2026-10-14T11:00:00Z',
                      started_at='2026-10-14T11:00:01Z', completed_at='2026-10-14T11:00:02Z',
                      conclusion='failure')
        third = dict(self.carried_a, id=1000, run_attempt=3, created_at='2026-10-14T11:01:00Z')
        with self.assertRaises(ValueError):
            self.latest([third, self.a, failed], 3)

    def test_current_run_and_attempt_apply_before_canonicalization(self):
        for changed in (dict(self.carried_a, run_id=999), dict(self.carried_a, run_attempt=3)):
            with self.assertRaises(ValueError):
                self.latest([self.a, changed])

    def test_missing_carry_marker_does_not_reuse_old_artifact_identity(self):
        changed = dict(self.carried_a)
        del changed['created_at']
        self.assertEqual(self.latest([self.a, changed])[self.a['name']]['id'], changed['id'])

    def test_non_utc_or_invalid_carry_time_is_rejected(self):
        for value in ('bad', '2026-10-14T10:51:28', '2026-10-14T10:51:28+03:00'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.latest([self.a, dict(self.carried_a, created_at=value)])


if __name__ == '__main__':
    unittest.main()
