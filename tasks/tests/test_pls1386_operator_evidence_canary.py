"""PLS-1386: the operator-evidence live-canary harness command contract.

The command is the PLS-1323 live-acceptance prerequisite DevOps executes
against the serving stack. These tests pin its contract inside CI:
- refuse the production org (fail-closed boundary),
- refuse unbound source unless explicitly acknowledged,
- all four PLS-1323 beats PASS with zero stubbed egress,
- --cleanup removes exactly the seeded rows.
"""
from __future__ import annotations

import os
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TransactionTestCase

from tasks.models import Item, OperatorWaitMarker, Project


CANARY_COMMAND = 'pls1386_operator_evidence_canary'
TEST_ORG_ID = 99_001


class PLS1386CanaryCommandTests(TransactionTestCase):
    def _run(self, *args):
        env = {'PULSE_SOURCE_SHA': 'pls1386-test-source-sha'}
        with mock.patch.dict(os.environ, env, clear=False):
            call_command(CANARY_COMMAND, *args)

    def test_refuses_production_org_fail_closed(self):
        with self.assertRaises(CommandError) as ctx:
            self._run('--org-id', '1')
        self.assertIn('production organization', str(ctx.exception))

    def test_refuses_unbound_source(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('PULSE_SOURCE_SHA', None)
            with self.assertRaises(CommandError) as ctx:
                call_command(CANARY_COMMAND, '--org-id', str(TEST_ORG_ID))
        self.assertIn('PULSE_SOURCE_SHA', str(ctx.exception))

    def test_all_four_beats_green_with_zero_egress(self):
        self._run('--org-id', str(TEST_ORG_ID), '--unchanged-beats', '3')
        project = Project.objects.get(key='PLS1386C', org_id=TEST_ORG_ID)
        root = project.items.get(item_key='PLS1386-CANARY-ROOT')
        # PLAT-4215: the surfaced child must have entered `blocked` through the
        # owned-blocker path (durable root linked as its active assigned
        # blocker), never a direct `blocked` create.
        surfaced = project.items.get(item_key='PLS1386-CANARY-SURFACED')
        self.assertEqual(surfaced.status, 'blocked')
        self.assertTrue(
            surfaced.outward_links.filter(
                link_type='is_blocked_by', target_item=root,
            ).exists(),
        )
        marker = OperatorWaitMarker.objects.get(item=root)
        # Relevant change tombstoned the canary root exactly once.
        self.assertEqual(marker.state, 'inactive')
        self.assertEqual(marker.invalidated_reason, 'evidence_fingerprint_changed')
        # Tombstone increments generation before the exactly-once queue
        # stamps verification_queued_generation to that new generation.
        self.assertEqual(marker.generation, 2)
        self.assertEqual(marker.verification_queued_generation, 2)
        verifications = root.comments.filter(
            origin_event='operator_wait.evidence_reactivated',
        )
        self.assertEqual(verifications.count(), 1)
        payload = verifications.first().origin_payload or {}
        self.assertEqual(payload.get('fingerprint'), 'pls1386-canary-fingerprint-B')

    def test_cleanup_removes_exactly_the_seed(self):
        self._run('--org-id', str(TEST_ORG_ID))
        self.assertTrue(
            Project.objects.filter(key='PLS1386C', org_id=TEST_ORG_ID).exists()
        )
        self._run('--org-id', str(TEST_ORG_ID), '--cleanup')
        self.assertFalse(
            Project.objects.filter(key='PLS1386C', org_id=TEST_ORG_ID).exists()
        )
        self.assertFalse(
            Item.objects.filter(item_key__startswith='PLS1386-CANARY-').exists()
        )
        self.assertFalse(
            OperatorWaitMarker.objects.filter(
                item__item_key__startswith='PLS1386-CANARY-',
            ).exists()
        )
