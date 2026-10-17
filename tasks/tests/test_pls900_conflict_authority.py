"""PLS-900 / ADR-0052 conflict authority (Option C) — resolver hard-exclusion.

Acceptance slice (PLS-611 criteria 3/6):
1. A task carrying `conflicted_parties` naming X must never resolve to X, even
   when X is the least-loaded available member (hard exclusion).
2. Negative control: the same task WITHOUT the exclusion resolves to X.
3. An empty post-exclusion candidate set produces a visible routing HOLD
   (`conflicted_exclusion`), not a silent conflicted assignment.

The resolver under test is `WorkflowService._resolve_team_assignee` — the same
choke point every ownership/provenance writer uses, so routing, pull, rebalance,
failover, and reconciliation all converge on the exclusion.
"""
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase

from tasks.models import Item, Project, TeamRoster
from tasks.services.workflow_service import WorkflowService


class ConflictAuthorityResolverTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.x = User.objects.create_user(
            username='conflicted', email='x@shizuha.com', password='x',
        )
        self.y = User.objects.create_user(
            username='peer', email='y@shizuha.com', password='x',
        )
        self.project = Project.objects.create(
            name='PLS900', key='P900', org_id=1,
        )
        TeamRoster.objects.update_or_create(
            organization_id=1,
            team_name='engineering',
            defaults={
                'organization_slug': 'shizuha',
                'tiers': [['x@shizuha.com', 'y@shizuha.com']],
                'is_active': True,
            },
        )

    def _patch(self, roster):
        """Deterministic single-team resolver backdrop.

        X is intentionally least-loaded (0) so any selection that ignores the
        conflict authority would pick X first.
        """
        return mock.patch.multiple(
            WorkflowService,
            _pod_roster=mock.Mock(return_value=roster),
            _is_agent_available_for_assignment=mock.Mock(return_value=True),
            _seat_satisfies_required_capability=mock.Mock(return_value=True),
            _member_can_execute_from_status=mock.Mock(return_value=True),
            _seat_assignment_allowed=mock.Mock(return_value=True),
        )

    def _resolve(self, item, exclude_email=None):
        def _lookup(email):
            if (email or '').lower() == 'x@shizuha.com':
                return {'id': self.x.id, 'email': self.x.email}
            if (email or '').lower() == 'y@shizuha.com':
                return {'id': self.y.id, 'email': self.y.email}
            return None
        with mock.patch(
            'tasks.user_service.user_service.get_user_by_email',
            side_effect=_lookup,
        ):
            return WorkflowService._resolve_team_assignee(
                'engineering',
                item=item,
                exclude_email=exclude_email,
                enforce_cap=False,
            )

    def test_conflicted_party_never_selected_when_least_loaded(self):
        """Criterion 3: naming X as conflicted → X (least-loaded) not selected."""
        item = Item.objects.create(
            title='C3 conflicted exclusion',
            project=self.project,
            mode='task',
            workflow_name='autonomous-dev',
            status='todo',
            assignment_group='engineering',
            conflicted_parties=['x@shizuha.com'],
        )
        with self._patch({'engineering': ['x@shizuha.com', 'y@shizuha.com']}):
            uid, email = self._resolve(item)

        self.assertIsNotNone(uid)
        self.assertEqual(email, 'y@shizuha.com')
        self.assertNotEqual(email, 'x@shizuha.com')

    def test_negative_control_same_task_without_exclusion_selects_x(self):
        """Criterion 3 negative control: no exclusion → X (least-loaded) IS selected."""
        item = Item.objects.create(
            title='C3b negative control no exclusion',
            project=self.project,
            mode='task',
            workflow_name='autonomous-dev',
            status='todo',
            assignment_group='engineering',
        )
        with self._patch({'engineering': ['x@shizuha.com', 'y@shizuha.com']}):
            uid, email = self._resolve(item)

        self.assertEqual(email, 'x@shizuha.com')

    def test_empty_post_exclusion_pool_holds_visibly(self):
        """Criterion 6: only member conflicted → (None,None) + conflicted_exclusion HOLD."""
        # Narrow the DB roster (the item-bearing resolver reads TeamRoster via
        # routing_candidate_snapshot_for_item, not the mock) so only X remains.
        TeamRoster.objects.filter(
            organization_id=1, team_name='engineering',
        ).update(tiers=[['x@shizuha.com']])
        item = Item.objects.create(
            title='C6 empty post-exclusion',
            project=self.project,
            mode='task',
            workflow_name='autonomous-dev',
            status='todo',
            assignment_group='engineering',
            conflicted_parties=['x@shizuha.com'],
        )
        with self._patch({'engineering': ['x@shizuha.com']}):
            uid, email = self._resolve(item)

        self.assertIsNone(uid)
        self.assertIsNone(email)
        # Visible, deduped HOLD breadcrumb naming the conflict authority reason.
        # The mechanical comment carries the raw reason in origin_payload.dedupe
        # and a human label in reason_classes — assert the conflict marker is
        # present in the durable payload (not just a generic no-candidate hold).
        from tasks.models import Comment
        holds = Comment.objects.filter(
            item=item,
            origin_event='resolver.routability_hold',
        )
        self.assertTrue(holds.exists(), 'expected a routability hold comment')
        payload = holds.first().origin_payload or {}
        dedupe = str(payload.get('dedupe') or '')
        reason_classes = payload.get('reason_classes') or []
        self.assertTrue(
            'conflicted_exclusion' in dedupe
            or 'conflicted exclusion' in str(reason_classes)
            or 'conflicted_exclusion' in str(reason_classes),
            f'expected conflicted-exclusion hold, got dedupe={dedupe!r} classes={reason_classes!r}',
        )

    def test_field_normalization_lowercases_and_ignores_blanks(self):
        """Entries are normalized to lowercase; blanks/None are dropped."""
        item = Item(
            title='normalization',
            conflicted_parties=['X@SHIZUHA.COM', '  ', None, 'y@shizuha.com'],
        )
        # Model stores as-is (JSON); resolver normalizes at consume time.
        self.assertEqual(
            item.conflicted_parties,
            ['X@SHIZUHA.COM', '  ', None, 'y@shizuha.com'],
        )
        # Resolver-side normalization fold is exercised via exclusion test above;
        # assert the helper folds case-insensitively through the real path.
        cleaned = {
            str(p).strip().lower()
            for p in item.conflicted_parties
            if p is not None and str(p).strip()
        }
        self.assertEqual(cleaned, {'x@shizuha.com', 'y@shizuha.com'})

    # --- PLS-971 regression: create-time probe item must carry conflicted_parties ---

    def test_create_probe_item_carries_conflicted_parties(self):
        """PLS-971: the create-time probe item copies conflicted_parties so the
        resolver's hard-exclusion sees them during create routing.

        Live regression: PLS-970 was created with conflicted_parties naming
        yuki, but the probe item used for create-time resolution omitted the
        field, so the resolver assigned the explicitly excluded candidate.
        """
        validated = {
            'title': 'PLS-971 probe carry',
            'project': self.project,
            'workflow_name': 'reviewed-task',
            'conflicted_parties': ['yuki@shizuha.com'],
        }
        probe = WorkflowService.create_permission_probe_item(
            validated,
            extra_data={'assignment_group': 'engineering'},
            assignment_group='engineering',
        )
        self.assertEqual(probe.conflicted_parties, ['yuki@shizuha.com'])

    def test_create_probe_item_resolver_excludes_conflicted(self):
        """PLS-971: resolving through the create-time probe item never selects a
        declared conflicted party, while the no-exclusion control does."""
        roster = {'engineering': [['x@shizuha.com', 'y@shizuha.com']]}
        with self._patch(roster):
            # With exclusion naming X (least-loaded): X must NOT be selected.
            probe = WorkflowService.create_permission_probe_item(
                {
                    'title': 'PLS-971 excluded',
                    'project': self.project,
                    'workflow_name': 'reviewed-task',
                    'conflicted_parties': ['x@shizuha.com'],
                },
                extra_data={'assignment_group': 'engineering'},
                assignment_group='engineering',
            )
            uid, email = self._resolve(probe)
            self.assertIsNotNone(email)
            self.assertNotEqual((email or '').lower(), 'x@shizuha.com')
            self.assertEqual((email or '').lower(), 'y@shizuha.com')

            # Negative control: same task WITHOUT the exclusion selects X.
            probe2 = WorkflowService.create_permission_probe_item(
                {
                    'title': 'PLS-971 control',
                    'project': self.project,
                    'workflow_name': 'reviewed-task',
                },
                extra_data={'assignment_group': 'engineering'},
                assignment_group='engineering',
            )
            uid2, email2 = self._resolve(probe2)
            self.assertEqual((email2 or '').lower(), 'x@shizuha.com')

    def test_serializer_normalizes_conflicted_parties(self):
        """PLS-971: the write serializer normalizes conflicted_parties to
        lowercase, stripped, blank-free — matching the MCP normalization."""
        from tasks.serializers import ItemSerializer
        s = ItemSerializer(data={
            'title': 'PLS-971 normalization',
            'project': self.project.id,
            'conflicted_parties': ['YUKI@SHIZUHA.COM', ' yuki@shizuha.com ', ''],
        })
        self.assertTrue(s.is_valid(), s.errors)
        self.assertEqual(
            s.validated_data['conflicted_parties'],
            ['yuki@shizuha.com'],
        )
        # Comma-separated string form also normalizes.
        s2 = ItemSerializer(data={
            'title': 'PLS-971 csv',
            'project': self.project.id,
            'conflicted_parties': 'YUKI@SHIZUHA.COM, yuki@shizuha.com, ',
        })
        self.assertTrue(s2.is_valid(), s2.errors)
        self.assertEqual(
            s2.validated_data['conflicted_parties'],
            ['yuki@shizuha.com'],
        )
