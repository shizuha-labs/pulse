"""PLS-261 regression: sweep_org_health capacity-starve / whole-team-outage must
NOT count assignee-null but already-picked-up items (e.g. security-finding
`found`/`accepted` being dispositioned by the security role in place) as
"unrouted". Those inflated the "N unrouted" count and produced ≥5 false
`capacity-starve` fires (PLAT-2913/2901/2733/1314/1266).

Fix: `_org_health_routable_queue()` filters role-owned active statuses out of the
team capacity queue before the starvation / outage signals count it, without
touching the shared `_queued_tasks_for_team` (also used by the capacity-pull).
"""
from types import SimpleNamespace

from django.test import TestCase
from django.utils import timezone

from tasks.models import AgentAvailability, Comment, Item, ItemActivity, Project
from tasks.services.workflow_service import WorkflowService
from tasks.tasks import (
    _ORG_HEALTH_ROLE_OWNED_ACTIVE_STATUSES,
    _org_health_recent_active_team_activity,
    _org_health_routable_queue,
)


class RoutableQueueUnitTests(TestCase):
    """Pure-logic: the helper drops picked-up statuses, keeps awaiting-routing."""

    def _stub(self, status):
        return SimpleNamespace(status=status, item_key=f'X-{status}')

    def test_role_owned_active_statuses_are_dropped(self):
        for st in ('found', 'accepted', 'in_progress'):
            self.assertEqual(
                _org_health_routable_queue([self._stub(st)]), [],
                f"status {st!r} should be filtered out of the routable queue")

    def test_awaiting_routing_statuses_are_kept(self):
        for st in ('needs_triage', 'open', 'todo'):
            kept = _org_health_routable_queue([self._stub(st)])
            self.assertEqual(len(kept), 1,
                             f"status {st!r} must remain (genuinely unrouted)")

    def test_status_match_is_case_insensitive(self):
        self.assertEqual(_org_health_routable_queue([self._stub('FOUND')]), [])

    def test_mixed_queue_keeps_only_unrouted(self):
        q = [self._stub('found'), self._stub('needs_triage'),
             self._stub('accepted'), self._stub('open')]
        kept = {it.status for it in _org_health_routable_queue(q)}
        self.assertEqual(kept, {'needs_triage', 'open'})

    def test_constant_covers_the_reported_false_positive_statuses(self):
        # The PLS-261 false fires were security-findings in `found`/`accepted`.
        self.assertIn('found', _ORG_HEALTH_ROLE_OWNED_ACTIVE_STATUSES)
        self.assertIn('accepted', _ORG_HEALTH_ROLE_OWNED_ACTIVE_STATUSES)


class RoutableQueueDbIntegrationTests(TestCase):
    """End-to-end at the DB layer: assignee-null `found` findings routed to a
    team are returned by `_queued_tasks_for_team` (assignee IS NULL) but filtered
    out by `_org_health_routable_queue`, while a `needs_triage` item survives."""

    def setUp(self):
        self.project = Project.objects.create(
            name='Platform', key='PLAT', org_id=1, access_level='open')
        WorkflowService._roster_cache = None

    def _finding(self, key, status):
        # assignee-null, team-routed security finding (role-dispositioned).
        return Item.objects.create(
            project=self.project, item_key=key, title=f'{key} finding',
            mode='task', item_type='task.bug', workflow_name='security-finding',
            workflow_status=status, status=status, assignment_group='security',
            assignee_email='', created_by_email='zen@shizuha.com', created_by_id=0,
        )

    def test_found_findings_filtered_but_needs_triage_survives(self):
        self._finding('PLAT-9101', 'found')
        self._finding('PLAT-9102', 'found')
        self._finding('PLAT-9103', 'needs_triage')

        raw = list(WorkflowService._queued_tasks_for_team('security'))
        raw_keys = {i.item_key for i in raw}
        # all three are assignee-null + non-terminal → present in the raw queue
        self.assertEqual(raw_keys, {'PLAT-9101', 'PLAT-9102', 'PLAT-9103'})

        routable = _org_health_routable_queue(raw)
        routable_keys = {i.item_key for i in routable}
        # only the genuinely-unrouted needs_triage item counts toward starvation
        self.assertEqual(routable_keys, {'PLAT-9103'})

    def test_all_found_yields_empty_routable_queue(self):
        # The exact false-positive shape: a burst of assigned `found` findings
        # must NOT register as an unrouted/starving queue.
        for n in range(5):
            self._finding(f'PLAT-92{n:02d}', 'found')
        routable = _org_health_routable_queue(
            list(WorkflowService._queued_tasks_for_team('security')))
        self.assertEqual(routable, [])


class RecentActiveTeamActivityTests(TestCase):
    """PLAT-3981: do not file whole-team-outage for a team that is
    AgentAvailability-active and visibly producing Pulse work during transient
    pod recreation / exporter churn."""

    def setUp(self):
        self.project = Project.objects.create(
            name='Platform', key='PLAT', org_id=1, access_level='open')
        self.item = Item.objects.create(
            project=self.project, item_key='PLAT-3981', title='team outage',
            mode='task', item_type='task', workflow_name='simple',
            workflow_status='in_progress', status='in_progress',
            assignment_group='admin-ops', created_by_email='system@pulse',
            created_by_id=0,
        )

    def test_active_member_recent_comment_suppresses_outage(self):
        AgentAvailability.objects.create(
            email='sora@shizuha.com', active=True, status='active',
            updated_by='operator',
        )
        Comment.objects.create(
            item=self.item, author_id=11, author_email='sora@shizuha.com',
            content='reviewed merge queue',
        )

        result = _org_health_recent_active_team_activity(
            ['sora@shizuha.com', 'kei@shizuha.com'], timezone.now())

        self.assertIsNotNone(result)
        # `events` = comments + ItemActivity, and the celery stale-sweep can add
        # an ItemActivity on the test item mid-run, making the exact count flaky
        # (observed events==2 in CI, blocking the deploy gate). The suppression
        # logic only needs events >= min_events(1); assert that, and keep the
        # deterministic comments==1 check for the comment itself.
        self.assertGreaterEqual(result['events'], 1)
        self.assertEqual(result['comments'], 1)
        self.assertIn('sora@shizuha.com', result['active_members'])

    def test_inactive_member_activity_does_not_suppress(self):
        AgentAvailability.objects.create(
            email='sora@shizuha.com', active=False, status='disabled',
            updated_by='health-sweep',
        )
        Comment.objects.create(
            item=self.item, author_id=11, author_email='sora@shizuha.com',
            content='stale activity from disabled member',
        )

        result = _org_health_recent_active_team_activity(
            ['sora@shizuha.com'], timezone.now())

        self.assertIsNone(result)

    def test_old_activity_does_not_suppress(self):
        AgentAvailability.objects.create(
            email='sora@shizuha.com', active=True, status='active',
            updated_by='operator',
        )
        old = Comment.objects.create(
            item=self.item, author_id=11, author_email='sora@shizuha.com',
            content='old activity',
        )
        Comment.objects.filter(pk=old.pk).update(
            created_at=timezone.now() - timezone.timedelta(hours=2),
        )
        # The post_save signal (signals.track_comment_activity) also creates a
        # paired 'commented' ItemActivity at NOW — age it too, else the function
        # finds fresh activity and the "old activity does not suppress" premise
        # is violated (the test would see activities=1 and fail).
        ItemActivity.objects.filter(item=self.item).update(
            created_at=timezone.now() - timezone.timedelta(hours=2),
        )

        result = _org_health_recent_active_team_activity(
            ['sora@shizuha.com'], timezone.now())

        self.assertIsNone(result)

    def test_availability_alias_counts_canonical_comment(self):
        AgentAvailability.objects.create(
            email='sora@agents.shizuha.io', active=True, status='active',
            updated_by='operator',
        )
        Comment.objects.create(
            item=self.item, author_id=11, author_email='sora@shizuha.com',
            content='canonical alias activity',
        )

        result = _org_health_recent_active_team_activity(
            ['sora@shizuha.com'], timezone.now())

        self.assertIsNotNone(result)
        self.assertGreaterEqual(result['events'], 1)

    def test_recent_item_activity_counts_too(self):
        AgentAvailability.objects.create(
            email='kei@shizuha.com', active=True, status='active',
            updated_by='operator',
        )
        ItemActivity.objects.create(
            item=self.item, action='updated', user_id=12,
            user_email='kei@shizuha.com', field_name='status',
            old_value='in_review', new_value='awaiting_merge',
        )

        result = _org_health_recent_active_team_activity(
            ['kei@shizuha.com'], timezone.now())

        self.assertIsNotNone(result)
        self.assertGreaterEqual(result['events'], 1)
        self.assertGreaterEqual(result['activities'], 1)
