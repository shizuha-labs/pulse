"""PLS-592: Focused-QA SoD exclusion + deployment surface coalescing."""

from __future__ import annotations

from django.test import SimpleTestCase, TestCase, override_settings
from unittest.mock import MagicMock, patch

from tasks.models import Item, ItemActivity, Project, TeamRoster
from tasks.services.focused_qa import (
    _conflicted_verifier_emails,
    _deployment_surface_key,
    _find_open_qa_task,
    ensure_focused_qa_for_item,
    ensure_focused_qa_task,
)


class ConflictedVerifierTests(SimpleTestCase):
    def test_includes_origin_assignee_and_actor(self):
        origin = MagicMock()
        origin.pk = None
        origin.assignee_email = 'nova@shizuha.com'
        origin.created_by_email = 'bot@shizuha.com'
        got = _conflicted_verifier_emails(origin, actor_email='Nova@shizuha.com')
        self.assertIn('nova@shizuha.com', got)
        self.assertIn('bot@shizuha.com', got)

    def test_scores_typed_sod_exclusion_from_origin_metadata(self):
        """PLS-1336: fixture parties recorded under the typed
        ``sod_excluded_verifier_emails`` key on the origin item are scored as
        conflicted — the router must never re-land a security-sensitive QA lap
        on them regardless of capacity."""
        origin = MagicMock()
        # pk=None keeps the typed-lifecycle participants branch (DB) out of
        # this SimpleTestCase; the typed-key scoring below is pk-independent.
        origin.pk = None
        origin.assignee_email = None
        origin.created_by_email = None
        origin.metadata = {
            'sod_excluded_verifier_emails': [
                'fixture-party@shizuha.com', '  ', None,
            ],
        }
        got = _conflicted_verifier_emails(origin)
        self.assertIn('fixture-party@shizuha.com', got)
        self.assertNotIn('', got)
        self.assertNotIn(None, got)

    def test_persists_prior_metadata(self):
        origin = MagicMock()
        origin.pk = None
        origin.assignee_email = None
        origin.created_by_email = None
        got = _conflicted_verifier_emails(
            origin,
            existing_metadata={'focused_qa_conflicted_emails': ['zen@shizuha.com']},
        )
        self.assertEqual(got, {'zen@shizuha.com'})


class FocusedQaResolverWiringTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(key='P592', org_id=1, name='PLS-592')
        TeamRoster.objects.update_or_create(
            organization_slug='shizuha',
            organization_id=1,
            team_name='engineering',
            defaults={'tiers': [['nova@shizuha.com']]},
        )
        TeamRoster.objects.update_or_create(
            organization_slug='shizuha',
            organization_id=1,
            team_name='qa',
            defaults={'tiers': [['safe-peer@shizuha.com']]},
        )
        self.origin = Item.objects.create(
            project=self.project,
            item_key=self.project.get_next_item_key(),
            title='Origin verification',
            status='done',
            assignment_group='engineering',
            assignee_id=11,
            assignee_email='nova@shizuha.com',
            created_by_id=12,
            created_by_email='saki@shizuha.com',
        )
        ItemActivity.objects.create(
            item=self.origin,
            action='status_changed',
            field_name='Status',
            old_value='in_review',
            new_value='done',
            user_id=13,
            user_email='reika@shizuha.com',
        )

    @patch('tasks.services.focused_qa._focused_qa_route', return_value=('qa', 'qa-roster'))
    @patch(
        'tasks.services.workflow_service.WorkflowService._resolve_team_assignee',
        return_value=(None, None),
    )
    def test_no_safe_peer_holds_group_queued_with_all_participants_excluded(
        self, resolver, _route,
    ):
        qa = ensure_focused_qa_task(
            origin_item=self.origin,
            risk_label='requires-live-qa',
            actor_email='zen@shizuha.com',
        )

        qa.refresh_from_db()
        self.assertIsNone(qa.assignee_id)
        self.assertIsNone(qa.assignee_email)
        self.assertTrue(qa.metadata['focused_qa_sod_hold'])
        kwargs = resolver.call_args.kwargs
        self.assertEqual(kwargs['exclude_email'], 'nova@shizuha.com')
        self.assertEqual(
            kwargs['hard_exclude_emails'],
            {
                'nova@shizuha.com',
                'saki@shizuha.com',
                'reika@shizuha.com',
                'zen@shizuha.com',
            },
        )

    @patch('tasks.services.focused_qa._focused_qa_route', return_value=('qa', 'qa-roster'))
    @patch(
        'tasks.services.workflow_service.WorkflowService._resolve_team_assignee',
        return_value=(21, 'safe-peer@shizuha.com'),
    )
    def test_safe_peer_is_assigned_after_resolver_receives_hard_exclusions(
        self, resolver, _route,
    ):
        qa = ensure_focused_qa_task(
            origin_item=self.origin,
            risk_label='requires-live-qa',
        )

        qa.refresh_from_db()
        self.assertEqual(qa.assignee_email, 'safe-peer@shizuha.com')
        self.assertNotIn('focused_qa_sod_hold', qa.metadata)
        self.assertIn(
            'reika@shizuha.com',
            resolver.call_args.kwargs['hard_exclude_emails'],
        )


class SurfaceKeyTests(SimpleTestCase):
    def test_surface_ignores_event_id(self):
        e1 = MagicMock(repo='hive', service='hive', environment='live',
                       deployment_target='prod', namespace='shizuha', id=1)
        e2 = MagicMock(repo='hive', service='hive', environment='live',
                       deployment_target='prod', namespace='shizuha', id=99)
        self.assertEqual(_deployment_surface_key(e1), _deployment_surface_key(e2))
        self.assertIn('hive', _deployment_surface_key(e1))


class NoPrGateTests(TestCase):
    @override_settings(PULSE_FOCUSED_QA_ENABLED='1')
    @patch('tasks.services.focused_qa._focused_qa_enabled', return_value=True)
    @patch('tasks.services.focused_qa._is_focused_qa_artifact', return_value=False)
    @patch('tasks.services.focused_qa.risk_labels_for_item', return_value={'requires-live-qa'})
    @patch('tasks.services.focused_qa._is_non_fix_disposition', return_value=False)
    @patch('tasks.services.focused_qa._is_static_scan_finding', return_value=False)
    @patch('tasks.services.focused_qa.ensure_focused_qa_task')
    def test_no_pr_label_update_does_not_mint(self, ensure_task, *_mocks):
        item = MagicMock()
        item.item_key = 'PLAT-4992'
        item.labels = {'qa-cadence': True, 'no-pr': True}
        item.tags = 'qa-cadence,no-pr'
        item.assignee_email = 'nova@shizuha.com'
        out = ensure_focused_qa_for_item(item, trigger='label_update')
        self.assertEqual(out, [])
        ensure_task.assert_not_called()


class FindOpenQaSurfaceTests(SimpleTestCase):
    @patch('tasks.services.focused_qa.Item')
    def test_prefers_surface_key_over_event_id(self, ItemModel):
        event = MagicMock(
            id=42, repo='hive', service='hive-frontend', environment='live',
            deployment_target='', namespace='',
        )
        existing = MagicMock()
        qs = MagicMock()
        ItemModel.objects.filter.return_value = qs
        qs.exclude.return_value = qs
        qs.filter.return_value = qs
        qs.order_by.return_value.first.return_value = existing

        found = _find_open_qa_task(None, event, 'requires-live-qa', 'live')
        self.assertIs(found, existing)
        # surface filter must be attempted
        filter_kwargs = [c.kwargs for c in qs.filter.call_args_list if c.kwargs]
        self.assertTrue(
            any('metadata__focused_qa_deployment_surface_key' in kw for kw in filter_kwargs),
            filter_kwargs,
        )
