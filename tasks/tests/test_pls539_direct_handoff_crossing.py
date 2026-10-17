"""PLS-539 (hiro/ADR-0010): the REAL guarded Item.save direct-handoff crossing.

Replaces the mocked test_materialized_lane_uses_signed_entitlement (which patched
routing_authority_applies_to_item + subject_authorized_for_item and never executed
the guarded save). These build an ACTUAL materialized RoutingTeam / RoutingSubject /
RoutingTeamBinding / ReceiveAssignmentEntitlement / checkpoint lane and drive the
real PATCH API through Item.save + the DB triggers, across two orgs, asserting hiro's:
  (a) an authorized direct override crosses a materialized guarded lane and lands the
      concrete owner (clearing the guarded group), consuming a one-use direct_handoff
      receipt — WITHOUT the target holding old-lane membership; and org-agnostic
      across shizuha (org 1) and shizuha-trading (org 7);
  (b) a reasonless (unauthorized) direct clear from a guarded lane is still
      routing_authority:direct_writer_denied (400, zero mutation).
"""
import uuid
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management.color import no_style
from django.db import connection
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from unittest.mock import patch

from tasks.models import (
    Item, Project, ReceiveAssignmentEntitlement, RoutingAssignmentReceipt,
    RoutingAuthorityCheckpoint, RoutingSubject, RoutingTeam, RoutingTeamBinding,
    TeamRoster,
)

User = get_user_model()

# org_id -> slug, for the admin_service.get_organization patch.
_ORGS = {1: 'shizuha', 7: 'shizuha-trading'}


class PLS539DirectHandoffCrossingTests(APITestCase):
    def setUp(self):
        self.gate = patch.dict('os.environ', {
            'PULSE_ROUTING_BINDINGS_ENFORCE': '1',
            'PULSE_ROUTING_BINDINGS_TEAMS': 'cortex',
        })
        self.gate.start()
        self.addCleanup(self.gate.stop)
        self.client = APIClient()
        self.actor = User.objects.create_user(
            username='dhactor', email='dhactor@shizuha.com', password='x',
            is_staff=True, is_superuser=True)
        with connection.cursor() as cursor:
            for sql in connection.ops.sequence_reset_sql(no_style(), [User]):
                cursor.execute(sql)

    @staticmethod
    def _orgmap(org_id):
        slug = _ORGS.get(org_id)
        return {'id': org_id, 'slug': slug} if slug else None

    def _seed_lane(self, *, org_id, key, sub_id):
        """A materialized guarded 'cortex' lane in org_id, group-queued item, plus a
        task-org direct-handoff TARGET agent NOT in the cortex lane. Returns (item,
        target_email)."""
        project = Project.objects.create(
            name=key, key=key, org_id=org_id, access_level='open', is_active=True)
        item = Item.objects.create(
            mode='task', item_type='task.bug', title='cross', project=project,
            item_key=f'{key}-1', assignment_group='cortex', status='open')
        team = RoutingTeam.objects.create(
            organization_id=org_id, key='cortex', display_name='Cortex',
            state='active', version=1, digest='1' * 64)
        now = timezone.now()
        member = RoutingSubject.objects.create(
            organization_id=org_id, subject_id=sub_id,
            canonical_email=f'cortexer{sub_id}@shizuha.com', state='active',
            generation=1, digest='2' * 64, observed_at=now,
            lease_until=now + timedelta(seconds=90))
        RoutingAuthorityCheckpoint.objects.create(
            authority_id='shizuha-id', aggregate_key=f'{org_id}:{sub_id}',
            subject_generation=1, observed_version=1, digest=member.digest,
            revocation_high_water=0, checkpoint_generation=1,
            source_event_id=uuid.uuid4(), first_received_at=now,
            source_expires_at=now + timedelta(seconds=90),
            observed_at=now, lease_until=now + timedelta(seconds=90))
        RoutingTeamBinding.objects.create(
            organization_id=org_id, routing_team=team, subject=member,
            state='active', version=1, is_current=True, subject_generation=1,
            command_id=uuid.uuid4(), digest='3' * 64)
        ReceiveAssignmentEntitlement.objects.create(
            organization_id=org_id, subject=member, routing_team=team,
            project=None, workflow_name=item.workflow_name,
            action='receive_assignment', state='active', version=1,
            is_current=True, subject_generation=1,
            grant_source='reviewed_task:PLS-539', grant_issuer_id=77,
            grant_issuer_email='ren@shizuha.com', grant_event_id=uuid.uuid4(),
            command_id=uuid.uuid4(), digest='4' * 64)
        # The direct-handoff TARGET: a task-org agent in some OTHER team, NOT in
        # cortex and NOT a cortex RoutingSubject. No User row -> treated as an agent.
        target = f'decider{sub_id}@shizuha.com'
        TeamRoster.objects.create(
            team_name='other-eng', organization_slug=_ORGS[org_id],
            tiers=[[target]], is_active=True)
        return item, target

    def _patch(self, item, body):
        self.client.force_authenticate(self.actor)
        with patch('tasks.services.admin_service.admin_service.get_organization',
                   side_effect=self._orgmap), \
             patch('tasks.services.admin_service.admin_service.is_org_member',
                   return_value=True):
            return self.client.patch(f'/api/items/{item.id}/', body, format='json')

    def _assert_crosses(self, item, target):
        resp = self._patch(item, {
            'assignee_email': target,
            'direct_assignment_reason': 'named-decider handoff off the cortex lane'})
        self.assertIn(resp.status_code, (200, 202), resp.content)
        item.refresh_from_db()
        self.assertEqual(item.assignee_email, target)
        # Crossed OUT of the guarded lane: the stale cortex group is cleared.
        self.assertIsNone(item.assignment_group,
                          'guarded cortex lane not cleared on the direct handoff')
        # A one-use direct_handoff receipt was minted AND consumed by the trigger.
        receipt = RoutingAssignmentReceipt.objects.filter(
            item=item, action=RoutingAssignmentReceipt.ACTION_DIRECT_HANDOFF).first()
        self.assertIsNotNone(receipt, 'no direct_handoff receipt minted')
        self.assertIsNotNone(receipt.consumed_at, 'direct_handoff receipt not consumed')

    def test_a_authorized_direct_handoff_crosses_materialized_lane_shizuha(self):
        item, target = self._seed_lane(org_id=1, key='DHX1', sub_id=91)
        self._assert_crosses(item, target)

    def test_c_crossing_is_org_agnostic_shizuha_trading(self):
        item, target = self._seed_lane(org_id=7, key='DHX7', sub_id=97)
        self._assert_crosses(item, target)

    def test_b_reasonless_direct_clear_from_guarded_lane_still_denied(self):
        item, target = self._seed_lane(org_id=1, key='DHXB', sub_id=93)
        # No direct_assignment_reason + non-operator actor -> the reason gate rejects
        # BEFORE any mutation; the guarded lane is never crossed.
        resp = self._patch(item, {'assignee_email': target})
        self.assertEqual(resp.status_code, 400, resp.content)
        item.refresh_from_db()
        self.assertFalse(item.assignee_email)
        self.assertEqual(item.assignment_group, 'cortex')
        self.assertFalse(
            RoutingAssignmentReceipt.objects.filter(
                item=item, action=RoutingAssignmentReceipt.ACTION_DIRECT_HANDOFF,
                consumed_at__isnull=False).exists())
