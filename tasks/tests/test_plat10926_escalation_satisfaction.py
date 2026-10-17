"""PLAT-10926: typed satisfaction releases a superseded escalation carrier.

PLS-89 must still refuse a bare terminalization. A validated trail pointer
on the releasing carrier is the only override.
"""

from unittest import mock

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase

from tasks.services.escalation_satisfaction import (
    SCHEMA,
    EscalationSatisfactionError,
    satisfaction_overrides_manual_block,
    validate_escalation_satisfaction,
)


def _evidence():
    return {
        'schema': SCHEMA,
        'trail_pointer': 'PLAT-10275:kube-config-populated',
        'acceptance_check': 'org secret KUBE_CONFIG_B64 is populated',
        'superseded': True,
    }


class EscalationSatisfactionTests(SimpleTestCase):
    def test_valid_record_normalizes(self):
        stamped = validate_escalation_satisfaction(_evidence())
        self.assertEqual(stamped['schema'], SCHEMA)
        self.assertTrue(stamped['superseded'])

    def test_missing_trail_is_rejected(self):
        raw = _evidence()
        raw['trail_pointer'] = 'short'
        with self.assertRaises(EscalationSatisfactionError):
            validate_escalation_satisfaction(raw)

    def test_role_strength_is_not_evidence(self):
        self.assertFalse(satisfaction_overrides_manual_block({'role': 'admin'}))
        self.assertFalse(satisfaction_overrides_manual_block(None))

    def test_stamped_record_overrides_pls89(self):
        self.assertTrue(satisfaction_overrides_manual_block({
            'escalation_satisfaction': _evidence(),
        }))


class EscalationSatisfactionReleaseTests(TestCase):
    """Workflow path: typed evidence releases; a bare close does not stamp."""

    def setUp(self):
        from tasks.models import (
            Item, ItemLink, Project, Status, Workflow, WorkflowStatus,
            WorkflowTransition,
        )
        self.Item = Item
        self.ItemLink = ItemLink
        self.actor = User.objects.create_user(
            username='plat10926', email='plat10926@shizuha.com', password='x',
            is_staff=True,
        )
        self.project = Project.objects.create(
            name='PLAT-10926', key='P10926', org_id=1, is_active=True,
        )
        statuses = {}
        for slug, name, category in (
            ('in_progress', 'In Progress', 'in_progress'),
            ('blocked', 'Blocked', 'in_progress'),
            ('done', 'Done', 'done'),
        ):
            statuses[slug], _ = Status.objects.get_or_create(
                slug=slug, defaults={'name': name, 'category': category},
            )
        self.workflow = Workflow.objects.create(
            name='PLAT-10926 release', slug='plat10926-release',
            initial_status=statuses['in_progress'], is_active=True,
        )
        for position, slug in enumerate(statuses):
            WorkflowStatus.objects.create(
                workflow=self.workflow, status=statuses[slug], position=position,
            )
        self.done = WorkflowTransition.objects.create(
            workflow=self.workflow, name='Complete',
            from_status='in_progress', to_status='done', position=0,
        )
        # PLS-577: the restore path only uses a documented Blocker-Resolved
        # recovery edge (name must start with 'Blocker Resolved').
        self.resume = WorkflowTransition.objects.create(
            workflow=self.workflow, name='Blocker Resolved - Resume',
            from_status='blocked', to_status='in_progress', position=1,
        )
        # The org-roster authorization fence requires the assignee to be in
        # the assignment group's roster row for the task organization.
        from tasks.models import TeamRoster
        roster, _ = TeamRoster.objects.get_or_create(
            organization_id=self.project.org_id,
            team_name='test-fixtures',
            defaults={
                'organization_slug': f'test-org-{self.project.org_id}',
                'tiers': [[self.actor.email]],
            },
        )
        tiers = roster.tiers or [[]]
        members = {
            str(m).strip().lower()
            for tier in tiers
            for m in (tier if isinstance(tier, list) else [tier])
        }
        if self.actor.email.lower() not in members:
            tiers[0].append(self.actor.email)
            roster.tiers = tiers
            roster.save(update_fields=['tiers', 'updated_at'])

    def _pair(self):
        # Sanctioned durable shape: the assigned carrier blocks the parent via
        # create_blocked_item's owned-blocker graph. The parent's block is then
        # marked operator-owned (pre_block_state.had_active_blocker=False) — the
        # metadata the "Raise to Admin Ops" escalation capture writes — so
        # terminalizing the superseded carrier hits the PLS-89 hold instead of
        # plain auto-resume.
        from tasks.tests.base import create_blocked_item
        carrier = self.Item.objects.create(
            item_key='P10926-CARRIER', title='superseded escalation carrier',
            mode='task', project=self.project, status='in_progress',
            workflow_status='in_progress', workflow_name=self.workflow.slug,
            created_by_id=self.actor.id, created_by_email=self.actor.email,
            assignee_id=self.actor.id, assignee_email=self.actor.email,
            assignment_group='test-fixtures',
        )
        parent = create_blocked_item(
            item_key='P10926-PARENT', title='operator-blocked parent',
            mode='task', project=self.project, pre_block_status='in_progress',
            workflow_name=self.workflow.slug, blocker=carrier,
            created_by_id=self.actor.id, created_by_email=self.actor.email,
            assignee_id=self.actor.id, assignee_email=self.actor.email,
            assignment_group='test-fixtures',
        )
        # create_blocked_item enters blocked via a bare save, which does not
        # capture pre-block state (that is transition-only). Write the
        # operator-escalation capture shape explicitly: PLS-89 arms on
        # had_active_blocker=False (operator-owned block).
        meta = dict(parent.metadata or {})
        meta['pre_block_state'] = {
            'status': 'in_progress',
            'workflow_status': 'in_progress',
            'assignee_id': self.actor.id,
            'assignee_email': self.actor.email,
            'assignment_group': 'test-fixtures',
            'had_active_blocker': False,
            'blocked_by_email': self.actor.email,
        }
        parent.metadata = meta
        parent.save(update_fields=['metadata', 'updated_at'])
        parent.refresh_from_db()
        return carrier, parent

    def _close(self, carrier, screen_data):
        from tasks.services.workflow_service import WorkflowService
        WorkflowService.execute_transition(
            item=carrier, new_status='done', user=self.actor,
            screen_data=screen_data, transition_id=str(self.done.id),
        )

    def test_bare_close_rejects_and_drops_any_stamp(self):
        from tasks.services.workflow_service import WorkflowService, WorkflowValidationError
        carrier, parent = self._pair()
        with self.assertRaises(WorkflowValidationError) as raised:
            self._close(carrier, {
                'escalation_satisfaction': {
                    'schema': 'not-the-schema',
                    'trail_pointer': 'too-short',
                    'acceptance_check': 'too-short',
                    'superseded': False,
                },
            })
        self.assertIn(
            raised.exception.code,
            ('escalation_satisfaction_invalid', 'dependency_release_rejected'),
        )
        carrier.refresh_from_db()
        self.assertNotIn('escalation_satisfaction', carrier.metadata or {})
        parent.refresh_from_db()
        self.assertEqual(parent.status, 'blocked')
        with self.assertRaises(WorkflowValidationError) as raised:
            self._close(carrier, {})
        self.assertEqual(raised.exception.code, 'dependency_release_rejected')
        carrier.refresh_from_db()
        self.assertNotIn('escalation_satisfaction', carrier.metadata or {})

    def test_rejected_release_clears_a_valid_stamp(self):
        from tasks.services.workflow_service import WorkflowService, WorkflowValidationError
        carrier, _parent = self._pair()
        with mock.patch.object(
            WorkflowService, '_restore_blocked_item', return_value=False,
        ), self.assertRaises(WorkflowValidationError) as raised:
            self._close(carrier, {'escalation_satisfaction': _evidence()})
        self.assertEqual(raised.exception.code, 'dependency_release_rejected')
        carrier.refresh_from_db()
        self.assertNotIn('escalation_satisfaction', carrier.metadata or {})

    def test_rejected_release_keeps_a_preexisting_stamp(self):
        from tasks.services.workflow_service import WorkflowService, WorkflowValidationError
        carrier, _parent = self._pair()
        prior = _evidence()
        prior['trail_pointer'] = 'PLAT-10275:prior-valid-trail'
        meta = dict(carrier.metadata or {})
        meta['escalation_satisfaction'] = prior
        carrier.metadata = meta
        carrier.save(update_fields=['metadata', 'updated_at'])
        fresh = dict(_evidence())
        fresh['trail_pointer'] = 'PLAT-10275:this-call-stamp'
        with mock.patch.object(
            WorkflowService, '_restore_blocked_item', return_value=False,
        ), self.assertRaises(WorkflowValidationError):
            self._close(carrier, {'escalation_satisfaction': fresh})
        carrier.refresh_from_db()
        self.assertEqual(
            (carrier.metadata or {}).get('escalation_satisfaction', {}).get('trail_pointer'),
            'PLAT-10275:prior-valid-trail',
        )

    def test_typed_evidence_releases_the_manual_block(self):
        carrier, parent = self._pair()
        self._close(carrier, {'escalation_satisfaction': _evidence()})
        carrier.refresh_from_db()
        parent.refresh_from_db()
        self.assertEqual(carrier.status, 'done')
        self.assertNotEqual(parent.status, 'blocked')
        self.assertEqual(
            carrier.metadata['escalation_satisfaction']['trail_pointer'],
            _evidence()['trail_pointer'],
        )
