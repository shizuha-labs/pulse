"""PLS-527: park operator-evidence wait on the PLS-529 marker + fingerprint.

Production-order contract (Sora sequencing):
- Consume PLS-529 operator_wait — no parallel authority, no Backlog/Scheduled park.
- First complete probe records checklist+fingerprint while marker stays live.
- 20 unchanged heartbeats → zero external re-probes warranted, zero re-leases.
- Relevant fingerprint change reactivates once and queues one verification.
- Unrelated/identical fingerprints do not reactivate; state survives restarts
  because the fingerprint is persisted on OperatorWaitMarker.
"""
from __future__ import annotations

import os
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from tasks.models import Comment, Item, OperatorWaitMarker, Project, TeamRoster
from tasks.services.actionable_items import filter_active_queue_items
from tasks.services.operator_wait import (
    apply_operator_evidence_event,
    operator_wait_state_for_item,
    record_operator_evidence_probe,
)
from tasks.services.workflow_service import WorkflowService
from .base import create_blocked_item


User = get_user_model()

_SWEEP_ENV = {'PULSE_HEALTH_EXPORTER_URL': '', 'PULSE_API_HEALTH_URL': 'disabled'}
GORO = 'goro@shizuha.com'
SHIZUHA = 'shizuha@shizuha.com'
SHION = 'shion@shizuha.com'
ENGINEER = 'ryo@shizuha.com'
USERS = {
    GORO: 206,
    SHIZUHA: 207,
    SHION: 208,
    ENGINEER: 202,
}


def _user_by_email(email):
    uid = USERS.get((email or '').strip().lower())
    return {'id': uid, 'email': email} if uid else None


def _organization_by_id(org_id):
    return {'id': int(org_id), 'slug': 'shizuha', 'name': 'Shizuha'}


class OperatorEvidenceParkTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(
            name='Evidence park', key='EVP', org_id=1, access_level='open',
        )
        self.digest_project = Project.objects.create(
            name='Evolution', key='EVOL', org_id=1, access_level='open',
        )
        TeamRoster.objects.update_or_create(
            organization_slug='shizuha',
            organization_id=1,
            team_name='admin-ops',
            defaults={
                'tiers': [[GORO, SHIZUHA, SHION]],
                'junior_emails': [],
                'is_active': True,
            },
        )
        WorkflowService._roster_cache = None
        self.digest = Item.objects.create(
            project=self.digest_project,
            item_key='EVOL-36',
            title='Blocked-on-operator digest',
            mode='task',
            workflow_name='autonomous-dev',
            status='scheduled',
            assignment_group='admin-ops',
            assignee_id=USERS[SHION],
            assignee_email=SHION,
        )
        self.root = Item.objects.create(
            project=self.project,
            item_key='BKSH-7',
            title='Canonical operator blocker',
            mode='task',
            workflow_name='autonomous-dev',
            status='open',
            assignment_group='admin-ops',
            assignee_id=USERS[SHION],
            assignee_email=SHION,
            tags='blocker,decision-required,escalation',
        )
        self.parent = create_blocked_item(
            blocker=self.root,
            project=self.project,
            item_key='BKSH-8',
            title='Operator dependency parent',
            mode='task',
            workflow_name='autonomous-dev',
            assignment_group='admin-ops',
            assignee_id=USERS[SHION],
            assignee_email=SHION,
            metadata={'escalation_routing_gate': {
                'classification': 'genuine_decision_or_physical_action',
                'outcome': 'allowed',
            }},
        )
        Comment.objects.create(
            item=self.digest,
            author_id=USERS[SHION],
            author_email=SHION,
            content=(
                '## Blocked-on-operator digest — fixture\n\n'
                'DM delivered to Hritik via Connect '
                '(message_id `a1b2c3d4-e5f6-7890-abcd-ef1234567890`).\n\n'
                f'- **{self.parent.item_key}** — operator evidence required.'
            ),
        )
        self.marker = OperatorWaitMarker.objects.get(item=self.root, state='active')

    def _run_sweep(self, *available_emails):
        from tasks.tasks import sweep_rebalance_pods

        def available(email):
            return (email or '').strip().lower() in {
                value.strip().lower() for value in available_emails
            }

        with mock.patch(
            'tasks.user_service.user_service.get_user_by_email',
            side_effect=_user_by_email,
        ), mock.patch.object(
            WorkflowService, '_disabled_agent_emails', return_value=set(),
        ), mock.patch.object(
            WorkflowService,
            '_is_agent_available_for_assignment',
            side_effect=available,
        ), mock.patch(
            'tasks.services.admin_service.admin_service.get_organization',
            side_effect=_organization_by_id,
        ), mock.patch(
            'tasks.tasks._soft_unavailable_owner_protected', return_value=False,
        ), mock.patch.dict(
            os.environ,
            {**_SWEEP_ENV, 'PULSE_FAILOVER_COOLDOWN_HOURS': '0'},
            clear=False,
        ):
            return sweep_rebalance_pods.apply(
                args=[], kwargs={'max_skew': 0},
            ).get()

    def test_first_probe_records_fingerprint_and_twenty_unchanged_heartbeats(
        self,
    ):
        """BKSH-7/8 production order: park once, silence for 20 beats."""
        checklist = {
            'folder_id': 'drive-folder-bksh7',
            'required': ['bank_statement.pdf', 'gst_return.xlsx'],
        }
        first = record_operator_evidence_probe(
            self.root.pk,
            'fp-drive-v1',
            checklist=checklist,
            source='drive',
        )
        self.assertEqual(first['outcome'], 'recorded')
        self.assertFalse(first['should_probe_external'])
        self.assertFalse(first['reactivated'])

        self.marker.refresh_from_db()
        self.assertEqual(self.marker.state, 'active')
        self.assertEqual(self.marker.evidence_fingerprint, 'fp-drive-v1')
        self.assertEqual(self.marker.evidence_checklist, checklist)

        # Marker keeps the root out of actionable heartbeat demand.
        actionable = list(
            filter_active_queue_items(
                Item.objects.filter(assignee_email=SHION, mode='task')
            ).values_list('pk', flat=True)
        )
        self.assertNotIn(self.root.pk, actionable)

        pre_owner = self.root.assignee_email
        pre_failovers = Comment.objects.filter(
            item=self.root,
            author_email='system@pulse',
            content__startswith='Auto-failover',
        ).count()
        external_probes_warranted = 0

        for beat in range(20):
            state = operator_wait_state_for_item(self.root.pk)
            self.assertTrue(state['live'])
            self.assertFalse(
                state['should_probe_external'],
                f'beat {beat}: parked fingerprint must suppress external probe',
            )
            result = record_operator_evidence_probe(
                self.root.pk,
                'fp-drive-v1',
                source='drive',
            )
            self.assertEqual(result['outcome'], 'unchanged')
            self.assertFalse(result['should_probe_external'])
            if result['should_probe_external']:
                external_probes_warranted += 1
            # Alternate available owners each beat — must not re-lease.
            self._run_sweep(GORO if beat % 2 == 0 else SHIZUHA)

        self.root.refresh_from_db()
        self.marker.refresh_from_db()
        self.assertEqual(external_probes_warranted, 0)
        self.assertEqual(self.root.assignee_email, pre_owner)
        self.assertEqual(
            Comment.objects.filter(
                item=self.root,
                author_email='system@pulse',
                content__startswith='Auto-failover',
            ).count(),
            pre_failovers,
        )
        self.assertEqual(self.marker.evidence_unchanged_probes, 20)
        self.assertEqual(self.marker.state, 'active')
        # Restart survival: fingerprint is on the durable marker row.
        reloaded = OperatorWaitMarker.objects.get(pk=self.marker.pk)
        self.assertEqual(reloaded.evidence_fingerprint, 'fp-drive-v1')

    def test_relevant_fingerprint_change_reactivates_once(self):
        record_operator_evidence_probe(
            self.root.pk, 'fp-drive-v1', source='drive',
        )
        changed = apply_operator_evidence_event(
            self.root.pk, 'fp-drive-v2', source='drive_mutation',
        )
        self.assertEqual(changed['outcome'], 'reactivated')
        self.assertTrue(changed['reactivated'])
        self.assertTrue(changed['verification_queued'])

        self.marker.refresh_from_db()
        self.assertEqual(self.marker.state, 'inactive')
        self.assertEqual(
            self.marker.invalidated_reason, 'evidence_fingerprint_changed',
        )
        self.assertEqual(
            Comment.objects.filter(
                item=self.root,
                origin_event='operator_wait.evidence_reactivated',
                content__contains='Operator-evidence reactivation (PLS-527)',
            ).count(),
            1,
        )

        # Second event on the inactive marker is a no-op (not_waiting).
        again = apply_operator_evidence_event(
            self.root.pk, 'fp-drive-v3', source='drive_mutation',
        )
        self.assertEqual(again['outcome'], 'not_waiting')
        self.assertFalse(again['reactivated'])
        self.assertEqual(
            Comment.objects.filter(
                item=self.root,
                origin_event='operator_wait.evidence_reactivated',
                content__contains='Operator-evidence reactivation (PLS-527)',
            ).count(),
            1,
            'exactly one verification must be queued',
        )

        # One eligible lease after reactivation.
        self._run_sweep(GORO)
        self.root.refresh_from_db()
        self.assertEqual(self.root.assignee_email, GORO)

    def test_identical_fingerprint_event_does_not_reactivate(self):
        record_operator_evidence_probe(
            self.root.pk, 'fp-drive-v1', source='drive',
        )
        same = apply_operator_evidence_event(
            self.root.pk, 'fp-drive-v1', source='unrelated_drive_noise',
        )
        self.assertEqual(same['outcome'], 'unchanged')
        self.assertFalse(same['reactivated'])
        self.marker.refresh_from_db()
        self.assertEqual(self.marker.state, 'active')

    def test_api_probe_and_heartbeat_preflight_skips_operator_wait_only_queue(self):
        agent = User.objects.create_user(
            username='pls527-agent',
            email=SHION,
            password='x',
        )
        client = APIClient()
        client.force_authenticate(user=agent)

        state_resp = client.get(f'/api/items/{self.root.pk}/operator-wait-state/')
        self.assertEqual(state_resp.status_code, 200)
        self.assertTrue(state_resp.data['live'])
        self.assertTrue(state_resp.data['should_probe_external'])

        probe_resp = client.post(
            f'/api/items/{self.root.pk}/operator-evidence-probe/',
            {
                'fingerprint': 'fp-api-1',
                'source': 'drive',
                'checklist': {'folder_id': 'x'},
            },
            format='json',
        )
        self.assertEqual(probe_resp.status_code, 200)
        self.assertEqual(probe_resp.data['outcome'], 'recorded')
        self.assertFalse(probe_resp.data['should_probe_external'])

        # Active blocker roots must remain WIP (ATD) — never park them to
        # backlog. Heartbeat preflight must still skip when the only owned
        # work is operator_wait (no ready_task / backlog_pull wake).
        Item.objects.filter(assignee_email=SHION).exclude(
            pk__in=[self.root.pk, self.parent.pk],
        ).update(assignee_email='', assignee_id=None)
        # Parent is blocked (waiting); root is operator_wait-excluded.
        preflight = client.get(
            '/api/items/heartbeat-preflight/',
            {'assignee_email': SHION},
        )
        self.assertEqual(preflight.status_code, 200)
        self.assertEqual(preflight.data['decision'], 'skip')
        self.assertNotIn(
            preflight.data.get('reason'),
            {'ready_task', 'backlog_pull'},
        )

        # Defensive PLAT-4850 path: a non-root backlog item with an active
        # operator_wait marker must not wake backlog_pull either.
        parked = Item.objects.create(
            project=self.project,
            item_key='EVP-99',
            title='Non-root parked row',
            mode='task',
            workflow_name='autonomous-dev',
            status='backlog',
            assignment_group='admin-ops',
            assignee_id=USERS[SHION],
            assignee_email=SHION,
        )
        OperatorWaitMarker.objects.create(
            item=parked,
            surfaced_item=self.parent,
            digest_item=self.digest,
            digest_comment=self.digest.comments.first(),
            source_revision='fixture-rev',
            source_message_id='a1b2c3d4-e5f6-7890-abcd-ef1234567890',
            lineage_item_ids=[parked.pk],
            state='active',
            evidence_fingerprint='fp-parked',
        )
        preflight2 = client.get(
            '/api/items/heartbeat-preflight/',
            {'assignee_email': SHION},
        )
        self.assertEqual(preflight2.status_code, 200)
        self.assertNotEqual(preflight2.data.get('reason'), 'backlog_pull')
        self.assertEqual(preflight2.data['decision'], 'skip')
