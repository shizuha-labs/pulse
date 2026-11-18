import importlib.util
from pathlib import Path
import unittest

_SPEC = importlib.util.spec_from_file_location(
    'stale_pr_review_alarm',
    Path(__file__).resolve().parents[1] / 'services' / 'stale_pr_review_alarm.py',
)
_MOD = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MOD)
stale_pr_review_decision = _MOD.stale_pr_review_decision
dispatch_stale_pr_review_alarms = _MOD.dispatch_stale_pr_review_alarms
review_record_count = _MOD.review_record_count
ci_is_all_green = _MOD.ci_is_all_green


class StalePrReviewAlarmTests(unittest.TestCase):
    def test_deploy_1599_replay_pages_capable_rung_at_four_hours(self):
        """Urgent, all-green, zero reviews at 4h must reach an executor."""
        void_lead = [
            {'name': 'review-lead', 'capable': False},
            {'name': 'merge-lane', 'capable': True},
            {'name': 'operator-oob', 'capable': True},
        ]
        decision = stale_pr_review_decision(
            priority='urgent',
            ci_green=True,
            review_count=0,
            age_hours=4,
            rungs=void_lead,
        )
        self.assertTrue(decision['qualifies'])
        self.assertEqual(decision['target'], 'merge-lane')
        self.assertFalse(decision['mechanism_failure'])

    def test_any_review_at_head_suppresses(self):
        decision = stale_pr_review_decision(
            priority='urgent',
            ci_green=True,
            review_count=1,
            age_hours=20,
        )
        self.assertFalse(decision['qualifies'])
        self.assertIsNone(decision['target'])

    def test_high_waits_twelve_hours(self):
        early = stale_pr_review_decision(
            priority='high', ci_green=True, review_count=0, age_hours=11,
        )
        late = stale_pr_review_decision(
            priority='high', ci_green=True, review_count=0, age_hours=12,
        )
        self.assertFalse(early['qualifies'])
        self.assertTrue(late['qualifies'])
        self.assertEqual(late['target'], 'review-lead')

    def test_all_void_rungs_are_a_mechanism_failure(self):
        decision = stale_pr_review_decision(
            priority='urgent',
            ci_green=True,
            review_count=0,
            age_hours=4,
            rungs=[{'name': 'review-lead', 'capable': False}],
        )
        self.assertTrue(decision['mechanism_failure'])
        self.assertIsNone(decision['target'])

    def test_dispatch_pages_only_the_capable_rung(self):
        pages = []
        result = dispatch_stale_pr_review_alarms(
            [{
                'repo': 'shizuha-labs/deploy',
                'number': 1599,
                'priority': 'urgent',
                'ci_green': True,
                'review_count': 0,
                'age_hours': 4,
                'head': 'abc',
                'rungs': [
                    {'name': 'review-lead', 'capable': False},
                    {'name': 'merge-lane', 'capable': True},
                ],
            }],
            lambda target, candidate, decision: pages.append((target, candidate['number'])),
        )
        self.assertEqual(pages, [('merge-lane', 1599)])
        self.assertEqual(review_record_count([{'state': 'COMMENT'}, {'state': 'APPROVED'}]), 1)
        self.assertFalse(ci_is_all_green({'state': 'pending'}))
        self.assertTrue(ci_is_all_green({'state': 'success'}))
        self.assertEqual(len(result['paged']), 1)
        self.assertEqual(result['failures'], [])


if __name__ == '__main__':
    unittest.main()
