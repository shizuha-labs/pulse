"""PLAT-1054 — resolve-assignee must never return a synthesized `user_<id>` handle.

The alert-notifier DMs the username from /api/teams/resolve-assignee/. When a
resolved agent has no real username, the federated-identity layer falls back to
`user_<uid>` (authentication.py) — NOT a registered Connect username, so the DM
404s and the alert silently strands (observed: `user_24`). The view now drops a
synthesized handle to None so the notifier falls back to a resolvable recipient.
"""
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase, TestCase

from tasks.models import (
    Comment,
    Item,
    ItemActivity,
    ItemLink,
    Project,
    TeamRoster,
    WipSlotReservation,
)
from tasks.services.workflow_service import WorkflowService
from tasks.views import ResolveAssigneeView


def _call(group, resolved_uid, resolved_email, username):
    req = SimpleNamespace(
        query_params={'group': group, 'item_key': 'TEST-1'},
        user=SimpleNamespace(),
    )
    item = SimpleNamespace(
        item_key='TEST-1',
        assignment_group=group,
        project=SimpleNamespace(org_id=1),
    )
    item_queryset = mock.MagicMock()
    item_queryset.select_related.return_value.filter.return_value.first.return_value = item
    with mock.patch('tasks.services.workflow_service.WorkflowService._resolve_team_assignee',
                    return_value=(resolved_uid, resolved_email)), \
         mock.patch(
             'tasks.views.PermissionService.get_user_item_queryset',
             return_value=item_queryset,
         ), \
         mock.patch(
             'tasks.services.routing_authority.routing_candidate_snapshot_for_item',
             return_value={
                 'source': 'team_roster_compat',
                 'source_version': 'fixture',
                 'candidates': [],
                 'reason': '',
             },
         ), \
         mock.patch('tasks.user_service.user_service.get_user_by_id',
                    return_value={'id': resolved_uid, 'username': username,
                                  'email': resolved_email} if resolved_uid else None):
        return ResolveAssigneeView().get(req).data


class ResolveAssigneeSynthesizedUsernameTests(SimpleTestCase):
    def test_synthesized_user_id_dropped_to_none(self):
        d = _call('devops', 24, 'shizuha@shizuha.com', 'user_24')
        self.assertIsNone(d['username'])           # synthesized -> null
        self.assertEqual(d['user_id'], 24)         # uid/email still returned
        self.assertEqual(d['email'], 'shizuha@shizuha.com')
        self.assertTrue(d['available'])            # availability unaffected

    def test_real_username_passes_through(self):
        self.assertEqual(_call('devops', 16, 'mika@shizuha.com', 'mika')['username'], 'mika')
        self.assertEqual(_call('admin-ops', 24, 'shizuha@shizuha.com', 'shizuha')['username'], 'shizuha')

    def test_non_numeric_user_prefix_not_treated_as_synthesized(self):
        # a real handle that merely starts with 'user_' but isn't user_<digits>
        self.assertEqual(_call('review', 5, 'u@shizuha.com', 'user_ace')['username'], 'user_ace')

    def test_missing_username_stays_none(self):
        self.assertIsNone(_call('review', 7, 'g@shizuha.com', None)['username'])

    def test_no_available_agent(self):
        d = _call('review', None, None, None)
        self.assertIsNone(d['username'])
        self.assertFalse(d['available'])


class TerminalOutageResolverCanaryTests(TestCase):
    """PLAT-5349: terminal source -> server-owned equivalent -> resolver."""

    def setUp(self):
        self.project = Project.objects.create(
            name='Platform',
            key='PLAT',
            org_id=1,
            access_level='open',
        )
        TeamRoster.objects.create(
            organization_id=1,
            organization_slug='shizuha',
            team_name='accounting',
            tiers=[['banto@shizuha.com']],
            wip_enforced=True,
            ready_wip_cap=5,
            active_wip_cap=10,
            is_active=True,
        )
        WorkflowService._invalidate_team_roster_caches()
        self.source = Item.objects.create(
            project=self.project,
            item_key='MAIL-79',
            title='terminal accounting work',
            mode='task',
            workflow_name='autonomous-bug',
            workflow_status='open',
            status='open',
            assignment_group='merge',
            labels={'needs': ['credential-broker']},
            created_by_id=9,
            created_by_email='qa@shizuha.com',
        )
        self.owner = Item.objects.create(
            project=self.project,
            item_key='PLAT-4837',
            title='accounting outage regression',
            mode='task',
            workflow_name='autonomous-bug',
            workflow_status='done',
            status='done',
            assignment_group='merge',
            created_by_id=10,
            created_by_email='aoi@shizuha.com',
        )
        self.anchor = Item.objects.create(
            project=self.project,
            item_key='PLAT-4821',
            title='system-filed accounting outage',
            mode='task',
            workflow_name='autonomous-dev',
            workflow_status='done',
            status='done',
            assignment_group='admin-ops',
            metadata={
                'org_health_signal': 'zero-member-outage:accounting',
                'auto_filed': '2026-07-17T14:00:00+00:00',
            },
            created_by_id=0,
            created_by_email='system@pulse',
        )
        ItemLink.objects.create(
            source_item=self.source,
            target_item=self.owner,
            link_type='relates_to',
            created_by_id=9,
            created_by_email='qa@shizuha.com',
        )
        ItemLink.objects.create(
            source_item=self.owner,
            target_item=self.anchor,
            link_type='relates_to',
            created_by_id=10,
            created_by_email='aoi@shizuha.com',
        )
        self.source.status = 'done'
        self.source.workflow_status = 'done'
        self.source.save(update_fields=['status', 'workflow_status', 'updated_at'])

    def _request(self, group='accounting'):
        return SimpleNamespace(
            query_params={'group': group, 'item_key': self.source.item_key},
            user=SimpleNamespace(),
        )

    @staticmethod
    def _snapshot(version='row:1;updated:v1'):
        return {
            'source': 'team_roster_compat',
            'source_version': version,
            'candidates': [{'email': 'banto@shizuha.com'}],
            'reason': '',
        }

    def _permission_queryset(self):
        queryset = mock.MagicMock()
        queryset.select_related.return_value.filter.return_value.first.return_value = self.source
        return queryset

    def test_terminal_original_uses_linked_server_owned_canary(self):
        before = {
            'items': Item.objects.count(),
            'links': ItemLink.objects.count(),
            'comments': Comment.objects.count(),
            'activities': ItemActivity.objects.count(),
            'reservations': WipSlotReservation.objects.count(),
            'source_group': self.source.assignment_group,
            'source_status': self.source.status,
        }
        with mock.patch(
            'tasks.views.PermissionService.get_user_item_queryset',
            return_value=self._permission_queryset(),
        ), mock.patch(
            'tasks.services.workflow_service.WorkflowService._resolve_team_assignee',
            wraps=WorkflowService._resolve_team_assignee,
        ) as resolver, mock.patch(
            'tasks.user_service.user_service.get_user_by_id',
            return_value={
                'id': 112,
                'email': 'banto@shizuha.com',
                'username': 'banto',
            },
        ), mock.patch(
            'tasks.user_service.user_service.get_user_by_email',
            return_value={
                'id': 112,
                'email': 'banto@shizuha.com',
                'username': 'banto',
            },
        ), mock.patch.object(
            WorkflowService,
            '_is_agent_available_for_assignment',
            return_value=True,
        ), mock.patch.object(
            WorkflowService,
            '_member_can_execute_from_status',
            return_value=True,
        ), mock.patch.object(
            WorkflowService,
            '_seat_satisfies_required_capability',
            return_value=True,
        ), mock.patch.object(
            WorkflowService,
            '_agent_active_load',
            return_value=0,
        ), mock.patch.object(
            WorkflowService,
            '_agent_workable_load',
            return_value=0,
        ):
            response = ResolveAssigneeView().get(self._request())

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data['available'])
        self.assertEqual(response.data['email'], 'banto@shizuha.com')
        self.assertEqual(response.data['source'], 'server_owned_outage_canary')
        self.assertEqual(response.data['routing_source'], 'team_roster_compat')
        canary = response.data['canary']
        self.assertEqual(canary['anchor_item_key'], 'PLAT-4821')
        self.assertEqual(
            canary['equivalence_path'],
            ['MAIL-79', 'PLAT-4837', 'PLAT-4821'],
        )
        self.assertEqual(canary['subject'], {
            'organization_id': 1,
            'team_slug': 'accounting',
        })
        self.assertTrue(canary['context_digest'].startswith('sha256:'))
        self.assertEqual(
            canary['required_capabilities'],
            ['credential-broker'],
        )

        call = resolver.call_args
        probe_item = call.kwargs['item']
        self.assertEqual(probe_item.pk, self.source.pk)
        self.assertEqual(probe_item.assignment_group, 'accounting')
        self.assertEqual(probe_item.status, canary['status'])
        self.assertFalse(WorkflowService._status_is_terminal(probe_item.status))
        self.assertEqual(probe_item.assignee_email, '')
        self.assertTrue(probe_item._suppress_routability_holds)
        self.assertEqual(
            WorkflowService._task_required_capabilities(probe_item),
            {'credential-broker'},
        )
        self.assertEqual(call.kwargs['assignment_group_override'], 'accounting')
        self.assertTrue(call.kwargs['enforce_cap'])
        self.assertFalse(call.kwargs['reserve_capacity'])

        self.source.refresh_from_db()
        self.assertEqual(Item.objects.count(), before['items'])
        self.assertEqual(ItemLink.objects.count(), before['links'])
        self.assertEqual(Comment.objects.count(), before['comments'])
        self.assertEqual(ItemActivity.objects.count(), before['activities'])
        self.assertEqual(
            WipSlotReservation.objects.count(),
            before['reservations'],
        )
        self.assertEqual(self.source.assignment_group, before['source_group'])
        self.assertEqual(self.source.status, before['source_status'])

    def test_no_wake_or_live_candidate_remains_unavailable(self):
        with mock.patch(
            'tasks.views.PermissionService.get_user_item_queryset',
            return_value=self._permission_queryset(),
        ), mock.patch(
            'tasks.services.routing_authority.routing_candidate_snapshot_for_item',
            return_value=self._snapshot(),
        ), mock.patch(
            'tasks.services.workflow_service.WorkflowService._resolve_team_assignee',
            return_value=(None, None),
        ):
            response = ResolveAssigneeView().get(self._request())

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data['available'])
        self.assertEqual(response.data['reason'], 'no_routable_candidate')
        self.assertEqual(response.data['source'], 'server_owned_outage_canary')

    def test_authority_version_change_fails_closed_as_stale(self):
        with mock.patch(
            'tasks.views.PermissionService.get_user_item_queryset',
            return_value=self._permission_queryset(),
        ), mock.patch(
            'tasks.services.routing_authority.routing_candidate_snapshot_for_item',
            side_effect=[self._snapshot('v1'), self._snapshot('v2')],
        ), mock.patch(
            'tasks.services.workflow_service.WorkflowService._resolve_team_assignee',
            return_value=(112, 'banto@shizuha.com'),
        ):
            response = ResolveAssigneeView().get(self._request())

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data['available'])
        self.assertEqual(response.data['reason'], 'canary_context_stale')

    def test_canary_evidence_read_failure_is_not_reported_as_a_conflict(self):
        with mock.patch(
            'tasks.views.PermissionService.get_user_item_queryset',
            return_value=self._permission_queryset(),
        ), mock.patch(
            'tasks.services.resolver_canary.ItemActivity.objects.filter',
            side_effect=RuntimeError('activity store unavailable'),
        ), mock.patch(
            'tasks.services.workflow_service.WorkflowService._resolve_team_assignee',
        ) as resolver:
            response = ResolveAssigneeView().get(self._request())

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data, {'error': 'resolver_canary_unavailable'})
        resolver.assert_not_called()

    def test_binding_authorization_uses_the_frozen_canary_workflow(self):
        snapshot = {
            'source': 'routing_binding',
            'source_version': 'team:4;bindings:7:active',
            'candidates': [{
                'user_id': 112,
                'email': 'banto@shizuha.com',
            }],
            'reason': '',
        }
        decision = SimpleNamespace(allowed=True, reason='')
        with mock.patch(
            'tasks.views.PermissionService.get_user_item_queryset',
            return_value=self._permission_queryset(),
        ), mock.patch(
            'tasks.services.routing_authority.routing_candidate_snapshot_for_item',
            return_value=snapshot,
        ), mock.patch(
            'tasks.services.workflow_service.WorkflowService._resolve_team_assignee',
            return_value=(112, 'banto@shizuha.com'),
        ), mock.patch(
            'tasks.services.routing_authority.subject_authorized_for_item',
            return_value=decision,
        ) as authorize:
            response = ResolveAssigneeView().get(self._request())

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data['available'])
        self.assertEqual(response.data['routing_source'], 'routing_binding')
        canary_item = authorize.call_args.args[1]
        self.assertEqual(
            authorize.call_args.kwargs['expected_workflow_name'],
            canary_item.workflow_name,
        )
        self.assertEqual(
            authorize.call_args.kwargs['expected_assignment_group'],
            'accounting',
        )

    def test_unrelated_or_wrong_team_terminal_context_is_rejected(self):
        queryset = self._permission_queryset()
        with mock.patch(
            'tasks.views.PermissionService.get_user_item_queryset',
            return_value=queryset,
        ), mock.patch(
            'tasks.services.workflow_service.WorkflowService._resolve_team_assignee',
        ) as resolver:
            response = ResolveAssigneeView().get(self._request('legal'))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data, {'error': 'routing_context_conflict'})
        resolver.assert_not_called()

        anchor_meta = dict(self.anchor.metadata)
        anchor_meta['org_health_outage'] = {
            'probe_context': {
                'signal': 'zero-member-outage:accounting',
                'subject': {
                    'organization_id': 1,
                    'team_slug': 'legal',
                },
            },
        }
        self.anchor.metadata = anchor_meta
        self.anchor.save(update_fields=['metadata', 'updated_at'])
        with mock.patch(
            'tasks.views.PermissionService.get_user_item_queryset',
            return_value=self._permission_queryset(),
        ), mock.patch(
            'tasks.services.workflow_service.WorkflowService._resolve_team_assignee',
        ) as resolver:
            response = ResolveAssigneeView().get(self._request())
        self.assertEqual(response.status_code, 400)
        resolver.assert_not_called()

        ItemLink.objects.all().delete()
        with mock.patch(
            'tasks.views.PermissionService.get_user_item_queryset',
            return_value=self._permission_queryset(),
        ), mock.patch(
            'tasks.services.workflow_service.WorkflowService._resolve_team_assignee',
        ) as resolver:
            response = ResolveAssigneeView().get(self._request())
        self.assertEqual(response.status_code, 400)
        resolver.assert_not_called()

    def test_exact_org_signal_is_accepted_but_spoofed_creator_is_not(self):
        metadata = dict(self.anchor.metadata)
        metadata['org_health_signal'] = 'zero-member-outage:1:accounting'
        self.anchor.metadata = metadata
        self.anchor.save(update_fields=['metadata', 'updated_at'])

        with mock.patch(
            'tasks.views.PermissionService.get_user_item_queryset',
            return_value=self._permission_queryset(),
        ), mock.patch(
            'tasks.services.routing_authority.routing_candidate_snapshot_for_item',
            return_value=self._snapshot(),
        ), mock.patch(
            'tasks.services.workflow_service.WorkflowService._resolve_team_assignee',
            return_value=(None, None),
        ):
            response = ResolveAssigneeView().get(self._request())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.data['canary']['signal'],
            'zero-member-outage:1:accounting',
        )

        self.anchor.created_by_id = 99
        self.anchor.save(update_fields=['created_by_id', 'updated_at'])
        with mock.patch(
            'tasks.views.PermissionService.get_user_item_queryset',
            return_value=self._permission_queryset(),
        ), mock.patch(
            'tasks.services.workflow_service.WorkflowService._resolve_team_assignee',
        ) as resolver:
            response = ResolveAssigneeView().get(self._request())
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data, {'error': 'routing_context_conflict'})
        resolver.assert_not_called()

    def test_post_terminal_link_cannot_retroactively_authorize_canary(self):
        ItemLink.objects.all().delete()
        ItemLink.objects.create(
            source_item=self.source,
            target_item=self.anchor,
            link_type='relates_to',
            created_by_id=9,
            created_by_email='qa@shizuha.com',
        )
        with mock.patch(
            'tasks.views.PermissionService.get_user_item_queryset',
            return_value=self._permission_queryset(),
        ), mock.patch(
            'tasks.services.workflow_service.WorkflowService._resolve_team_assignee',
        ) as resolver:
            response = ResolveAssigneeView().get(self._request())
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data, {'error': 'routing_context_conflict'})
        resolver.assert_not_called()

    def test_non_terminal_context_cannot_use_outage_canary(self):
        self.source.status = 'open'
        self.source.workflow_status = 'open'
        self.source.save(update_fields=['status', 'workflow_status'])
        with mock.patch(
            'tasks.views.PermissionService.get_user_item_queryset',
            return_value=self._permission_queryset(),
        ), mock.patch(
            'tasks.services.workflow_service.WorkflowService._resolve_team_assignee',
        ) as resolver:
            response = ResolveAssigneeView().get(self._request())
        self.assertEqual(response.status_code, 400)
        resolver.assert_not_called()
