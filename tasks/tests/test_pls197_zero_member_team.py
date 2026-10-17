"""PLS-197 regression: sweep_org_health signal 3b — a team with ZERO members
(absent from TeamRoster entirely, or present with an empty roster) that has
active tasks routed to it must produce an admin-ops finding.

Reproduces the CVQA-L6 R-05 game-day escape (PLAT-540): team created via the
admin service (never registered in Pulse TeamRoster) + one task routed via
assignment_group + zero members for >30 min → no alert fired, because signal 3
iterated only TeamRoster teams and skipped empty member lists.

The debounce streak helper fails OPEN (returns threshold on Redis error), so in
the test environment the finding files on the first sweep cycle.
"""
import os
import threading
import uuid
from datetime import timedelta
from unittest import skipUnless
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection, transaction
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from tasks.models import (
    Comment, Item, Project, RoutingSubject, RoutingTeam, RoutingTeamBinding,
    TeamRoster,
)
from tasks.services.workflow_service import WorkflowService
from tasks.services.notifications import notification_service
from tasks.tasks import (
    _is_terminal_status, _org_health_admin_routing_drift,
    _org_health_admin_team_snapshot, _org_health_exact_routing_lanes,
    _record_zero_member_team_detector_health,
    _reconcile_org_health_signal_carriers, sweep_org_health,
    _report_zero_member_team_detector_failure, send_alert_notification,
    _resolve_zero_member_team_detector_failure_alert,
)


def _mk_project():
    return Project.objects.create(name='PLS-197 Platform', key='P197', org_id=1,
                                  access_level='open')


def _mk_task(project, group, key='PLAT-9001', assignee_email=''):
    """Active task routed to `group`.

    A zero-member Team cannot authorize a concrete assignee, so the valid
    tenant-scoped representation is a Team-queued task with no assignee.
    """
    return Item.objects.create(
        project=project, item_key=key, title=f'{key} injected task',
        mode='task', status='open', workflow_name='autonomous-dev',
        assignment_group=group,
        assignee_email=assignee_email,
        created_by_email='zen@shizuha.com', created_by_id=0,
        created_at=timezone.now(),
    )


class ZeroMemberTeamSignalTest(TestCase):

    def setUp(self):
        self.project = _mk_project()
        # _pod_roster caches for 30s at class level — without a reset, TeamRoster
        # rows created by one test are invisible to the next (stale-cache false
        # fires / missed fires across tests).
        WorkflowService._roster_cache = None

    def _run_sweep(self):
        # Freshness gate only guards signal 3 (exporter-derived); 3b must run
        # regardless — patch it stale to prove independence.
        with patch('tasks.tasks._org_health_exporter_fresh', return_value=False):
            return sweep_org_health.run()

    def _zero_member_findings(self):
        return list(
            Item.objects.filter(assignment_group='admin-ops')
            .filter(metadata__org_health_signal__startswith='zero-member-team:')
        )

    def test_non_roster_team_with_active_task_files_finding(self):
        """The exact R-05 escape: admin-created team, never in TeamRoster."""
        _mk_task(self.project, 'cvqa-r05-test')
        self._run_sweep()
        findings = self._zero_member_findings()
        self.assertEqual(len(findings), 1, 'expected one zero-member-team finding')
        f = findings[0]
        self.assertIn('cvqa-r05-test', f.title)
        self.assertIn('NOT registered in Pulse TeamRoster', f.description)
        self.assertEqual(f.metadata.get('org_health_signal'),
                         'zero-member-team:1:cvqa-r05-test')

    def test_empty_roster_team_with_active_task_files_finding(self):
        """A TeamRoster row that exists but has no members is the same outage."""
        TeamRoster.objects.create(team_name='ghost-team', tiers=[[]], is_active=True)
        _mk_task(self.project, 'ghost-team')
        self._run_sweep()
        findings = self._zero_member_findings()
        self.assertEqual(len(findings), 1)
        self.assertIn('ghost-team', findings[0].title)
        self.assertIn('empty TeamRoster entry', findings[0].description)

    def test_workable_root_on_empty_team_is_visible_to_detectors(self):
        """A chain root stays runnable, so an empty team holding it is an outage."""
        item = _mk_task(self.project, 'external-wait-only', key='PLAT-9077')
        item.status = 'waiting_external'
        with self.assertRaises(ValidationError):
            item.save(update_fields=['status'])
        item.refresh_from_db()
        self.assertNotEqual(item.status, 'waiting_external')

        self._run_sweep()
        signals = set(
            Item.objects.exclude(metadata__org_health_signal__isnull=True)
            .values_list('metadata__org_health_signal', flat=True)
        )
        self.assertTrue(
            any('external-wait-only' in (signal or '') for signal in signals),
            signals,
        )

    def test_staffed_team_does_not_fire(self):
        TeamRoster.objects.create(
            team_name='staffed-team', tiers=[['kai@shizuha.com']], is_active=True)
        _mk_task(self.project, 'staffed-team')
        self._run_sweep()
        self.assertEqual(self._zero_member_findings(), [])

    @patch(
        'tasks.services.admin_service.admin_service.get_organization',
        return_value={
            'id': 4, 'slug': 'personal-3', 'name': 'Hritik Workspace',
            'is_personal': True,
        },
    )
    def test_personal_workspace_zero_member_team_does_not_fire(self, _):
        """PLS-743: a personal workspace has no teams by construction — a missing
        TeamRoster row is the permanent correct answer, not a repairable defect.
        The zero-member-team sweep must emit no carrier for it."""
        project = Project.objects.create(
            name='Personal 4', key='P4', org_id=4, access_level='open')
        _mk_task(project, 'engineering', key='PLAT-5352')
        self._run_sweep()
        self.assertEqual(self._zero_member_findings(), [])
        # No zero-member-team signal for the personal org lane at all.
        signals = set(
            Item.objects.exclude(metadata__org_health_signal__isnull=True)
            .values_list('metadata__org_health_signal', flat=True)
        )
        self.assertNotIn('zero-member-team:4:engineering', signals)

    @patch(
        'tasks.services.admin_service.admin_service.get_organization',
        return_value={
            'id': 1, 'slug': 'shizuha', 'name': 'Shizuha',
            'is_personal': False,
        },
    )
    def test_staffed_org_zero_member_team_still_fires(self, _):
        """PLS-743 regression guard: a non-personal org with a zero-member team
        must still file the carrier — the exclusion is scoped to personal
        workspaces only."""
        _mk_task(self.project, 'cvqa-r05-test')
        self._run_sweep()
        findings = self._zero_member_findings()
        self.assertEqual(len(findings), 1, 'expected one zero-member-team finding')
        self.assertIn('cvqa-r05-test', findings[0].title)

    def test_legacy_alias_cannot_erase_staffed_canonical_roster(self):
        """A stale platform-org alias must not win the flattened roster key."""
        TeamRoster.objects.filter(
            organization_slug='shizuha',
            team_name__in=['engineering', 'trading-engineering'],
        ).delete()
        TeamRoster.objects.create(
            organization_slug='shizuha',
            team_name='engineering',
            tiers=[['haru@shizuha.com']],
            is_active=True,
        )
        # bulk_create deliberately reproduces the pre-canonicalization legacy
        # row still present in production; TeamRoster.save() correctly rejects
        # creating this alias today.
        TeamRoster.objects.bulk_create([TeamRoster(
            organization_slug='shizuha',
            team_name='trading-engineering',
            tiers=[[]],
            is_active=True,
        )])
        WorkflowService._roster_cache = None

        self.assertEqual(
            WorkflowService._assignable_team_members('engineering'),
            ['haru@shizuha.com'],
        )
        _mk_task(self.project, 'engineering')
        self._run_sweep()
        self.assertEqual(self._zero_member_findings(), [])

    def test_detector_lanes_follow_exact_resolver_source_through_tombstone(self):
        """PLAT-4829 regression sequence: exact legacy → binding → HOLD."""
        digital = Project.objects.create(
            name='Digital Platform', key='DP197', org_id=2, access_level='open',
        )
        TeamRoster.objects.update_or_create(
            team_name='review', organization_slug='shizuha',
            defaults={
                'tiers': [['legacy-review@shizuha.com']], 'is_active': True,
            },
        )
        TeamRoster.objects.update_or_create(
            team_name='review', organization_slug='shizuha-digital',
            defaults={
                'tiers': [['digital-review@shizuha.com']], 'is_active': True,
            },
        )
        global_item = _mk_task(self.project, 'review', key='PLAT-4829')
        _mk_task(digital, 'review', key='DP197-1')
        roster_before = list(TeamRoster.objects.order_by('id').values(
            'id', 'organization_id', 'organization_slug', 'team_name', 'tiers',
            'updated_at',
        ))

        legacy = _org_health_exact_routing_lanes(WorkflowService)
        by_org = {lane['organization_id']: lane for lane in legacy}
        self.assertEqual(by_org[1]['source'], 'team_roster_compat')
        self.assertEqual(by_org[1]['members'], ['legacy-review@shizuha.com'])
        self.assertEqual(by_org[2]['members'], ['digital-review@shizuha.com'])

        team = RoutingTeam.objects.create(
            organization_id=1, key='review', display_name='Review',
            state=RoutingTeam.STATE_ACTIVE, version=1, digest='a' * 64,
        )
        subject = RoutingSubject.objects.create(
            organization_id=1, subject_id=4829,
            canonical_email='current-review@shizuha.com',
            state=RoutingSubject.STATE_ACTIVE, generation=1, digest='b' * 64,
        )
        binding = RoutingTeamBinding.objects.create(
            organization_id=1, routing_team=team, subject=subject,
            state=RoutingTeamBinding.STATE_ACTIVE, version=1, is_current=True,
            subject_generation=1, command_id=uuid.uuid4(), digest='c' * 64,
        )
        with patch.dict('os.environ', {
            'PULSE_ROUTING_BINDINGS_ENFORCE': '1',
            'PULSE_ROUTING_BINDINGS_TEAMS': 'review',
        }):
            activated = _org_health_exact_routing_lanes(WorkflowService)
            by_org = {lane['organization_id']: lane for lane in activated}
            self.assertEqual(by_org[1]['source'], 'routing_binding')
            self.assertEqual(by_org[1]['members'], ['current-review@shizuha.com'])
            self.assertEqual(by_org[2]['source'], 'team_roster_compat')
            self.assertEqual(by_org[2]['members'], ['digital-review@shizuha.com'])

            binding.is_current = False
            binding.save(update_fields=['is_current'])
            RoutingTeamBinding.objects.create(
                organization_id=1, routing_team=team, subject=subject,
                state=RoutingTeamBinding.STATE_TOMBSTONED, version=2,
                is_current=True, subject_generation=1, command_id=uuid.uuid4(),
                digest='d' * 64,
            )
            rolled_back = _org_health_exact_routing_lanes(WorkflowService)
            by_org = {lane['organization_id']: lane for lane in rolled_back}
            self.assertEqual(by_org[1]['source'], 'routing_binding')
            self.assertEqual(by_org[1]['members'], [])
            self.assertEqual(by_org[1]['reason'], 'routing_binding_missing')

        global_item.refresh_from_db()
        self.assertIsNone(global_item.assignee_id)
        self.assertEqual(
            list(TeamRoster.objects.order_by('id').values(
                'id', 'organization_id', 'organization_slug', 'team_name', 'tiers',
                'updated_at',
            )),
            roster_before,
        )

    @patch.dict(os.environ, {"JWT_SECRET_KEY": uuid.uuid4().hex})
    def test_admin_drift_uses_real_team_members_contract(self):
        """Admin list metadata cannot substitute for /members identities."""

        class Resp:
            status_code = 200

            def json(self):
                return {
                    'team': 'Review',
                    'count': 4,
                    'members': [
                        {'employee_email': 'revi@shizuha.com'},
                        {'employee_email': 'kei@shizuha.com'},
                        {'employee_email': 'ichi@shizuha.com'},
                        {'employee_email': 'san@shizuha.com'},
                    ],
                }

        list_shape = [{
            'id': 7,
            'name': 'Review',
            'lead_email': 'revi@shizuha.com',
            'member_count': 4,
        }]
        with patch(
            'tasks.services.workflow_service.WorkflowService._fetch_admin_team_pages',
            return_value=list_shape,
        ), patch('requests.get', return_value=Resp()) as get:
            snapshot = _org_health_admin_team_snapshot(1, 'shizuha', 'review')

        get.assert_called_once()
        self.assertTrue(get.call_args.args[0].endswith(
            '/organizations/shizuha/teams/7/members/',
        ))
        self.assertEqual(snapshot['status'], 'ok')
        self.assertEqual(snapshot['member_count'], 4)
        self.assertEqual(snapshot['endpoint_member_count'], 4)
        self.assertEqual(snapshot['members'], [
            'ichi@shizuha.com', 'kei@shizuha.com',
            'revi@shizuha.com', 'san@shizuha.com',
        ])
        drift = _org_health_admin_routing_drift(snapshot, [
            'revi@shizuha.com', 'reika@shizuha.com',
            'rei@shizuha.com', 'rui@shizuha.com',
        ])
        self.assertEqual(drift, {
            'admin_only': [
                'ichi@shizuha.com', 'kei@shizuha.com', 'san@shizuha.com',
            ],
            'routing_only': [
                'rei@shizuha.com', 'reika@shizuha.com', 'rui@shizuha.com',
            ],
        })

    @patch.dict(os.environ, {"JWT_SECRET_KEY": uuid.uuid4().hex})
    def test_incomplete_admin_membership_never_computes_drift(self):
        """A list/member count mismatch is partial evidence, not authority."""

        class Resp:
            status_code = 200

            def json(self):
                return {
                    'team': 'Review',
                    'count': 1,
                    'members': [{'employee_email': 'revi@shizuha.com'}],
                }

        with patch(
            'tasks.services.workflow_service.WorkflowService._fetch_admin_team_pages',
            return_value=[{
                'id': 7, 'name': 'Review', 'lead_email': 'revi@shizuha.com',
                'member_count': 4,
            }],
        ), patch('requests.get', return_value=Resp()):
            snapshot = _org_health_admin_team_snapshot(1, 'shizuha', 'review')

        self.assertEqual(snapshot['status'], 'partial')
        self.assertEqual(snapshot['reason'], 'member_count_mismatch')
        self.assertEqual(snapshot['members'], [])
        self.assertEqual(snapshot['observed_members'], ['revi@shizuha.com'])
        self.assertIsNone(_org_health_admin_routing_drift(
            snapshot, ['legacy-review@shizuha.com'],
        ))

    @patch.dict(os.environ, {"JWT_SECRET_KEY": uuid.uuid4().hex})
    def test_admin_endpoint_count_mismatch_never_computes_drift(self):
        """The canonical envelope count must agree with its member identities."""

        class Resp:
            status_code = 200

            def json(self):
                return {
                    'team': 'Review',
                    'count': 4,
                    'members': [{'employee_email': 'revi@shizuha.com'}],
                }

        with patch(
            'tasks.services.workflow_service.WorkflowService._fetch_admin_team_pages',
            return_value=[{
                'id': 7, 'name': 'Review', 'lead_email': 'revi@shizuha.com',
                'member_count': 1,
            }],
        ), patch('requests.get', return_value=Resp()):
            snapshot = _org_health_admin_team_snapshot(1, 'shizuha', 'review')

        self.assertEqual(snapshot['status'], 'partial')
        self.assertEqual(snapshot['reason'], 'member_count_mismatch')
        self.assertEqual(snapshot['endpoint_member_count'], 4)
        self.assertEqual(snapshot['observed_member_count'], 1)
        self.assertEqual(snapshot['members'], [])
        self.assertIsNone(_org_health_admin_routing_drift(
            snapshot, ['revi@shizuha.com'],
        ))

    @patch('tasks.tasks._run_workflow_stale_timers', return_value=0)
    @patch('requests.post')
    @patch('tasks.tasks._org_health_exporter_fresh', return_value=False)
    def test_exact_lane_snapshot_failure_preserves_prior_detector_state(
            self, _fresh, post, _timers):
        """An incomplete exact snapshot cannot create or clear lane signals."""
        signals = [
            'capacity-starve:1:review',
            'zero-member-outage:1:review',
            'zero-member-team:1:review',
        ]
        carriers = []
        for index, signal in enumerate(signals, start=1):
            carriers.append(Item.objects.create(
                project=self.project,
                item_key=f'P197-{index + 50}',
                title=f'active exact carrier {signal}',
                mode='task',
                status='open',
                workflow_name='autonomous-dev',
                assignment_group=(
                    'program-management'
                    if signal.startswith('capacity-starve:') else 'admin-ops'
                ),
                priority='urgent',
                metadata={'org_health_signal': signal},
            ))
        before = {
            item.pk: (item.status, Comment.objects.filter(item=item).count())
            for item in carriers
        }
        redis = MagicMock()
        redis.set.return_value = True
        redis.scan_iter.return_value = [
            'pulse:org_health_dm:capacity-starve:1:review',
        ]
        attempted = []

        def fail_after_attempted_lane(_workflow_service):
            attempted.append((1, 'review'))
            raise RuntimeError('routing store unavailable after review lane')

        with patch(
            'tasks.tasks._org_health_exact_routing_lanes',
            side_effect=fail_after_attempted_lane,
        ), patch(
            'tasks.services.workflow_service.WorkflowService._redis_client',
            return_value=redis,
        ):
            result = sweep_org_health.run()

        self.assertEqual(attempted, [(1, 'review')])
        self.assertEqual(result['routing_snapshot'], {
            'status': 'failed',
            'lane_count': None,
            'failure_class': 'RuntimeError',
            'attempted_lane_count': 0,
            'completed_lane_count': 0,
        })
        self.assertEqual(result['findings'], 0)
        self.assertEqual(result['dmed'], 0)
        self.assertEqual(result['resolved'], 0)
        self.assertEqual(result['admin_findings'], 0)
        self.assertEqual(result['admin_filed'], 0)
        self.assertEqual(result['admin_resolved'], 0)
        self.assertEqual(Item.objects.filter(
            metadata__org_health_signal__in=signals,
        ).count(), len(signals))
        for item in carriers:
            item.refresh_from_db()
            self.assertEqual(
                (item.status, Comment.objects.filter(item=item).count()),
                before[item.pk],
            )
        redis.delete.assert_not_called()
        post.assert_not_called()

    @patch('tasks.tasks._run_workflow_stale_timers', return_value=0)
    @patch('requests.post')
    @patch('tasks.tasks._org_health_exporter_fresh', return_value=False)
    def test_zero_member_scan_failure_preserves_family_state(
            self, _fresh, post, _timers):
        """A partial signal-3b scan cannot create or clear exact carriers."""
        pending = _mk_task(self.project, 'aaa-empty', key='P197-61')
        _mk_task(self.project, 'review', key='P197-62')
        signal = 'zero-member-team:1:review'
        carrier = Item.objects.create(
            project=self.project,
            item_key='P197-63',
            title='active exact zero-member carrier',
            mode='task',
            status='open',
            workflow_name='autonomous-dev',
            assignment_group='admin-ops',
            priority='urgent',
            metadata={'org_health_signal': signal},
        )
        before = (carrier.status, Comment.objects.filter(item=carrier).count())
        attempted = []
        redis = MagicMock()
        redis.set.return_value = True
        redis.scan_iter.return_value = []

        def partial_snapshot(team, item):
            attempted.append((team, item.pk))
            common = {
                'organization_id': 1,
                'organization_slug': 'shizuha',
                'source': 'team_roster_compat',
                'source_version': 'test-v1',
                'reason': 'team_roster_empty',
            }
            if team == 'review':
                raise RuntimeError('exact item read failed after earlier lane')
            if team == 'aaa-empty':
                return {**common, 'candidates': []}
            return {
                **common,
                'reason': 'candidates_available',
                'candidates': [{'email': 'ops@shizuha.com'}],
            }

        with patch(
            'tasks.tasks._org_health_exact_routing_lanes',
            return_value=[],
        ) as primary_snapshot, patch(
            'tasks.services.routing_authority.routing_candidate_snapshot_for_item',
            side_effect=partial_snapshot,
        ), patch(
            'tasks.services.workflow_service.WorkflowService._redis_client',
            return_value=redis,
        ), patch(
            'tasks.tasks._org_health_admin_team_snapshot',
            return_value={'status': 'ok', 'team_id': 7, 'members': []},
        ):
            result = sweep_org_health.run()

        primary_snapshot.assert_called_once()
        self.assertIn(('aaa-empty', pending.pk), attempted)
        self.assertTrue(any(team == 'review' for team, _item in attempted))
        self.assertEqual(result['routing_snapshot']['status'], 'ok')
        self.assertEqual(result['admin_findings'], 0)
        self.assertEqual(result['admin_filed'], 0)
        self.assertEqual(result['admin_resolved'], 0)
        carrier.refresh_from_db()
        self.assertEqual(
            (carrier.status, Comment.objects.filter(item=carrier).count()),
            before,
        )
        self.assertEqual(Item.objects.filter(
            metadata__org_health_signal__startswith='zero-member-team:',
        ).count(), 1)
        redis.incr.assert_not_called()
        redis.expire.assert_not_called()
        redis.delete.assert_not_called()
        post.assert_not_called()

    @patch('tasks.tasks._run_workflow_stale_timers', return_value=0)
    @patch('requests.post')
    @patch('tasks.tasks._org_health_exporter_fresh', return_value=False)
    def test_complete_zero_member_scan_applies_streak_ops_once(
            self, _fresh, _post, _timers):
        """A complete family scan commits each staged streak operation once."""
        empty = _mk_task(self.project, 'aaa-empty', key='P197-64')
        staffed = _mk_task(self.project, 'zzz-staffed', key='P197-65')
        redis = MagicMock()
        redis.incr.return_value = 1
        redis.set.return_value = False
        redis.scan_iter.return_value = []

        def complete_snapshot(team, item):
            common = {
                'organization_id': 1,
                'organization_slug': 'shizuha',
                'source': 'team_roster_compat',
                'source_version': 'test-v1',
            }
            if team == 'aaa-empty':
                self.assertEqual(item.pk, empty.pk)
                return {
                    **common,
                    'reason': 'team_roster_empty',
                    'candidates': [],
                }
            self.assertEqual((team, item.pk), ('zzz-staffed', staffed.pk))
            return {
                **common,
                'reason': 'candidates_available',
                'candidates': [{'email': 'ops@shizuha.com'}],
            }

        with patch(
            'tasks.tasks._org_health_exact_routing_lanes',
            return_value=[],
        ), patch(
            'tasks.services.routing_authority.routing_candidate_snapshot_for_item',
            side_effect=complete_snapshot,
        ), patch(
            'tasks.services.workflow_service.WorkflowService._redis_client',
            return_value=redis,
        ), patch(
            'tasks.tasks._org_health_admin_team_snapshot',
            return_value={'status': 'ok', 'team_id': 7, 'members': []},
        ):
            result = sweep_org_health.run()

        self.assertEqual(result['admin_findings'], 1)
        redis.incr.assert_called_once_with(
            'pulse:org_health_streak:zero-member-team:1:aaa-empty')
        redis.expire.assert_called_once_with(
            'pulse:org_health_streak:zero-member-team:1:aaa-empty', 3600)
        redis.delete.assert_called_once_with(
            'pulse:org_health_streak:zero-member-team:1:zzz-staffed')

    def test_legacy_deferred_outage_sentinel_is_adopted_before_exact_filing(self):
        """Deploy-before-activation preserves PLAT-4835 and two clean cycles."""
        TeamRoster.objects.update_or_create(
            team_name='review', organization_slug='shizuha',
            defaults={
                'tiers': [['legacy-review@shizuha.com']], 'is_active': True,
            },
        )
        _mk_task(self.project, 'review', key='PLAT-4675')
        sentinel = Item.objects.create(
            project=self.project,
            item_key=self.project.get_next_item_key(),
            title='legacy Review outage containment',
            mode='task', status='deferred', workflow_name='autonomous-dev',
            assignment_group='admin-ops', priority='urgent',
            metadata={'org_health_signal': 'zero-member-outage:review'},
        )
        admin = {
            'status': 'ok', 'team_id': 7, 'member_count': 4,
            'members': [
                'ichi@shizuha.com', 'kei@shizuha.com',
                'revi@shizuha.com', 'san@shizuha.com',
            ],
        }
        unavailable = {'legacy-review@shizuha.com': ['seat_not_live']}
        with patch('tasks.tasks._org_health_exporter_fresh', return_value=True), \
                patch('tasks.tasks._org_health_recent_active_team_activity', return_value=None), \
                patch('tasks.tasks._org_health_candidate_availability', return_value=unavailable), \
                patch('tasks.tasks._org_health_admin_team_snapshot', return_value=admin), \
                patch.dict('os.environ', {
                    'PULSE_ORG_HEALTH_OUTAGE_DEBOUNCE': '1',
                    'PULSE_ORG_HEALTH_OUTAGE_SANITY_MIN': '99',
                }):
            first = sweep_org_health.run()

        sentinel.refresh_from_db()
        exact_signal = 'zero-member-outage:1:review'
        self.assertEqual(sentinel.status, 'deferred')
        self.assertEqual(sentinel.metadata['org_health_signal'], exact_signal)
        self.assertEqual(
            sentinel.metadata['org_health_signal_legacy'],
            'zero-member-outage:review',
        )
        self.assertEqual(first['admin_filed'], 0)
        self.assertEqual(Item.objects.filter(
            metadata__org_health_signal=exact_signal,
        ).count(), 1)

        available = {'legacy-review@shizuha.com': ['available']}
        with patch('tasks.tasks._org_health_exporter_fresh', return_value=True), \
                patch('tasks.tasks._org_health_candidate_availability', return_value=available), \
                patch.dict('os.environ', {
                    'PULSE_ORG_HEALTH_OUTAGE_DEBOUNCE': '1',
                    'PULSE_ORG_HEALTH_OUTAGE_SANITY_MIN': '99',
                }):
            clean_one = sweep_org_health.run()
            clean_two = sweep_org_health.run()

        sentinel.refresh_from_db()
        self.assertEqual(sentinel.status, 'deferred')
        self.assertEqual(clean_one['admin_filed'], 0)
        self.assertEqual(clean_two['admin_filed'], 0)
        self.assertEqual(Item.objects.filter(
            metadata__org_health_signal=exact_signal,
        ).count(), 1)

    def test_legacy_capacity_carriers_adopt_only_their_exact_org(self):
        """A bare carrier cannot suppress the same team in another org."""
        digital = Project.objects.create(
            name='Digital Platform', key='DP197', org_id=2, access_level='open',
        )
        global_carrier = Item.objects.create(
            project=self.project, item_key=self.project.get_next_item_key(),
            title='legacy global capacity sentinel', mode='task',
            status='deferred', workflow_name='autonomous-dev',
            assignment_group='review', priority='high',
            metadata={'org_health_signal': 'capacity-starve:review'},
        )
        digital_carrier = Item.objects.create(
            project=digital, item_key=digital.get_next_item_key(),
            title='legacy digital capacity sentinel', mode='task',
            status='deferred', workflow_name='autonomous-dev',
            assignment_group='review', priority='high',
            metadata={'org_health_signal': 'capacity-starve:review'},
        )

        result = _reconcile_org_health_signal_carriers(
            'capacity-starve:1:review', signal_active=True,
        )
        global_carrier.refresh_from_db()
        digital_carrier.refresh_from_db()
        self.assertEqual(result['carrier'].pk, global_carrier.pk)
        self.assertEqual(
            global_carrier.metadata['org_health_signal'],
            'capacity-starve:1:review',
        )
        self.assertEqual(
            digital_carrier.metadata['org_health_signal'],
            'capacity-starve:review',
        )

        digital_result = _reconcile_org_health_signal_carriers(
            'capacity-starve:2:review', signal_active=True,
        )
        digital_carrier.refresh_from_db()
        self.assertEqual(digital_result['carrier'].pk, digital_carrier.pk)
        self.assertEqual(
            digital_carrier.metadata['org_health_signal'],
            'capacity-starve:2:review',
        )

    def test_sweep_clears_untriaged_carrier_and_duplicate_with_valid_terminal_status(self):
        """An Admin-Ops-local alert self-resolves only after 2+ clean cycles (PLS-854).

        The fire path requires the outage conjunction present for 2+ consecutive
        detector cycles; the clear path now requires the same 2+ consecutive
        absent cycles, so a single clean read must NOT terminalize an urgent
        carrier. Here the signal is absent for two consecutive clean sweeps and
        only then do both carriers resolve, with an evidence-bearing clear comment.
        """
        roster = TeamRoster.objects.create(
            team_name='cross-lane-team',
            tiers=[[]],
            is_active=True,
        )
        _mk_task(self.project, 'cross-lane-team')
        self._run_sweep()
        canonical = self._zero_member_findings()[0]
        duplicate = Item.objects.create(
            project=self.project,
            item_key=self.project.get_next_item_key(),
            title='duplicate signal carrier',
            mode='task',
            status='open',
            workflow_name='autonomous-dev',
            assignment_group='admin-ops',
            priority='urgent',
            metadata={
                'org_health_signal': 'zero-member-team:1:cross-lane-team',
            },
        )

        roster.tiers = [['kai@shizuha.com']]
        roster.save(update_fields=['tiers', 'updated_at'])
        WorkflowService._roster_cache = None
        evidence = ('signal absent for 2+ consecutive detector cycles — '
                    'both legs cleared; queued=0 at clear time (PLS-854-test)')
        with patch(
            'tasks.tasks._org_health_outage_clear_evidence',
            return_value=evidence,
        ):
            clean_one = self._run_sweep()
            canonical.refresh_from_db()
            duplicate.refresh_from_db()
            # First clean cycle may NOT clear (clear debounce = 2).
            self.assertEqual(clean_one['admin_resolved'], 0)
            self.assertFalse(_is_terminal_status(canonical.status))
            self.assertFalse(_is_terminal_status(duplicate.status))

            clean_two = self._run_sweep()
            canonical.refresh_from_db()
            duplicate.refresh_from_db()
            self.assertTrue(_is_terminal_status(canonical.status))
            self.assertTrue(_is_terminal_status(duplicate.status))
            self.assertIsNone(canonical.resolution_id)
            self.assertIsNone(duplicate.resolution_id)
            self.assertEqual(clean_two['admin_resolved'], 2)
            self.assertTrue(Comment.objects.filter(
                item=canonical,
                content__contains='signal cleared',
            ).exists())
            last = Comment.objects.filter(item=canonical).order_by('-created_at').first()
            self.assertIn(evidence, last.content)
        self.assertTrue(Comment.objects.filter(
            item=duplicate,
            content__contains='signal cleared',
        ).exists())

    def test_routed_carrier_survives_clean_then_suppresses_recurrence(self):
        """Production order: onset -> route -> clean -> bad reuses one carrier.

        PLAT-4925 was routed from Admin Ops to Engineering, auto-terminalized on
        one clean detector cycle, then re-fired as PLAT-4933 and PLAT-4934. A
        routed implementation carrier is the durable suppression sentinel until
        its workflow completes; detector reconciliation must not close it.
        """
        roster = TeamRoster.objects.create(
            team_name='cross-lane-team', tiers=[[]], is_active=True,
        )
        _mk_task(self.project, 'cross-lane-team')

        onset = self._run_sweep()
        canonical = self._zero_member_findings()[0]
        canonical.assignment_group = 'engineering'
        canonical.status = 'open'
        canonical.save(update_fields=['assignment_group', 'status'])

        roster.tiers = [['kai@shizuha.com']]
        roster.save(update_fields=['tiers', 'updated_at'])
        WorkflowService._roster_cache = None
        clean = self._run_sweep()

        canonical.refresh_from_db()
        self.assertFalse(_is_terminal_status(canonical.status))
        self.assertEqual(clean['admin_resolved'], 0)

        roster.tiers = [[]]
        roster.save(update_fields=['tiers', 'updated_at'])
        WorkflowService._roster_cache = None
        recurrence = self._run_sweep()

        canonical.refresh_from_db()
        signal = 'zero-member-team:1:cross-lane-team'
        self.assertFalse(_is_terminal_status(canonical.status))
        self.assertEqual(recurrence['admin_filed'], 0)
        self.assertEqual(
            Item.objects.filter(metadata__org_health_signal=signal).count(), 1,
        )
        self.assertEqual(onset['admin_filed'], 1)

    def test_completed_routed_carrier_allows_fresh_recurrence(self):
        """Production reset boundary: completed carrier cannot suppress forever.

        Real caller order is onset sweep -> route -> clean sweep -> owning
        workflow completes -> bad sweep.  The terminal carrier remains audit
        history while one new exact-signal carrier is filed for the new episode.
        """
        roster = TeamRoster.objects.create(
            team_name='cross-lane-team', tiers=[[]], is_active=True,
        )
        _mk_task(self.project, 'cross-lane-team')

        onset = self._run_sweep()
        original = self._zero_member_findings()[0]
        original.assignment_group = 'engineering'
        original.status = 'in_progress'
        original.save(update_fields=['assignment_group', 'status'])

        roster.tiers = [['kai@shizuha.com']]
        roster.save(update_fields=['tiers', 'updated_at'])
        WorkflowService._roster_cache = None
        clean = self._run_sweep()
        original.refresh_from_db()
        self.assertFalse(_is_terminal_status(original.status))
        self.assertEqual(clean['admin_resolved'], 0)

        original.status = 'completed'
        original.save(update_fields=['status'])
        roster.tiers = [[]]
        roster.save(update_fields=['tiers', 'updated_at'])
        WorkflowService._roster_cache = None
        recurrence = self._run_sweep()

        signal = 'zero-member-team:1:cross-lane-team'
        carriers = list(Item.objects.filter(
            metadata__org_health_signal=signal,
        ).order_by('created_at', 'id'))
        original.refresh_from_db()
        self.assertEqual(onset['admin_filed'], 1)
        self.assertEqual(recurrence['admin_filed'], 1)
        self.assertEqual(len(carriers), 2)
        self.assertEqual(carriers[0].pk, original.pk)
        self.assertEqual(original.status, 'completed')
        self.assertNotEqual(carriers[1].pk, original.pk)
        self.assertFalse(_is_terminal_status(carriers[1].status))

    def test_successful_sweep_records_zero_member_detector_liveness(self):
        """The 30-minute backstop proves the event-primary detector is alive."""
        TeamRoster.objects.create(
            team_name='staffed-team',
            tiers=[['kai@shizuha.com']],
            is_active=True,
        )
        _mk_task(self.project, 'staffed-team')
        from tasks import liveness
        with patch(
            'tasks.liveness.record_heartbeat', return_value=123.0,
        ) as heartbeat:
            self._run_sweep()
        heartbeat.assert_any_call(liveness.ZERO_MEMBER_TEAM_DETECTOR)

    def test_carrier_write_failure_never_refreshes_success_liveness(self):
        """A partial sweep is fail-loud and cannot immediately self-heal."""
        TeamRoster.objects.create(
            team_name='write-failure-team', tiers=[[]], is_active=True,
        )
        _mk_task(self.project, 'write-failure-team')
        from tasks import liveness

        failure = RuntimeError('forced carrier write failure')
        with patch.object(
            Item.objects, 'create', side_effect=failure,
        ), patch(
            'tasks.tasks._report_zero_member_team_detector_failure',
        ) as report, patch(
            'tasks.liveness.record_heartbeat', return_value=123.0,
        ) as heartbeat:
            result = self._run_sweep()

        self.assertEqual(result['admin_filed'], 0)
        report.assert_called_once()
        self.assertEqual(
            report.call_args.kwargs['trigger'], 'sweep:file-carrier',
        )
        self.assertIs(report.call_args.kwargs['error'], failure)
        self.assertFalse(any(
            call.args == (liveness.ZERO_MEMBER_TEAM_DETECTOR,)
            for call in heartbeat.call_args_list
        ))

    def test_active_signal_keeps_oldest_cross_lane_carrier_and_closes_duplicate(self):
        """A workflow handoff cannot make the event/sweep dedupe lose its carrier."""
        TeamRoster.objects.create(
            team_name='cross-lane-team',
            tiers=[[]],
            is_active=True,
        )
        task = _mk_task(self.project, 'cross-lane-team')
        self._run_sweep()
        canonical = self._zero_member_findings()[0]
        canonical.assignment_group = 'review'
        canonical.status = 'in_review'
        canonical.save(update_fields=['assignment_group', 'status'])
        duplicate = Item.objects.create(
            project=self.project,
            item_key=self.project.get_next_item_key(),
            title='duplicate signal carrier',
            mode='task',
            status='in_review',
            workflow_name='autonomous-dev',
            assignment_group='qa',
            priority='urgent',
            metadata={
                'org_health_signal': 'zero-member-team:1:cross-lane-team',
            },
        )

        finding = WorkflowService._maybe_file_zero_member_team_alert(
            task,
            'cross-lane-team',
            trigger='assignment',
        )

        canonical.refresh_from_db()
        duplicate.refresh_from_db()
        self.assertEqual(finding.pk, canonical.pk)
        self.assertFalse(_is_terminal_status(canonical.status))
        self.assertTrue(_is_terminal_status(duplicate.status))
        self.assertEqual(
            Item.objects.filter(
                metadata__org_health_signal='zero-member-team:1:cross-lane-team',
            ).exclude(status__in=WorkflowService._assignment_excluded_statuses()).count(),
            1,
        )
        self.assertTrue(Comment.objects.filter(
            item=duplicate,
            content__contains='duplicate signal carrier',
        ).exists())

    def test_empty_roster_cannot_be_overlaid_by_directory_members(self):
        """Directory membership is not assignment authority."""
        TeamRoster.objects.create(team_name='sec-ops', tiers=[[]], is_active=True)
        _mk_task(self.project, 'sec-ops')
        self._run_sweep()
        members = WorkflowService._assignable_team_members(
            'sec-ops', include_active_task_groups=True
        )
        self.assertEqual(members, [])
        self.assertEqual(len(self._zero_member_findings()), 1)

    def test_admin_display_enrichment_follows_paginated_results(self):
        """Admin display enrichment consumes every safe pagination page."""

        class Resp:
            def __init__(self, payload):
                self.status_code = 200
                self._payload = payload

            def json(self):
                return self._payload

        calls = []

        def fake_get(url, **kwargs):
            calls.append(url)
            if url.endswith('/teams/'):
                return Resp({
                    'count': 2,
                    'next': 'https://public.example/admin/api/organizations/shizuha/teams/?page=2',
                    'previous': None,
                    'results': [{'id': 1, 'name': 'Older Team', 'member_count': 0}],
                })
            if url.endswith('/teams/?page=2'):
                return Resp({
                    'count': 2, 'next': None, 'previous': 'ignored',
                    'results': [{'id': 52, 'name': 'Sec Ops', 'member_count': 2}],
                })
            raise AssertionError(f'unexpected URL {url}')

        from unittest.mock import Mock
        requests_module = Mock(get=fake_get)
        records = WorkflowService._fetch_admin_team_pages(
            requests_module,
            'http://admin.test/api',
            'shizuha',
            {'Authorization': 'test'},
        )

        self.assertEqual(calls, [
            'http://admin.test/api/organizations/shizuha/teams/',
            'http://admin.test/api/organizations/shizuha/teams/?page=2',
        ])
        self.assertEqual([r['name'] for r in records], ['Older Team', 'Sec Ops'])

    def test_zero_member_team_without_tasks_does_not_fire(self):
        TeamRoster.objects.create(team_name='idle-empty', tiers=[[]], is_active=True)
        self._run_sweep()
        self.assertEqual(self._zero_member_findings(), [])

    def test_terminal_task_does_not_count(self):
        t = _mk_task(self.project, 'cvqa-r05-test')
        t.status = 'completed'  # in the assignment-excluded status set
        t.save(update_fields=['status'])
        self._run_sweep()
        self.assertEqual(self._zero_member_findings(), [])


class ZeroMemberTeamEventHookTest(APITestCase):
    """R2: assignment is the primary event. Pulse accepts the task and files the
    zero-member alert immediately instead of waiting for sweep_org_health."""

    def setUp(self):
        self.client = APIClient()
        User = get_user_model()
        self.actor = User.objects.create_user(
            username='nagi', email='nagi@shizuha.com', password='x', is_staff=True,
        )
        self.client.force_authenticate(user=self.actor)
        self.project = _mk_project()
        self.item = _mk_task(self.project, 'engineering', key='PLAT-9100')
        WorkflowService._roster_cache = None

    def tearDown(self):
        WorkflowService._roster_cache = None

    @patch('tasks.services.notifications.notification_service.notify_assignment_changed', return_value=None)
    @patch('tasks.tasks.send_item_notification.delay', return_value=None)
    @patch('tasks.tasks.notify_comment_mentions.delay', return_value=None)
    @patch(
        'tasks.services.admin_service.admin_service.get_organization',
        return_value={'id': 1, 'slug': 'shizuha', 'name': 'Shizuha'},
    )
    def test_assign_endpoint_accepts_zero_member_team_and_alerts_now(self, *_):
        TeamRoster.objects.create(
            team_name='cvqa-r05-test', tiers=[[]], is_active=True,
        )
        resp = self.client.post(
            f'/api/items/{self.item.id}/assign/',
            {'assignment_group': 'cvqa-r05-test'},
            format='json',
        )

        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertTrue(resp.data['zero_member_team_alert'])
        self.item.refresh_from_db()
        self.assertEqual(self.item.assignment_group, 'cvqa-r05-test')
        self.assertIsNone(self.item.assignee_id)
        self.assertIn(self.item.assignee_email, (None, ''))
        finding = Item.objects.get(
            assignment_group='admin-ops',
            metadata__org_health_signal='zero-member-team:1:cvqa-r05-test',
        )
        self.assertEqual(finding.priority, 'urgent')
        self.assertIn('Detected immediately on `/assign`', finding.description)
        self.assertTrue(Comment.objects.filter(
            item=self.item,
            content__contains='event-primary detection',
        ).exists())

    def test_staffed_event_keeps_routed_carrier_as_recurrence_sentinel(self):
        """Production order: onset → route → clean event → bad event, one carrier."""
        roster = TeamRoster.objects.create(
            team_name='cvqa-r05-test',
            tiers=[[]],
            is_active=True,
        )
        finding = WorkflowService._maybe_file_zero_member_team_alert(
            self.item,
            'cvqa-r05-test',
            trigger='assignment',
        )
        finding.assignment_group = 'review'
        finding.status = 'in_review'
        finding.save(update_fields=['assignment_group', 'status'])

        roster.tiers = [['kai@shizuha.com']]
        roster.save(update_fields=['tiers', 'updated_at'])
        WorkflowService._roster_cache = None
        cleared = WorkflowService._maybe_file_zero_member_team_alert(
            self.item,
            'cvqa-r05-test',
            trigger='assignment',
        )

        finding.refresh_from_db()
        self.assertIsNone(cleared)
        self.assertFalse(_is_terminal_status(finding.status))
        self.assertIsNone(finding.resolution_id)

        roster.tiers = [[]]
        roster.save(update_fields=['tiers', 'updated_at'])
        WorkflowService._roster_cache = None
        recurrence = WorkflowService._maybe_file_zero_member_team_alert(
            self.item,
            'cvqa-r05-test',
            trigger='assignment',
        )

        finding.refresh_from_db()
        self.assertEqual(recurrence.pk, finding.pk)
        self.assertFalse(_is_terminal_status(finding.status))
        self.assertEqual(Item.objects.filter(
            metadata__org_health_signal='zero-member-team:1:cvqa-r05-test',
        ).count(), 1)

    def test_completed_event_carrier_allows_fresh_recurrence(self):
        """Event order crosses completion and files one carrier for a new episode."""
        roster = TeamRoster.objects.create(
            team_name='cvqa-r05-test', tiers=[[]], is_active=True,
        )
        self.item.assignment_group = 'cvqa-r05-test'
        self.item.save(update_fields=['assignment_group'])
        original = WorkflowService._maybe_file_zero_member_team_alert(
            self.item, 'cvqa-r05-test', trigger='assignment',
        )
        original.assignment_group = 'engineering'
        original.status = 'in_progress'
        original.save(update_fields=['assignment_group', 'status'])

        roster.tiers = [['kai@shizuha.com']]
        roster.save(update_fields=['tiers', 'updated_at'])
        WorkflowService._roster_cache = None
        WorkflowService._maybe_file_zero_member_team_alert(
            self.item, 'cvqa-r05-test', trigger='assignment',
        )
        original.refresh_from_db()
        self.assertFalse(_is_terminal_status(original.status))

        original.status = 'completed'
        original.save(update_fields=['status'])
        roster.tiers = [[]]
        roster.save(update_fields=['tiers', 'updated_at'])
        WorkflowService._roster_cache = None
        recurrence = WorkflowService._maybe_file_zero_member_team_alert(
            self.item, 'cvqa-r05-test', trigger='assignment',
        )

        original.refresh_from_db()
        self.assertEqual(original.status, 'completed')
        self.assertNotEqual(recurrence.pk, original.pk)
        self.assertFalse(_is_terminal_status(recurrence.status))
        self.assertEqual(Item.objects.filter(
            metadata__org_health_signal='zero-member-team:1:cvqa-r05-test',
        ).count(), 2)

    def test_event_clear_failure_routes_bounded_owner_alert(self):
        """A reconciliation error is owner-visible, never only a warning log."""
        TeamRoster.objects.create(
            team_name='cvqa-r05-test',
            tiers=[['kai@shizuha.com']],
            is_active=True,
        )
        failure = RuntimeError('reconcile failed')
        with patch(
            'tasks.tasks._reconcile_org_health_signal_carriers',
            side_effect=failure,
        ), patch(
            'tasks.tasks._report_zero_member_team_detector_failure',
        ) as report:
            result = WorkflowService._maybe_file_zero_member_team_alert(
                self.item, 'cvqa-r05-test', trigger='assignment',
            )

        self.assertIsNone(result)
        report.assert_called_once()
        self.assertEqual(
            report.call_args.kwargs['signal'],
            'zero-member-team:1:cvqa-r05-test',
        )
        self.assertEqual(
            report.call_args.kwargs['trigger'],
            'event:assignment:clear',
        )
        self.assertIs(report.call_args.kwargs['error'], failure)


class ZeroMemberTeamFailLoudTest(TestCase):
    """PLAT-1254 failure delivery and liveness contract."""

    def setUp(self):
        self.project = Project.objects.create(
            name='Shizuha Platform', key='PLAT', org_id=1,
            access_level='open',
        )
        TeamRoster.objects.update_or_create(
            organization_slug='shizuha',
            team_name='engineering',
            defaults={
                'organization_id': 1,
                'tiers': [['kai@shizuha.com']],
                'is_active': True,
            },
        )
        WorkflowService._roster_cache = None

    @patch('tasks.tasks.send_alert_notification.delay')
    def test_repeated_failure_enqueues_one_engineering_owner_dm(
        self, send_dm,
    ):
        # Reproduce the live pre-fix row: the stable incident existed, but its
        # missing project made concrete owner resolution and Connect delivery
        # impossible. The next occurrence must repair it rather than fork it.
        Item.objects.create(
            mode='alert', item_type='alert.error',
            title='Pulse zero-member-team detector failed',
            source='pulse.zero_member_team_detector',
            source_id='zero-member-team-detector-failure',
            status='firing', severity='error', priority='urgent',
            assignment_group='engineering',
            occurrence_count=1,
            first_fired_at=timezone.now(),
            last_fired_at=timezone.now(),
        )
        with patch.object(
            WorkflowService,
            '_is_agent_available_for_assignment',
            return_value=True,
        ), patch(
            'tasks.user_service.user_service.get_user_by_email',
            return_value={'id': 101, 'email': 'kai@shizuha.com'},
        ):
            with self.captureOnCommitCallbacks(execute=True):
                for _ in range(2):
                    _report_zero_member_team_detector_failure(
                        signal='zero-member-team:1:review',
                        trigger='event:assignment:clear',
                        item_key='PLAT-4937',
                        project_id=self.project.id,
                        error=RuntimeError('reconcile failed'),
                    )

        alert = Item.objects.get(
            mode='alert',
            source='pulse.zero_member_team_detector',
            source_id='zero-member-team-detector-failure',
            status='firing',
        )
        self.assertEqual(alert.occurrence_count, 2)
        self.assertEqual(alert.project_id, self.project.id)
        self.assertEqual(alert.assignment_group, 'engineering')
        self.assertEqual(alert.assignee_id, 101)
        self.assertEqual(alert.assignee_email, 'kai@shizuha.com')
        send_dm.assert_called_once_with(alert.id)
        legacy = Item.objects.get(
            mode='alert',
            source='pulse.zero_member_team_detector',
            source_id='zero-member-team-detector-failure',
            project__isnull=True,
        )
        self.assertEqual(legacy.status, 'resolved')
        self.assertEqual(
            legacy.resolved_by,
            'zero-member-team-detector:scoped-replacement',
        )

        # Exercise the real queued-worker recipient + notification boundary,
        # not merely the fact that ``delay(alert.id)`` was called.
        with patch.object(
            notification_service, '_send_push', return_value=True,
        ), patch(
            'tasks.services.notifications.requests.post',
        ) as connect_post:
            connect_post.return_value.status_code = 200
            result = send_alert_notification.run(alert.id)

        self.assertIn('Sent notification for alert', result)
        connect_post.assert_called_once()
        self.assertEqual(
            connect_post.call_args.kwargs['json']['recipient_user_id'], 101,
        )

    @patch('tasks.tasks.send_alert_notification.delay')
    def test_org_b_sweep_failure_routes_only_to_org_b_engineering(
        self, send_dm,
    ):
        """Production sweep: an exact org-B failure cannot inherit org A scope.

        ``sweep_org_health`` historically selected the first global ``PLAT``
        project before scanning exact organization lanes.  Reproduce that
        caller order with a PLAT project in each organization, fail org B's
        onset reconciliation, and cross the real alert recipient + Connect
        worker boundary.  The firing incident and recipient must both remain
        in org B even though org A's project was created first.
        """
        org_a_email = 'org-a-engineer@shizuha.com'
        org_b_email = 'org-b-engineer@shizuha.com'
        TeamRoster.objects.update_or_create(
            organization_slug='shizuha', team_name='engineering',
            defaults={
                'organization_id': 1,
                'tiers': [[org_a_email]],
                'is_active': True,
            },
        )
        org_b_project = Project.objects.create(
            name='Digital Platform', key='PLAT', org_id=2,
            access_level='open',
        )
        TeamRoster.objects.create(
            organization_slug='shizuha-digital', organization_id=2,
            team_name='engineering', tiers=[[org_b_email]], is_active=True,
        )
        TeamRoster.objects.create(
            organization_slug='shizuha-digital', organization_id=2,
            team_name='review', tiers=[[]], is_active=True,
        )
        _mk_task(org_b_project, 'review', key='PLAT-4937-B')
        wrong_scope = Item.objects.create(
            project=self.project,
            mode='alert', item_type='alert.error',
            title='Pulse zero-member-team detector failed',
            source='pulse.zero_member_team_detector',
            source_id='zero-member-team-detector-failure',
            status='firing', severity='error', priority='urgent',
            assignment_group='engineering',
            assignee_id=101, assignee_email=org_a_email,
            occurrence_count=1,
            first_fired_at=timezone.now(),
            last_fired_at=timezone.now(),
        )
        WorkflowService._roster_cache = None

        real_reconcile = _reconcile_org_health_signal_carriers
        failure = RuntimeError('forced org-B sweep reconcile failure')

        def fail_org_b(signal, **kwargs):
            if signal == 'zero-member-team:2:review':
                raise failure
            return real_reconcile(signal, **kwargs)

        users = {
            org_a_email: {'id': 101, 'email': org_a_email},
            org_b_email: {'id': 202, 'email': org_b_email},
        }
        with patch.object(
            WorkflowService,
            '_is_agent_available_for_assignment',
            return_value=True,
        ), patch(
            'tasks.user_service.user_service.get_user_by_email',
            side_effect=lambda email: users.get(email),
        ), patch(
            'tasks.tasks._reconcile_org_health_signal_carriers',
            side_effect=fail_org_b,
        ), patch(
            'tasks.tasks._org_health_exporter_fresh', return_value=False,
        ):
            with self.captureOnCommitCallbacks(execute=True):
                with self.assertRaisesRegex(
                    RuntimeError, 'forced org-B sweep reconcile failure',
                ):
                    sweep_org_health.run()

        alert = Item.objects.get(
            mode='alert',
            source='pulse.zero_member_team_detector',
            source_id='zero-member-team-detector-failure',
            status='firing',
        )
        self.assertEqual(alert.project_id, org_b_project.id)
        self.assertEqual(alert.project.org_id, 2)
        self.assertEqual(alert.assignee_id, 202)
        self.assertEqual(alert.assignee_email, org_b_email)
        self.assertEqual(alert.payload['project_id'], org_b_project.id)
        send_dm.assert_called_once_with(alert.id)
        wrong_scope.refresh_from_db()
        self.assertEqual(wrong_scope.status, 'resolved')
        self.assertEqual(
            wrong_scope.resolved_by,
            'zero-member-team-detector:scoped-replacement',
        )

        with patch.object(
            notification_service, '_send_push', return_value=True,
        ), patch(
            'tasks.services.notifications.requests.post',
        ) as connect_post:
            connect_post.return_value.status_code = 200
            result = send_alert_notification.run(alert.id)

        self.assertIn('Sent notification for alert', result)
        connect_post.assert_called_once()
        self.assertEqual(
            connect_post.call_args.kwargs['json']['recipient_user_id'], 202,
        )

    @patch('tasks.tasks.send_alert_notification.delay')
    def test_unrelated_event_cannot_clear_failure_or_refresh_global_liveness(
        self, _send_dm,
    ):
        """Production callers: failure(A) -> success(B) -> success(A) -> sweep.

        The exact assignment event hook is intentionally invoked three times,
        including its real reconcile boundary. This guards the caller sequence
        that previously let any exact event reset detector-wide failure state;
        directly repeating the health helper would not exercise that boundary.
        """
        from tasks import liveness

        for team in ('review', 'qa'):
            TeamRoster.objects.update_or_create(
                organization_slug='shizuha',
                team_name=team,
                defaults={
                    'organization_id': 1,
                    'tiers': [['kai@shizuha.com']],
                    'is_active': True,
                },
            )
        WorkflowService._roster_cache = None
        item_a = _mk_task(
            self.project, 'review', key='PLAT-4937-A',
        )
        item_b = _mk_task(
            self.project, 'qa', key='PLAT-4937-B',
        )
        real_reconcile = _reconcile_org_health_signal_carriers
        failed_once = False

        def fail_first_a(signal, **kwargs):
            nonlocal failed_once
            if signal == 'zero-member-team:1:review' and not failed_once:
                failed_once = True
                raise RuntimeError('review reconcile failed')
            return real_reconcile(signal, **kwargs)

        with patch.object(
            WorkflowService,
            '_is_agent_available_for_assignment',
            return_value=True,
        ), patch(
            'tasks.user_service.user_service.get_user_by_email',
            return_value={'id': 101, 'email': 'kai@shizuha.com'},
        ), patch(
            'tasks.tasks._reconcile_org_health_signal_carriers',
            side_effect=fail_first_a,
        ), patch(
            'tasks.liveness.record_heartbeat', return_value=123.0,
        ) as heartbeat:
            with self.captureOnCommitCallbacks(execute=True):
                WorkflowService._maybe_file_zero_member_team_alert(
                    item_a, 'review', trigger='assignment',
                )
            with self.captureOnCommitCallbacks(execute=True):
                WorkflowService._maybe_file_zero_member_team_alert(
                    item_b, 'qa', trigger='assignment',
                )
            with self.captureOnCommitCallbacks(execute=True):
                WorkflowService._maybe_file_zero_member_team_alert(
                    item_a, 'review', trigger='assignment',
                )
            incident = Item.objects.get(
                mode='alert',
                source='pulse.zero_member_team_detector',
                source_id='zero-member-team-detector-failure',
                status='firing',
            )
            heartbeat.assert_not_called()
            with self.captureOnCommitCallbacks(execute=True), patch(
                'tasks.tasks._org_health_exporter_fresh', return_value=False,
            ):
                sweep_org_health.run()
        heartbeat.assert_called_once_with(
            liveness.ZERO_MEMBER_TEAM_DETECTOR,
        )
        incident.refresh_from_db()
        self.assertEqual(incident.status, 'resolved')

    def test_failure_does_not_refresh_success_heartbeat(self):
        from tasks import liveness

        with patch(
            'tasks.liveness.record_heartbeat', return_value=123.0,
        ) as heartbeat:
            _record_zero_member_team_detector_health(
                state='failure', signal='zero-member-team:1:review',
                trigger='sweep:clear-reconcile', error='boom',
            )
            heartbeat.assert_not_called()
            _record_zero_member_team_detector_health(
                state='success', signal='zero-member-team:*',
                trigger='sweep:complete',
                complete_scope=True,
            )
        heartbeat.assert_called_once_with(
            liveness.ZERO_MEMBER_TEAM_DETECTOR,
        )


class ZeroMemberTeamIncidentConcurrencyTest(TransactionTestCase):
    """The detector-wide incident is linearizable across real DB sessions."""

    def setUp(self):
        self.project_a = Project.objects.create(
            name='Global Platform', key='PLAT', org_id=1,
            access_level='open',
        )
        self.project_b = Project.objects.create(
            name='Digital Platform', key='PLAT', org_id=2,
            access_level='open',
        )
        self.owners = {
            1: (101, 'org-a-engineer@shizuha.com'),
            2: (202, 'org-b-engineer@shizuha.com'),
        }
        TeamRoster.objects.update_or_create(
            organization_slug='shizuha', team_name='engineering',
            defaults={
                'organization_id': 1, 'tiers': [[self.owners[1][1]]],
                'is_active': True,
            },
        )
        TeamRoster.objects.update_or_create(
            organization_slug='shizuha-digital', team_name='engineering',
            defaults={
                'organization_id': 2, 'tiers': [[self.owners[2][1]]],
                'is_active': True,
            },
        )
        TeamRoster.objects.update_or_create(
            organization_slug='shizuha', team_name='review',
            defaults={
                'organization_id': 1,
                'tiers': [['org-a-reviewer@shizuha.com']],
                'is_active': True,
            },
        )
        TeamRoster.objects.update_or_create(
            organization_slug='shizuha-digital', team_name='review',
            defaults={
                'organization_id': 2,
                'tiers': [['org-b-reviewer@shizuha.com']],
                'is_active': True,
            },
        )
        WorkflowService._roster_cache = None

    @skipUnless(
        connection.vendor == 'postgresql',
        'detector identity/row-lock concurrency requires PostgreSQL',
    )
    @patch('tasks.tasks.send_alert_notification.delay')
    def test_concurrent_two_org_failures_create_one_incident_and_one_dm(
        self, send_dm,
    ):
        """Two empty-first selects cannot fork detector-wide ownership.

        This is the production first-incident order from the event hook: two
        exact organizations fail concurrently before any stable-source row
        exists. The first committed, deliverable exact-org incident becomes the
        detector-wide owner latch; the other occurrence increments it without
        moving tenant-bound payload or enqueueing a second initial notification.
        """
        barrier = threading.Barrier(2)
        errors = []
        errors_lock = threading.Lock()
        item_a = _mk_task(
            self.project_a, 'review', key='PLAT-4937-A',
        )
        item_b = _mk_task(
            self.project_b, 'review', key='PLAT-4937-B',
        )

        def resolve_owner(_group, **kwargs):
            item = kwargs['item']
            return self.owners[item.project.org_id]

        def worker(item_id):
            close_old_connections()
            try:
                item = Item.objects.get(pk=item_id)
                barrier.wait(timeout=10)
                WorkflowService._maybe_file_zero_member_team_alert(
                    item, 'review', trigger='assignment',
                )
            except Exception as exc:  # surfaced below with the exact exception
                with errors_lock:
                    errors.append(exc)
            finally:
                close_old_connections()

        with patch.object(
            WorkflowService, '_resolve_team_assignee',
            side_effect=resolve_owner,
        ), patch(
            'tasks.tasks._reconcile_org_health_signal_carriers',
            side_effect=RuntimeError('concurrent event reconcile failed'),
        ):
            threads = [
                threading.Thread(
                    target=worker, args=(item_a.id,), daemon=True,
                ),
                threading.Thread(
                    target=worker, args=(item_b.id,), daemon=True,
                ),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=15)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        incidents = Item.objects.filter(
            mode='alert',
            source='pulse.zero_member_team_detector',
            source_id='zero-member-team-detector-failure',
        )
        self.assertEqual(incidents.count(), 1)
        active = incidents.get(status='firing')
        self.assertEqual(active.occurrence_count, 2)
        signal_org_id = int(active.payload['signal'].split(':', 2)[1])
        self.assertEqual(active.project.org_id, signal_org_id)
        self.assertEqual(
            (active.assignee_id, active.assignee_email),
            self.owners[signal_org_id],
        )
        send_dm.assert_called_once_with(active.id)

    @skipUnless(
        connection.vendor == 'postgresql',
        'detector identity/row-lock concurrency requires PostgreSQL',
    )
    def test_newer_refire_survives_older_recovery_cutoff(self):
        """Recovery waits on a writer, then re-checks the committed timestamp.

        The refire transaction holds the alert row after writing a timestamp
        newer than the complete-sweep observation. A second DB connection enters
        recovery and blocks. Once the writer commits, recovery must observe the
        newer row version and leave both firing state and timestamp untouched.
        """
        cutoff = timezone.now()
        old_fired_at = cutoff - timedelta(seconds=1)
        new_fired_at = cutoff + timedelta(seconds=1)
        alert = Item.objects.create(
            project=self.project_a,
            mode='alert', item_type='alert.error',
            title='Pulse zero-member-team detector failed',
            source='pulse.zero_member_team_detector',
            source_id='zero-member-team-detector-failure',
            status='firing', severity='error', priority='urgent',
            assignment_group='engineering',
            assignee_id=self.owners[1][0],
            assignee_email=self.owners[1][1],
            payload={'signal': 'zero-member-team:1:review'},
            occurrence_count=1,
            first_fired_at=old_fired_at,
            last_fired_at=old_fired_at,
        )

        writer_ready = threading.Event()
        recovery_attempting = threading.Event()
        allow_writer_commit = threading.Event()
        recovery_done = threading.Event()
        start_barrier = threading.Barrier(2)
        errors = []
        errors_lock = threading.Lock()

        def refire():
            close_old_connections()
            try:
                with transaction.atomic():
                    current = Item.objects.select_for_update().get(pk=alert.pk)
                    current.occurrence_count += 1
                    current.last_fired_at = new_fired_at
                    current.save(update_fields=[
                        'occurrence_count', 'last_fired_at', 'updated_at',
                    ])
                    writer_ready.set()
                    start_barrier.wait(timeout=10)
                    if not allow_writer_commit.wait(timeout=10):
                        raise TimeoutError('recovery never attempted the row lock')
            except Exception as exc:  # surfaced below with the exact exception
                with errors_lock:
                    errors.append(exc)
            finally:
                close_old_connections()

        def recover():
            close_old_connections()
            try:
                if not writer_ready.wait(timeout=10):
                    raise TimeoutError('refire never acquired the row lock')
                start_barrier.wait(timeout=10)
                recovery_attempting.set()
                _resolve_zero_member_team_detector_failure_alert(
                    recovered_before=cutoff,
                )
            except Exception as exc:  # surfaced below with the exact exception
                with errors_lock:
                    errors.append(exc)
            finally:
                recovery_done.set()
                close_old_connections()

        writer_thread = threading.Thread(target=refire, daemon=True)
        recovery_thread = threading.Thread(target=recover, daemon=True)
        writer_thread.start()
        recovery_thread.start()
        self.assertTrue(recovery_attempting.wait(timeout=10))
        self.assertFalse(
            recovery_done.wait(timeout=0.2),
            'recovery should be blocked behind the uncommitted re-fire',
        )
        allow_writer_commit.set()
        writer_thread.join(timeout=15)
        recovery_thread.join(timeout=15)

        self.assertFalse(writer_thread.is_alive())
        self.assertFalse(recovery_thread.is_alive())
        self.assertEqual(errors, [])
        alert.refresh_from_db()
        self.assertEqual(alert.status, 'firing')
        self.assertEqual(alert.occurrence_count, 2)
        self.assertEqual(alert.last_fired_at, new_fired_at)
        self.assertIsNone(alert.resolved_at)
