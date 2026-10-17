"""PLS-233: decision-owner direct-assignment pin must survive rebalance/failover.

A decision-gated task (e.g. a `simple`-workflow devops task needing an Architecture
sign-off) direct-assigned WITH A REASON to the decision-owner must not be re-resolved
back to the symptom-team pool by the rebalance sweep or the availability failover drain
(the CTX-274/278 3x bounce). These tests cover the persisted marker + the guard's
assignee-match auto-release semantics that the sweep/failover exclusions rely on.
"""
from unittest.mock import patch

from django.test import TestCase
from django.contrib.auth import get_user_model
from django.urls import reverse

from tasks.models import Item, TeamRoster
from tasks.tasks import (
    _decision_pinned_team, _is_decision_pinned, _repin_decision_assignment,
    _resolve_decision_pin_failover_target,
)
from tasks.services.workflow_service import WorkflowService
from tasks import views


class DecisionPinTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username='u', email='u@shizuha.com', password='x')
        TeamRoster.objects.update_or_create(
            team_name='architecture',
            defaults={'tiers': [['aoi@shizuha.com', 'nova@shizuha.com']], 'is_active': True},
        )
        TeamRoster.objects.update_or_create(
            team_name='devops',
            defaults={'tiers': [['ichi@shizuha.com', 'san@shizuha.com']], 'is_active': True},
        )
        WorkflowService._invalidate_team_roster_caches()

    def _item(self, **kw):
        return Item.objects.create(
            mode='task', title='decision-gated', workflow_name='simple',
            created_by_id=self.user.id, **kw,
        )

    def test_persist_and_guard_match(self):
        """Direct-assign-with-reason persists the pin; guard honors it while the
        current assignee still equals the pinned email (CTX-278 shape)."""
        item = self._item(assignee_email='aoi@shizuha.com', assignment_group='devops')
        views._persist_direct_assignment_pin(item, 'aoi@shizuha.com', 'Architecture sign-off (PLAT-1002)')
        item.refresh_from_db()
        self.assertEqual(item.metadata['direct_assignment']['assignee_email'], 'aoi@shizuha.com')
        self.assertEqual(item.metadata['direct_assignment']['reason'], 'Architecture sign-off (PLAT-1002)')
        self.assertEqual(item.metadata['direct_assignment']['pinned_team'], 'architecture')
        self.assertEqual(_decision_pinned_team(item), 'architecture')
        self.assertTrue(_is_decision_pinned(item))

    def test_persist_failure_increments_fail_open_observable_counter(self):
        """PLAT-3011: pin metadata write failures must stay fail-open, but no
        longer be silent; the statusz-exposed counter makes the failure observable."""
        views._reset_direct_assignment_pin_persist_failure_metric_for_tests()
        item = self._item(assignee_email='aoi@shizuha.com', assignment_group='devops')
        before = views._direct_assignment_pin_persist_failure_metrics()['count']

        with patch.object(item, 'save', side_effect=RuntimeError('metadata write down')):
            views._persist_direct_assignment_pin(item, 'aoi@shizuha.com', 'Architecture sign-off')

        metrics = views._direct_assignment_pin_persist_failure_metrics()
        self.assertEqual(metrics['count'], before + 1)
        self.assertIsNotNone(metrics['last_failure_ts'])
        resp = self.client.get(reverse('mechanism-liveness-statusz'))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['direct_assignment_pin_persist_failure_count'], before + 1)

    def test_failover_target_uses_pinned_decision_team_not_symptom_team(self):
        item = self._item(assignee_email='aoi@shizuha.com', assignment_group='devops')
        views._persist_direct_assignment_pin(item, 'aoi@shizuha.com', 'Architecture sign-off')
        item.refresh_from_db()

        with patch.object(WorkflowService, '_resolve_team_assignee', return_value=(71, 'nova@shizuha.com')) as resolve:
            uid, email = _resolve_decision_pin_failover_target(item, 'aoi@shizuha.com')

        self.assertEqual((uid, email), (71, 'nova@shizuha.com'))
        resolve.assert_called_once_with(
            'architecture',
            exclude_email='aoi@shizuha.com',
            enforce_cap=False,
            item=item,
        )

    def test_failover_rewrites_pin_to_replacement_decision_owner(self):
        """After a pinned-team failover, the metadata pin follows the new
        decision-team owner so the next rebalance candidate pass still excludes
        the task from the symptom-team pool."""
        item = self._item(assignee_email='aoi@shizuha.com', assignment_group='devops')
        views._persist_direct_assignment_pin(item, 'aoi@shizuha.com', 'Architecture sign-off')
        item.refresh_from_db()

        item.assignee_email = 'nova@shizuha.com'
        self.assertTrue(_repin_decision_assignment(item, 'nova@shizuha.com'))

        self.assertEqual(item.metadata['direct_assignment']['assignee_email'], 'nova@shizuha.com')
        self.assertEqual(item.metadata['direct_assignment']['pinned_team'], 'architecture')
        self.assertTrue(_is_decision_pinned(item))
        self.assertEqual(_decision_pinned_team(item), 'architecture')

        # This is the same exclusion predicate the rebalance movable set uses;
        # remaining pinned proves a later sweep will not put the item back in devops.
        would_be_movable_by_symptom_team = not _is_decision_pinned(item)
        self.assertFalse(would_be_movable_by_symptom_team)

    def test_clear_pin_helpers_remove_metadata_for_transition_or_reassign(self):
        item = self._item(assignee_email='aoi@shizuha.com', assignment_group='devops')
        views._persist_direct_assignment_pin(item, 'aoi@shizuha.com', 'Architecture sign-off')
        item.refresh_from_db()
        self.assertIn('direct_assignment', item.metadata)

        self.assertTrue(WorkflowService._clear_direct_assignment_pin(item))
        self.assertNotIn('direct_assignment', item.metadata)
        item.save(update_fields=['metadata'])
        item.refresh_from_db()
        self.assertNotIn('direct_assignment', item.metadata)

        views._persist_direct_assignment_pin(item, 'aoi@shizuha.com', 'Architecture sign-off')
        item.refresh_from_db()
        self.assertTrue(views._clear_direct_assignment_pin(item))
        item.refresh_from_db()
        self.assertNotIn('direct_assignment', item.metadata)

    def test_guard_auto_releases_when_assignee_changes(self):
        """A later real reassignment off the decision-owner releases the pin — so the
        sweep resumes normal routing (no stuck-forever, no separate clear path)."""
        item = self._item(assignee_email='aoi@shizuha.com', assignment_group='devops')
        views._persist_direct_assignment_pin(item, 'aoi@shizuha.com', 'reason')
        item.refresh_from_db()
        self.assertTrue(_is_decision_pinned(item))
        item.assignee_email = 'ichi@shizuha.com'  # rebalance-independent reassignment
        self.assertFalse(_is_decision_pinned(item))

    def test_guard_false_without_pin(self):
        """A normal team-routed task (no direct-assignment metadata) is never pinned."""
        item = self._item(assignee_email='aoi@shizuha.com', assignment_group='devops')
        self.assertFalse(_is_decision_pinned(item))

    def test_pin_is_case_insensitive_and_reason_truncated(self):
        """Email match is case-insensitive; reason is bounded to 500 chars."""
        item = self._item(assignee_email='Aoi@Shizuha.com', assignment_group='devops')
        views._persist_direct_assignment_pin(item, 'aoi@shizuha.com', 'x' * 600)
        item.refresh_from_db()
        self.assertEqual(len(item.metadata['direct_assignment']['reason']), 500)
        self.assertTrue(_is_decision_pinned(item))

    def test_malformed_metadata_is_not_pinned(self):
        """Defensive: non-dict / missing direct_assignment never raises or pins."""
        item = self._item(assignee_email='aoi@shizuha.com')
        item.metadata = {'direct_assignment': 'oops-not-a-dict'}
        self.assertFalse(_is_decision_pinned(item))
        item.metadata = {'direct_assignment': {'reason': 'no-email'}}
        self.assertFalse(_is_decision_pinned(item))
