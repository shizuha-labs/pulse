from django.test import TestCase

from tasks.models import Item, ItemLink
from tasks.views import OrgChartSnapshotView


class SnapshotWaitingRfcWorkloadTests(TestCase):
    email = 'sora@shizuha.com'
    team = 'architecture'

    def _snapshot_workload(self):
        return OrgChartSnapshotView()._aggregate_workload(org_scope='all')

    def test_active_implementation_followup_is_waiting_not_active(self):
        rfc = Item.objects.create(
            mode='task',
            title='RFC waiting for implementation',
            description='Implementation follow-up PLS-900 will land the change.',
            status='rfc_implementation_linked',
            workflow_name='feature-request-rfc',
            assignee_email=self.email,
            assignment_group=self.team,
            created_by_id=0,
        )
        followup = Item.objects.create(
            mode='task',
            item_key='PLS-900',
            title='Implementation follow-up',
            status='open',
            workflow_name='autonomous-dev',
            assignment_group='engineering',
            created_by_id=0,
        )
        # 37c0c9a dropped the legacy free-text branch (a description naming
        # "PLS-900") from filter_active_queue_items — it cost ~14s of Postgres JIT
        # on every is_active list. Typed implementation links are the durable
        # model (IMPLEMENTATION_LINK_TYPES / PLS-189 / PLS-445), and this snapshot
        # reuses that same canonical predicate, so the carrier must be linked with
        # an implementation link type rather than a generic `relates_to`.
        ItemLink.objects.create(
            source_item=rfc,
            target_item=followup,
            link_type='implemented_by',
            created_by_id=0,
            created_by_email=self.email,
        )

        by_email, by_group, _by_uid, by_assignee_team, _unassigned = (
            self._snapshot_workload()
        )

        self.assertEqual(by_email[self.email]['in_progress'], 0)
        self.assertEqual(by_email[self.email]['blocked'], 1)
        self.assertEqual(by_group[self.team]['in_progress'], 0)
        self.assertEqual(by_group[self.team]['blocked'], 1)
        scoped = by_assignee_team[f'{self.email}::{self.team}']
        self.assertEqual(scoped['in_progress'], 0)
        self.assertEqual(scoped['blocked'], 1)

        followup.status = 'done'
        followup.save(update_fields=['status'])

        by_email, by_group, _by_uid, by_assignee_team, _unassigned = (
            self._snapshot_workload()
        )
        self.assertEqual(by_email[self.email]['in_progress'], 1)
        self.assertEqual(by_email[self.email]['blocked'], 0)
        self.assertEqual(by_group[self.team]['in_progress'], 1)
        self.assertEqual(by_group[self.team]['blocked'], 0)
        scoped = by_assignee_team[f'{self.email}::{self.team}']
        self.assertEqual(scoped['in_progress'], 1)
        self.assertEqual(scoped['blocked'], 0)
