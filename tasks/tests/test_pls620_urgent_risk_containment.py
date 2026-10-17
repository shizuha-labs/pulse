"""PLS-620 production-order urgent-risk containment regressions."""

from unittest import mock

from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import TestCase
from rest_framework.test import APIClient

from tasks.models import (
    Comment,
    ContainmentDecision,
    ContainmentDecisionEvent,
    Item,
    ItemStateEventOutbox,
    Project,
    Status,
    TeamRoster,
    Workflow,
    WorkflowStatus,
    WorkflowTransition,
)
from tasks.services.urgent_risk_containment import (
    assert_transition_allowed,
    reconcile_item,
    record_decision,
)
from tasks.services.workflow_service import WorkflowService, WorkflowValidationError


ENGINEER = 'kai@shizuha.com'
LEAD = 'jun@shizuha.com'


class _FakeUserService:
    users = {
        ENGINEER: {'id': 41, 'email': ENGINEER, 'username': 'kai'},
        LEAD: {'id': 42, 'email': LEAD, 'username': 'jun'},
    }

    def get_user_by_email(self, email):
        return self.users.get(str(email or '').strip().lower())

    def get_user_by_username(self, username):
        username = str(username or '').strip().lower()
        return next(
            (
                row for row in self.users.values()
                if row['username'].lower() == username
            ),
            None,
        )

    def get_user_by_id(self, user_id):
        for row in self.users.values():
            if row['id'] == int(user_id):
                return row
        return None

    def resolve_user_info(self, user_id=None, email=None):
        return self.get_user_by_id(user_id) if user_id else self.get_user_by_email(email)


def _apply_owner(item, uid, email, update_fields, **_kwargs):
    item.assignee_id = uid
    item.assignee_email = email
    for field in ('assignee_id', 'assignee_email'):
        if field not in update_fields:
            update_fields.append(field)
    return uid, email


class PLS620UrgentRiskContainmentTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='pls620-owner',
            email=ENGINEER,
            password='x',
            is_staff=True,
        )
        self.project = Project.objects.create(
            name='Pulse 620', key='P620', org_id=1, access_level='open',
        )
        TeamRoster.objects.update_or_create(
            team_name='engineering',
            organization_id=1,
            defaults={
                'organization_slug': 'shizuha',
                'tiers': [[ENGINEER]],
                'is_active': True,
            },
        )
        self.open_status, _ = Status.objects.get_or_create(
            slug='open', defaults={'name': 'Open', 'category': 'todo', 'position': 1},
        )
        self.progress_status, _ = Status.objects.get_or_create(
            slug='in_progress',
            defaults={'name': 'In Progress', 'category': 'in_progress', 'position': 2},
        )
        self.workflow = Workflow.objects.create(
            name='PLS620 Workflow',
            slug='pls620-workflow',
            initial_status=self.open_status,
        )
        WorkflowStatus.objects.create(
            workflow=self.workflow, status=self.open_status, position=1,
        )
        WorkflowStatus.objects.create(
            workflow=self.workflow, status=self.progress_status, position=2,
        )
        self.start = WorkflowTransition.objects.create(
            workflow=self.workflow,
            name='Start Progress',
            from_status='open',
            to_status='in_progress',
            validators=[],
            post_functions=[],
            screen_fields=[],
            allowed_roles=[],
        )

    def _create(
        self,
        *,
        key,
        labels=None,
        priority='urgent',
        tags='',
        assignee_id=None,
        assignee_email='',
    ):
        return Item.objects.create(
            mode='task',
            item_type='task.bug',
            title=f'PLS-620 fixture {key}',
            project=self.project,
            item_key=key,
            workflow_name=self.workflow.name,
            status='open',
            workflow_status='Open',
            assignment_group='engineering',
            assignee_id=assignee_id,
            assignee_email=assignee_email,
            priority=priority,
            labels=labels or {},
            tags=tags,
            created_by_id=self.user.id,
            created_by_email=self.user.email,
        )

    def _owner_patches(self, *, resolver=(41, ENGINEER)):
        return (
            mock.patch(
                'tasks.services.workflow_service.WorkflowService._resolve_team_assignee',
                return_value=resolver,
            ),
            mock.patch(
                'tasks.services.workflow_service.WorkflowService._apply_resolved_assignee',
                side_effect=_apply_owner,
            ),
            mock.patch(
                'tasks.services.workflow_service.WorkflowService._member_can_execute_from_status',
                return_value=True,
            ),
            mock.patch(
                'tasks.services.workflow_service.WorkflowService._is_agent_available_for_assignment',
                return_value=True,
            ),
            mock.patch('tasks.user_service.user_service', _FakeUserService()),
        )

    def test_priority_raise_replaces_over_capacity_retained_owner(self):
        """Production order: high+risk on a retained full seat -> urgent.

        Operator 2026-09-27: urgent assignment ignores the WIP cap, so the
        retained full seat stays. The containment decision is still opened.
        """

        TeamRoster.objects.filter(
            team_name='engineering', organization_id=1,
        ).update(
            tiers=[[ENGINEER, LEAD]],
            wip_enforced=True,
            ready_wip_cap=1,
            active_wip_cap=1,
        )
        WorkflowService._invalidate_team_roster_caches()
        self._create(
            key='P620-CAPACITY',
            priority='high',
            labels={'backend': 'true'},
            assignee_id=41,
            assignee_email=ENGINEER,
        )
        item = self._create(
            key='P620-RETAINED',
            priority='high',
            labels={'data-loss': 'true'},
            assignee_id=41,
            assignee_email=ENGINEER,
        )

        with mock.patch(
            'tasks.services.workflow_service.WorkflowService._is_agent_available_for_assignment',
            return_value=True,
        ), mock.patch(
            'tasks.services.workflow_service.WorkflowService._member_can_execute_from_status',
            return_value=True,
        ), mock.patch('tasks.user_service.user_service', _FakeUserService()):
            item.priority = 'urgent'
            item.save(update_fields=['priority'])

        item.refresh_from_db()
        decision = ContainmentDecision.objects.get(item=item)
        self.assertEqual((item.assignee_id, item.assignee_email), (41, ENGINEER))
        self.assertEqual(decision.owner_email, ENGINEER)
        self.assertEqual(decision.state, ContainmentDecision.STATE_PENDING)
        self.assertEqual(item.assignment_group, 'engineering')
        self.assertEqual(item.state_generation, 1)
        self.assertEqual(
            list(
                ItemStateEventOutbox.objects.filter(item=item)
                .order_by('state_generation')
                .values_list('state_generation', 'changed_kinds')
            ),
            [(1, ['priority'])],
        )

    def test_priority_raise_clears_retained_owner_when_exact_lane_is_full(self):
        """Production order: retained full lane -> urgent.

        Both seats are over the ordinary cap. Urgent assignment keeps the
        current owner instead of clearing them into a group queue.
        """

        TeamRoster.objects.filter(
            team_name='engineering', organization_id=1,
        ).update(
            tiers=[[ENGINEER, LEAD]],
            wip_enforced=True,
            ready_wip_cap=1,
            active_wip_cap=1,
        )
        WorkflowService._invalidate_team_roster_caches()
        self._create(
            key='P620-CAPACITY-ENGINEER',
            priority='high',
            labels={'backend': 'true'},
            assignee_id=41,
            assignee_email=ENGINEER,
        )
        self._create(
            key='P620-CAPACITY-LEAD',
            priority='high',
            labels={'backend': 'true'},
            assignee_id=42,
            assignee_email=LEAD,
        )
        item = self._create(
            key='P620-RETAINED-NO-CANDIDATE',
            priority='high',
            labels={'data-loss': 'true'},
            assignee_id=41,
            assignee_email=ENGINEER,
        )

        with mock.patch(
            'tasks.services.workflow_service.WorkflowService._is_agent_available_for_assignment',
            return_value=True,
        ), mock.patch(
            'tasks.services.workflow_service.WorkflowService._member_can_execute_from_status',
            return_value=True,
        ), mock.patch(
            'tasks.services.workflow_service.WorkflowService._resolve_team_lead_email',
            return_value=LEAD,
        ), mock.patch('tasks.user_service.user_service', _FakeUserService()):
            item.priority = 'urgent'
            item.save(update_fields=['priority'])
            reconcile_item(item.pk, trigger='rebalance_pass')

        item.refresh_from_db()
        decision = ContainmentDecision.objects.get(item=item)
        self.assertEqual((item.assignee_id, item.assignee_email), (41, ENGINEER))
        self.assertEqual(item.assignment_group, 'engineering')
        self.assertEqual(decision.state, ContainmentDecision.STATE_PENDING)
        self.assertEqual(decision.owner_email, ENGINEER)
        self.assertEqual(decision.events.count(), 1)
        self.assertFalse(
            Comment.objects.filter(
                item=item,
                content__contains='[PLS-620 exact-lane hold',
            ).exists()
        )

    def test_production_order_create_triage_correction_rebalance_is_stable(self):
        """Real caller order: create → start attempt → severity correction →
        evidence decision → start → two reconciliation/rebalance passes.
        """

        p1, p2, p3, p4, p5 = self._owner_patches()
        with p1 as resolve, p2, p3, p4, p5:
            item = self._create(key='P620-1', labels={'data-loss': 'true'})

            item.refresh_from_db()
            self.assertEqual((item.assignee_id, item.assignee_email), (41, ENGINEER))
            decision = ContainmentDecision.objects.get(item=item)
            self.assertEqual(decision.state, ContainmentDecision.STATE_PENDING)
            self.assertTrue(decision.requirement_active)
            self.assertEqual(decision.owner_email, ENGINEER)
            self.assertEqual(decision.risk_markers, ['data-loss'])
            self.assertEqual(decision.events.count(), 1)
            self.assertTrue(resolve.call_args.kwargs['enforce_cap'])

            with self.assertRaises(WorkflowValidationError) as blocked:
                WorkflowService.execute_transition(
                    item,
                    'in_progress',
                    user=self.user,
                    transition_id=str(self.start.id),
                )
            self.assertEqual(blocked.exception.code, 'URGENT_RISK_CONTAINMENT_PENDING')
            item.refresh_from_db()
            self.assertEqual(item.status, 'open')

            # DRV-75 negative-control correction: the severe premise is removed,
            # but the pending gate remains until the evidence-backed decision lands.
            item.priority = 'high'
            item.labels = {'trash-ui': 'true'}
            item.save(update_fields=['priority', 'labels'])
            decision.refresh_from_db()
            self.assertEqual(decision.state, ContainmentDecision.STATE_PENDING)
            self.assertFalse(decision.requirement_active)

            evidence = {
                'delete_status': 204,
                'parent_get_status': 200,
                'child_get_status': 200,
                'trash_api_rows': ['parent', 'child'],
                'retention_days': 30,
            }
            decision = record_decision(
                item.pk,
                state=ContainmentDecision.STATE_EXPLICITLY_NOT_REQUIRED,
                reason='Recursive soft-delete and 30-day Trash retention are working.',
                evidence=evidence,
                actor_id=self.user.id,
                actor_email=self.user.email,
                trigger='drv75_negative_control',
            )
            self.assertEqual(decision.state, ContainmentDecision.STATE_EXPLICITLY_NOT_REQUIRED)

            with mock.patch.object(WorkflowService, '_reserve_wip_slot', return_value=True):
                WorkflowService.execute_transition(
                    item,
                    'in_progress',
                    user=self.user,
                    transition_id=str(self.start.id),
                )
            item.refresh_from_db()
            self.assertEqual(item.status, 'in_progress')

            version = decision.version
            event_count = ContainmentDecisionEvent.objects.filter(decision=decision).count()
            reconcile_item(item.pk, trigger='rebalance_pass')
            reconcile_item(item.pk, trigger='rebalance_pass')
            decision.refresh_from_db()
            self.assertEqual(decision.version, version)
            self.assertEqual(decision.evidence, evidence)
            self.assertEqual(
                ContainmentDecisionEvent.objects.filter(decision=decision).count(),
                event_count,
            )

    def test_urgent_without_explicit_risk_has_no_spurious_gate(self):
        item = self._create(key='P620-2', labels={'backend': 'true'}, tags='urgent')

        self.assertFalse(ContainmentDecision.objects.filter(item=item).exists())
        assert_transition_allowed(item, 'in_progress')

    def test_no_candidate_holds_exact_lane_and_dedupes(self):
        p1, p2, _p3, _p4, p5 = self._owner_patches(resolver=(None, None))
        with p1, p2, p5, \
                mock.patch.object(WorkflowService, '_resolve_team_lead_email', return_value=LEAD):
            item = self._create(key='P620-3', labels={'user-harm': 'true'})
            item.save(update_fields=['priority'])

        item.refresh_from_db()
        decision = ContainmentDecision.objects.get(item=item)
        self.assertIsNone(item.assignee_id)
        self.assertEqual(item.assignment_group, 'engineering')
        self.assertEqual(decision.state, ContainmentDecision.STATE_PENDING)
        self.assertEqual(decision.owner_email, LEAD)
        self.assertEqual(decision.events.count(), 1)
        self.assertEqual(
            Comment.objects.filter(item=item, content__contains='[PLS-620 exact-lane hold').count(),
            1,
        )

    def test_api_create_unknown_live_source_holds_exact_lane(self):
        """Production order: routed API create -> containment -> replay.

        PLS-639 reproduced when the provider-health map expired between a
        failover and the next urgent-risk create.  The roster seat had no
        durable availability row, so the old PLAT-319 fallback treated missing
        live authority as AVAILABLE.  That provisional owner then became both
        task and containment owner without a PLS-278 HOLD.

        Missing live authority is not affirmative liveness for a
        ``new_assignment`` (PLS-277 §5.3).  The real top-level create path must
        therefore commit the exact lane with no concrete owner, one lead-owned
        pending decision, and one deduplicated HOLD.  Two later reconciliations
        model the event/recovery replay boundary and must not resurrect the
        unknown seat or duplicate the projection.
        """

        client = APIClient()
        client.force_authenticate(user=self.user)
        body = {
            'mode': 'task',
            'title': 'PLS-639 unknown-live API create',
            'project': self.project.id,
            'workflow_name': self.workflow.slug,
            'assignment_group': 'engineering',
            'priority': 'urgent',
            'labels': {'data-loss': 'true'},
        }
        resolve = WorkflowService._resolve_team_assignee
        resolution_trace = []

        def traced_resolve(*args, **kwargs):
            result = resolve(*args, **kwargs)
            resolution_trace.append((
                bool(kwargs.get('require_live_evidence')),
                result,
            ))
            return result

        with mock.patch.object(
            WorkflowService, '_health_availability_map', return_value={},
        ), mock.patch.object(
            WorkflowService, '_disabled_agent_emails', return_value=set(),
        ), mock.patch.object(
            WorkflowService, '_configured_agent_availability', return_value=None,
        ), mock.patch.object(
            WorkflowService, '_is_human_account', return_value=False,
        ), mock.patch.object(
            WorkflowService, '_member_can_execute_from_status', return_value=True,
        ), mock.patch.object(
            WorkflowService, '_task_required_capabilities', return_value=set(),
        ), mock.patch.object(
            WorkflowService, '_seat_satisfies_required_capability', return_value=True,
        ), mock.patch.object(
            WorkflowService, '_resolve_team_lead_email', return_value=LEAD,
        ), mock.patch.object(
            WorkflowService, '_resolve_team_assignee', side_effect=traced_resolve,
        ), mock.patch('tasks.user_service.user_service', _FakeUserService()):
            response = client.post('/api/items/', body, format='json')
            self.assertEqual(response.status_code, 201, response.data)
            item = Item.objects.get(pk=response.data['id'])
            reconcile_item(item.pk, trigger='recovery_replay_1')
            reconcile_item(item.pk, trigger='recovery_replay_2')

        item.refresh_from_db()
        decision = ContainmentDecision.objects.get(item=item)
        self.assertIn((False, (41, ENGINEER)), resolution_trace)
        self.assertIn((True, (None, None)), resolution_trace)
        self.assertIsNone(item.assignee_id)
        self.assertFalse(item.assignee_email)
        self.assertEqual(item.assignment_group, 'engineering')
        self.assertEqual(decision.state, ContainmentDecision.STATE_PENDING)
        self.assertEqual(decision.owner_email, LEAD)
        self.assertEqual(decision.events.count(), 1)
        self.assertEqual(
            Comment.objects.filter(
                item=item,
                content__contains='[PLS-620 exact-lane hold',
            ).count(),
            1,
        )

    def test_branch_return_refresh_cannot_resurrect_unknown_owner(self):
        """Production order: branch hold -> urgent containment -> replay.

        A review-return hold pins the active Origin branch owner, but an empty
        live-authority map makes that owner unknown.  The initial ordinary
        assignment remains fail-open for compatibility; urgent containment must
        then clear it, preserve the exact lane, and carry its strict liveness
        predicate through the branch-owner refresh.  Two recovery passes cross
        the real re-arm boundary and must not resurrect the nominal owner or
        duplicate the pending decision/HOLD.
        """

        branch_metadata = {
            'pull_requests': [{
                'url': (
                    'https://origin.shizuha.com/shizuha-labs/'
                    'shizuha-tasks/pulls/110'
                ),
                'status': 'open',
                'head_sha': 'a' * 40,
            }],
            'branch_return_routing': {
                'state': 'group_queued',
                'reason_code': 'origin_author_unavailable',
                'team': 'engineering',
                'owner_id': 41,
                'owner_email': ENGINEER,
            },
        }

        with mock.patch.object(
            WorkflowService, '_health_availability_map', return_value={},
        ), mock.patch.object(
            WorkflowService, '_disabled_agent_emails', return_value=set(),
        ), mock.patch.object(
            WorkflowService, '_configured_agent_availability', return_value=None,
        ), mock.patch.object(
            WorkflowService, '_is_human_account', return_value=False,
        ), mock.patch.object(
            WorkflowService, '_member_can_execute_from_status', return_value=True,
        ), mock.patch.object(
            WorkflowService, '_task_required_capabilities', return_value=set(),
        ), mock.patch.object(
            WorkflowService, '_seat_satisfies_required_capability', return_value=True,
        ), mock.patch.object(
            WorkflowService, '_resolve_team_lead_email', return_value=LEAD,
        ), mock.patch.object(
            WorkflowService, '_origin_pr_author_login', return_value=('kai', None),
        ), mock.patch(
            'tasks.services.admin_service.admin_service.get_organization',
            return_value={'id': 1, 'slug': 'shizuha'},
        ), mock.patch(
            'tasks.services.admin_service.admin_service.is_org_member',
            return_value=True,
        ), mock.patch.object(
            WorkflowService, '_notify_routability_hold', return_value=True,
        ) as notify, mock.patch('tasks.user_service.user_service', _FakeUserService()):
            with self.captureOnCommitCallbacks(execute=True):
                item = Item.objects.create(
                    mode='task',
                    item_type='task.bug',
                    title='PLS-639 branch-return unknown-live fixture',
                    project=self.project,
                    item_key='P620-BRANCH-RETURN-UNKNOWN',
                    workflow_name=self.workflow.name,
                    status='open',
                    workflow_status='Open',
                    assignment_group='engineering',
                    assignee_id=None,
                    assignee_email='',
                    priority='urgent',
                    labels={'data-loss': 'true'},
                    created_by_id=self.user.id,
                    created_by_email=self.user.email,
                    metadata=branch_metadata,
                )
                reconcile_item(item.pk, trigger='branch_return_recovery_1')
                reconcile_item(item.pk, trigger='branch_return_recovery_2')

        item.refresh_from_db()
        decision = ContainmentDecision.objects.get(item=item)
        self.assertIsNone(item.assignee_id)
        self.assertFalse(item.assignee_email)
        self.assertEqual(item.assignment_group, 'engineering')
        self.assertEqual(decision.state, ContainmentDecision.STATE_PENDING)
        self.assertEqual(decision.owner_email, LEAD)
        self.assertEqual(decision.events.count(), 1)
        self.assertEqual(
            Comment.objects.filter(
                item=item,
                content__contains='[PLS-620 exact-lane hold',
            ).count(),
            1,
        )
        hold = Comment.objects.get(
            item=item,
            origin_event='resolver.routability_hold',
        )
        self.assertEqual(
            hold.origin_payload['reason_classes'],
            ['seat not live for new assignment'],
        )
        notify.assert_called_once_with(
            item,
            'engineering',
            ('seat_not_live',),
            source='branch-return-resolver',
            attempted_email=ENGINEER,
        )

    def test_transaction_rollback_leaves_no_decision_comment_or_owner(self):
        p1, p2, p3, p4, p5 = self._owner_patches()
        with p1, p2, p3, p4, p5:
            with self.assertRaises(RuntimeError):
                with transaction.atomic():
                    item = self._create(key='P620-4', labels={'data-loss': 'true'})
                    self.assertTrue(ContainmentDecision.objects.filter(item=item).exists())
                    raise RuntimeError('rollback fixture')

        self.assertFalse(Item.objects.filter(item_key='P620-4').exists())
        self.assertFalse(ContainmentDecision.objects.exists())
        self.assertFalse(Comment.objects.filter(content__contains='[PLS-620').exists())

    def test_full_lane_rollback_discards_routability_notification(self):
        """Production order: retained full owner -> urgent -> outer rollback.

        The exact-lane HOLD and containment projection are transactional, so
        their external fail-loud notification must not escape a rollback.
        """

        TeamRoster.objects.filter(
            team_name='engineering', organization_id=1,
        ).update(
            tiers=[[ENGINEER, LEAD]],
            wip_enforced=True,
            ready_wip_cap=1,
            active_wip_cap=1,
        )
        WorkflowService._invalidate_team_roster_caches()
        self._create(
            key='P620-ROLLBACK-CAPACITY-ENGINEER',
            priority='high',
            labels={'backend': 'true'},
            assignee_id=41,
            assignee_email=ENGINEER,
        )
        self._create(
            key='P620-ROLLBACK-CAPACITY-LEAD',
            priority='high',
            labels={'backend': 'true'},
            assignee_id=42,
            assignee_email=LEAD,
        )
        item = self._create(
            key='P620-ROLLBACK-NO-CANDIDATE',
            priority='high',
            labels={'data-loss': 'true'},
            assignee_id=41,
            assignee_email=ENGINEER,
        )

        with mock.patch(
            'tasks.services.workflow_service.WorkflowService._is_agent_available_for_assignment',
            return_value=True,
        ), mock.patch(
            'tasks.services.workflow_service.WorkflowService._member_can_execute_from_status',
            return_value=True,
        ), mock.patch(
            'tasks.services.workflow_service.WorkflowService._resolve_team_lead_email',
            return_value=LEAD,
        ), mock.patch(
            'tasks.services.workflow_service.WorkflowService._notify_routability_hold',
            return_value=True,
        ) as notify, mock.patch('tasks.user_service.user_service', _FakeUserService()):
            with self.assertRaisesRegex(RuntimeError, 'rollback full lane'):
                with transaction.atomic():
                    item.priority = 'urgent'
                    item.save(update_fields=['priority'])
                    raise RuntimeError('rollback full lane')

        item.refresh_from_db()
        self.assertEqual(item.priority, 'high')
        self.assertEqual((item.assignee_id, item.assignee_email), (41, ENGINEER))
        self.assertFalse(ContainmentDecision.objects.filter(item=item).exists())
        self.assertFalse(
            Comment.objects.filter(
                item=item,
                content__contains='PLS-278 routability hold',
            ).exists()
        )
        notify.assert_not_called()

    def test_full_lane_commit_delivers_one_routability_notification(self):
        """Production order: retained full owner -> urgent stays seated.

        The cap is not a reason to clear an urgent owner or to emit a
        routability hold. The containment decision is still recorded.
        """

        TeamRoster.objects.filter(
            team_name='engineering', organization_id=1,
        ).update(
            tiers=[[ENGINEER, LEAD]],
            wip_enforced=True,
            ready_wip_cap=1,
            active_wip_cap=1,
        )
        WorkflowService._invalidate_team_roster_caches()
        self._create(
            key='P620-COMMIT-CAPACITY-ENGINEER',
            priority='high',
            labels={'backend': 'true'},
            assignee_id=41,
            assignee_email=ENGINEER,
        )
        self._create(
            key='P620-COMMIT-CAPACITY-LEAD',
            priority='high',
            labels={'backend': 'true'},
            assignee_id=42,
            assignee_email=LEAD,
        )
        item = self._create(
            key='P620-COMMIT-NO-CANDIDATE',
            priority='high',
            labels={'data-loss': 'true'},
            assignee_id=41,
            assignee_email=ENGINEER,
        )

        with mock.patch(
            'tasks.services.workflow_service.WorkflowService._is_agent_available_for_assignment',
            return_value=True,
        ), mock.patch(
            'tasks.services.workflow_service.WorkflowService._member_can_execute_from_status',
            return_value=True,
        ), mock.patch(
            'tasks.services.workflow_service.WorkflowService._resolve_team_lead_email',
            return_value=LEAD,
        ), mock.patch(
            'tasks.services.workflow_service.WorkflowService._notify_routability_hold',
            return_value=True,
        ) as notify, mock.patch('tasks.user_service.user_service', _FakeUserService()):
            with self.captureOnCommitCallbacks(execute=True):
                with transaction.atomic():
                    item.priority = 'urgent'
                    item.save(update_fields=['priority'])

        notify.assert_not_called()
        item.refresh_from_db()
        self.assertEqual(item.priority, 'urgent')
        self.assertEqual((item.assignee_id, item.assignee_email), (41, ENGINEER))
        self.assertTrue(ContainmentDecision.objects.filter(item=item).exists())
        self.assertFalse(
            Comment.objects.filter(
                item=item,
                content__contains='PLS-278 routability hold',
            ).exists()
        )

    def test_new_risk_marker_reopens_resolved_decision_once(self):
        p1, p2, p3, p4, p5 = self._owner_patches()
        with p1, p2, p3, p4, p5:
            item = self._create(key='P620-5', labels={'data-loss': 'true'})
            decision = record_decision(
                item.pk,
                state=ContainmentDecision.STATE_APPLIED,
                reason='Write path disabled behind the containment gate.',
                evidence={'change_id': 'containment-1'},
                actor_id=self.user.id,
                actor_email=self.user.email,
            )
            item.labels = {'data-loss': 'true', 'user-harm': 'true'}
            item.save(update_fields=['labels'])
            item.save(update_fields=['labels'])

        decision.refresh_from_db()
        self.assertEqual(decision.state, ContainmentDecision.STATE_PENDING)
        self.assertEqual(decision.risk_markers, ['data-loss', 'user-harm'])
        self.assertEqual(decision.events.count(), 3)
        self.assertEqual(decision.evidence['supersedes_version'], 2)

    def test_same_risk_marker_reopens_after_declassify_reclassify(self):
        """Production order: resolve -> required false -> same marker true.

        The current projection must clear inactive classifications while the
        event ledger retains them, so recurrence of the same marker starts a
        fresh pending epoch and blocks ordinary progression.
        """

        p1, p2, p3, p4, p5 = self._owner_patches()
        with p1, p2, p3, p4, p5:
            item = self._create(key='P620-6', labels={'data-loss': 'true'})
            decision = record_decision(
                item.pk,
                state=ContainmentDecision.STATE_EXPLICITLY_NOT_REQUIRED,
                reason='Soft-delete and retention evidence disproved data loss.',
                evidence={'delete_status': 204, 'retention_days': 30},
                actor_id=self.user.id,
                actor_email=self.user.email,
            )

            # Declassification closes the active classification epoch without
            # deleting the earlier marker from the append-only event history.
            item.priority = 'high'
            item.labels = {'trash-ui': 'true'}
            item.save(update_fields=['priority', 'labels'])
            decision.refresh_from_db()
            self.assertEqual(
                decision.state,
                ContainmentDecision.STATE_EXPLICITLY_NOT_REQUIRED,
            )
            self.assertEqual(decision.risk_markers, [])

            declassified_version = decision.version
            self.assertEqual(
                decision.events.get(version=declassified_version - 1).risk_markers,
                ['data-loss'],
            )

            # The same marker recurring is a new required false -> true edge,
            # not a continuation of the already-resolved epoch.
            item.priority = 'urgent'
            item.labels = {'data-loss': 'true'}
            item.save(update_fields=['priority', 'labels'])
            decision.refresh_from_db()
            self.assertEqual(decision.state, ContainmentDecision.STATE_PENDING)
            self.assertEqual(decision.risk_markers, ['data-loss'])
            self.assertTrue(decision.requirement_active)
            self.assertEqual(
                decision.evidence,
                {'supersedes_version': declassified_version},
            )

            with self.assertRaises(WorkflowValidationError) as blocked:
                WorkflowService.execute_transition(
                    item,
                    'in_progress',
                    user=self.user,
                    transition_id=str(self.start.id),
                )
            self.assertEqual(
                blocked.exception.code,
                'URGENT_RISK_CONTAINMENT_PENDING',
            )

            version = decision.version
            event_count = decision.events.count()
            item.save(update_fields=['labels'])
            decision.refresh_from_db()
            self.assertEqual(decision.version, version)
            self.assertEqual(decision.events.count(), event_count)

    def test_priority_only_reactivation_reopens_same_marker_epoch(self):
        """Production order: resolve -> high+marker -> urgent+same marker twice.

        The false -> true edge belongs to the containment requirement, so a
        priority-only recurrence cannot inherit the previous resolved decision.
        """

        p1, p2, p3, p4, p5 = self._owner_patches()
        with p1, p2, p3, p4, p5:
            item = self._create(key='P620-7', labels={'data-loss': 'true'})
            decision = record_decision(
                item.pk,
                state=ContainmentDecision.STATE_APPLIED,
                reason='The destructive write path was disabled.',
                evidence={'change_id': 'containment-priority-epoch'},
                actor_id=self.user.id,
                actor_email=self.user.email,
            )

            item.priority = 'high'
            item.save(update_fields=['priority'])
            decision.refresh_from_db()
            self.assertEqual(decision.state, ContainmentDecision.STATE_APPLIED)
            self.assertFalse(decision.requirement_active)
            self.assertEqual(decision.risk_markers, ['data-loss'])
            inactive_version = decision.version

            item.priority = 'urgent'
            item.save(update_fields=['priority'])
            decision.refresh_from_db()
            self.assertEqual(decision.state, ContainmentDecision.STATE_PENDING)
            self.assertTrue(decision.requirement_active)
            self.assertEqual(decision.risk_markers, ['data-loss'])
            self.assertEqual(
                decision.evidence,
                {'supersedes_version': inactive_version},
            )

            with self.assertRaises(WorkflowValidationError) as blocked:
                WorkflowService.execute_transition(
                    item,
                    'in_progress',
                    user=self.user,
                    transition_id=str(self.start.id),
                )
            self.assertEqual(
                blocked.exception.code,
                'URGENT_RISK_CONTAINMENT_PENDING',
            )

            version = decision.version
            event_count = decision.events.count()
            item.save(update_fields=['priority'])
            decision.refresh_from_db()
            self.assertEqual(decision.version, version)
            self.assertEqual(decision.events.count(), event_count)
