"""
PLS-89 / PLAT-4215 blocked-task invariants.

PLS-89: an operator/manual escalation block (e.g. "Raise to Admin Ops") must
NOT be auto-resumed by the blocked-item sweep just because the task carries a
stale, already-terminal `is_blocked_by` link.

Root cause: sweep_resolved_blocked_items / _restore_blocked_item resumed any
blocked task whose linked blockers were all terminal — vacuously true when the
only link points at an already-terminal task (PLAT-690 → deferred HIVE-103) —
undoing the escalation every beat. Fix: record `had_active_blocker` at block
time; skip auto-resume when it's False.
"""
import importlib
import uuid
from unittest import skipUnless
from unittest.mock import patch

from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.test import TestCase, TransactionTestCase

from tasks.services import escalation_routing
from tasks.models import (
    Comment,
    Item,
    ItemLink,
    Project,
    Status,
    TeamRoster,
    Workflow,
    WorkflowStatus,
    WorkflowTransition,
)
from tasks.services.workflow_service import WorkflowService, WorkflowValidationError
from .base import create_blocked_item


class EscalationBlockNoAutoResumeTests(TestCase):
    def _item(self, title, status='in_progress', metadata=None):
        kwargs = {
            'title': title,
            'mode': 'task',
            'workflow_name': 'simple',
            'status': status,
            'metadata': metadata or {},
        }
        if status == 'blocked':
            return create_blocked_item(**kwargs)
        return Item.objects.create(**kwargs)

    def test_capture_records_false_when_only_blocker_is_terminal(self):
        blocker = self._item('blk-terminal', status='deferred')  # terminal
        item = self._item('victim')
        ItemLink.objects.create(source_item=item, target_item=blocker, link_type='is_blocked_by', created_by_id=1)
        WorkflowService._capture_pre_block_state(
            item, 'in_progress', 'in_progress', None, '', 'admin-ops', 0)
        self.assertIs(item.metadata['pre_block_state']['had_active_blocker'], False)

    def test_capture_records_true_when_blocker_active(self):
        blocker = self._item('blk-active', status='open')  # non-terminal
        item = self._item('victim2')
        ItemLink.objects.create(source_item=item, target_item=blocker, link_type='is_blocked_by', created_by_id=1)
        WorkflowService._capture_pre_block_state(
            item, 'in_progress', 'in_progress', None, '', 'engineering', 0)
        self.assertIs(item.metadata['pre_block_state']['had_active_blocker'], True)

    def test_restore_skips_escalation_block(self):
        # PLAT-690 scenario: escalation-blocked (had_active_blocker=False) +
        # stale terminal is_blocked_by link. PLAT-4215 additionally requires a
        # live owned blocker, so retain one while proving the legacy false flag
        # still prevents an accidental resume.
        blocker = self._item('hive103', status='deferred')
        item = self._item('plat690', status='blocked', metadata={
            'pre_block_state': {'status': 'in_progress', 'had_active_blocker': False},
        })
        ItemLink.objects.create(source_item=item, target_item=blocker, link_type='is_blocked_by', created_by_id=1)
        result = WorkflowService._restore_blocked_item(item)
        self.assertFalse(result)
        item.refresh_from_db()
        self.assertEqual(item.status, 'blocked')  # NOT spuriously resumed

    def test_sweep_does_not_resume_escalation_block(self):
        from tasks.tasks import sweep_resolved_blocked_items
        blocker = self._item('blk-terminal2', status='deferred')
        item = self._item('esc-task', status='blocked', metadata={
            'pre_block_state': {'status': 'in_progress', 'had_active_blocker': False},
        })
        ItemLink.objects.create(source_item=item, target_item=blocker, link_type='is_blocked_by', created_by_id=1)
        res = sweep_resolved_blocked_items.apply(kwargs={'limit': 200}).get()
        item.refresh_from_db()
        self.assertEqual(item.status, 'blocked')
        self.assertNotIn(item.item_key or str(item.id), res.get('resumed', []))


class AssignedBlockerInvariantTests(TestCase):
    """PLAT-4215: escalation child/link/status is one owned transaction."""

    def setUp(self):
        user_model = get_user_model()
        self.actor = user_model.objects.create_user(
            username='plat4215-actor',
            email='plat4215-actor@example.com',
        )
        self.owner = user_model.objects.create_user(
            username='plat4215-owner',
            email='plat4215-owner@example.com',
        )
        self.project = Project.objects.create(
            name='PLAT-4215 Project',
            key='PBI',
            org_id=1,
        )
        for team_name, email in (
            ('engineering', self.actor.email),
            ('admin-ops', self.owner.email),
        ):
            TeamRoster.objects.update_or_create(
                organization_id=1,
                team_name=team_name,
                defaults={
                    'organization_slug': 'shizuha',
                    'tiers': [[email]],
                    'is_active': True,
                },
            )
        open_status, _ = Status.objects.get_or_create(
            slug='open', defaults={'name': 'Open', 'category': 'todo'},
        )
        blocked_status, _ = Status.objects.get_or_create(
            slug='blocked', defaults={'name': 'Blocked', 'category': 'in_progress'},
        )
        self.workflow = Workflow.objects.create(
            name='PLAT-4215 Test Workflow',
            slug='plat-4215-test-workflow',
            initial_status=open_status,
            is_active=True,
        )
        WorkflowStatus.objects.create(
            workflow=self.workflow, status=open_status, position=0,
        )
        WorkflowStatus.objects.create(
            workflow=self.workflow, status=blocked_status, position=1,
        )
        self.transition = WorkflowTransition.objects.create(
            workflow=self.workflow,
            name='Raise to Admin Ops',
            from_status='open',
            to_status='blocked',
            post_functions=[
                {
                    'type': 'create_blocker_task',
                    'team': 'admin-ops',
                    'workflow': 'simple',
                    'priority': 'urgent',
                    'source': 'PLAT-4215',
                },
                {'type': 'assign_team', 'value': 'admin-ops'},
            ],
        )

    def _parent(self, suffix='1', *, capability_gap=False):
        return Item.objects.create(
            project=self.project,
            item_key=f'PBI-{suffix}',
            mode='task',
            title=(
                'Add org member failed with admin_invite_member 403'
                if capability_gap else 'Approve paid vendor plan upgrade'
            ),
            description=(
                'Agent lacks permission and needs JIT elevation.'
                if capability_gap else 'Material spend approval for an external vendor plan.'
            ),
            workflow_name=self.workflow.slug,
            status='open',
            workflow_status='open',
            assignment_group='engineering',
            assignee_id=self.actor.id,
            assignee_email=self.actor.email,
            created_by_id=self.actor.id,
            created_by_email=self.actor.email,
        )

    def test_escalation_creates_owned_child_and_link_before_blocking(self):
        parent = self._parent()

        with patch.object(
            WorkflowService,
            '_resolve_team_assignee',
            # Comment-only legacy input is a mixed/unknown HOLD.  The typed
            # plan preserves the parent's exact-org engineering owner for both
            # the configured blocker and parent rather than splitting the
            # blocker into the configured admin-ops lane.
            return_value=(self.actor.id, self.actor.email),
        ):
            result = WorkflowService.execute_transition(
                parent,
                'blocked',
                user=self.actor,
                screen_data={'comment': 'Material spend decision needed for Acme plan.'},
                transition_id=str(self.transition.id),
            )

        result.refresh_from_db()
        self.assertEqual(result.status, 'blocked')
        links = ItemLink.objects.filter(target_item=result, link_type='blocks')
        self.assertEqual(links.count(), 1)
        blocker = links.get().source_item
        self.assertEqual(blocker.assignee_id, self.actor.id)
        self.assertEqual(blocker.assignee_email, self.actor.email)
        self.assertEqual(blocker.assignment_group, 'engineering')
        self.assertEqual(result.assignment_group, blocker.assignment_group)
        self.assertEqual(blocker.metadata['auto_blocker_for'], 'PBI-1')
        self.assertTrue(WorkflowService._has_active_assigned_blocker(result))
        self.assertTrue(
            Comment.objects.filter(
                item=result,
                content__contains='PLAT-4215 escalation invariant',
            ).exists()
        )

    def test_unresolvable_registered_team_mints_group_owned_blocker(self):
        parent = self._parent('10')

        with patch.object(
            WorkflowService, '_resolve_team_assignee', return_value=(None, None),
        ):
            result = WorkflowService.execute_transition(
                parent,
                'blocked',
                user=self.actor,
                screen_data={'comment': 'Material spend decision needed.'},
                transition_id=str(self.transition.id),
            )

        result.refresh_from_db()
        self.assertEqual(result.status, 'blocked')
        blockers = ItemLink.objects.filter(
            target_item=result,
            link_type='blocks',
        ).select_related('source_item')
        self.assertEqual(blockers.count(), 1)
        blocker = blockers.get().source_item
        self.assertEqual(blocker.assignment_group, 'engineering')
        self.assertIsNone(blocker.assignee_id)
        self.assertFalse(blocker.assignee_email)
        self.assertTrue(blocker.metadata['escalation_group_owned_wait'])
        self.assertEqual(blocker.metadata['escalation_group_owned_org_id'], 1)
        # PLS-860 item 5: stage-1 (election empty) is mechanically distinct.
        self.assertEqual(blocker.metadata['escalation_group_owned_cause'], 'election_empty')
        # Missing typed evidence is deliberately not admitted to the external
        # wait state; it remains visible group-owned repair work.
        self.assertNotEqual(blocker.status, 'waiting_external')
        self.assertTrue(
            Comment.objects.filter(
                item=result,
                content__contains='exact-org group-owned',
            ).exists()
        )

    def test_write_refusal_after_election_parks_group_owned_fresh_path(self):
        """PLS-860 stage-2: elected seat refused by write guard → group-owned.

        Transaction boundary: inline inside execute_transition's atomic().
        Post-fix observable (item 4): transition succeeds, child group-owned
        with cause=assignee_write_refused, and a committed assignee-write-guard
        hold explains the refusal.
        """
        parent = self._parent('12')

        def refuse_write(item, uid, email, update_fields, **_kwargs):
            # Mirror the real PLS-454 refusal: record hold, unassign, return
            # (None, None). ``applied`` stays truthy (2-tuple); assignee_id is
            # what the stage-2 guard must read (item 12).
            WorkflowService._record_routability_hold(
                item,
                getattr(item, 'assignment_group', None) or 'engineering',
                ['seat_not_team_member'],
                source='assignee-write-guard',
                attempted_email=email,
                candidate_reasons=[(email, ['seat_not_team_member'])],
            )
            item.unassign(save=False, reason='routability_hold')
            item.assignee_email = ''
            for field in ('assignee_id', 'assignee_email'):
                if field not in update_fields:
                    update_fields.append(field)
            return None, None

        with patch.object(
            WorkflowService,
            '_resolve_team_assignee',
            return_value=(self.owner.id, self.owner.email),
        ), patch.object(
            WorkflowService,
            '_apply_resolved_assignee',
            side_effect=refuse_write,
        ):
            result = WorkflowService.execute_transition(
                parent,
                'blocked',
                user=self.actor,
                screen_data={'comment': 'Material spend decision needed.'},
                transition_id=str(self.transition.id),
            )

        result.refresh_from_db()
        self.assertEqual(result.status, 'blocked')
        blocker = ItemLink.objects.filter(
            target_item=result, link_type='blocks',
        ).select_related('source_item').get().source_item
        self.assertIsNone(blocker.assignee_id)
        self.assertFalse(blocker.assignee_email)
        self.assertTrue(blocker.metadata['escalation_group_owned_wait'])
        self.assertEqual(
            blocker.metadata['escalation_group_owned_cause'],
            'assignee_write_refused',
        )
        # Distinct from stage-1 election_empty (item 5).
        self.assertNotEqual(
            blocker.metadata['escalation_group_owned_cause'],
            'election_empty',
        )
        self.assertTrue(
            Comment.objects.filter(
                item=blocker,
                content__contains='assignee-write-guard',
            ).exists()
            or Comment.objects.filter(
                item=blocker,
                content__contains='Candidate verdicts',
            ).exists()
            or Comment.objects.filter(
                item=blocker,
                origin_event='resolver.routability_hold',
            ).exists(),
            msg='committed assignee-write-guard record must survive the success path',
        )

    def test_write_refusal_on_reuse_parks_group_owned(self):
        """PLS-860 stage-1 at both reuse sites: write refusal → group-owned.

        Exercises WS reuse branch (intent match) which shares the same
        _apply_or_park_group_owned helper as ER:_reconcile_reused_blocker_owner.
        """
        parent = self._parent('13')
        # First mint succeeds with a concrete seat so a reuse child exists.
        with patch.object(
            WorkflowService,
            '_resolve_team_assignee',
            return_value=(self.actor.id, self.actor.email),
        ):
            first = WorkflowService.execute_transition(
                parent,
                'blocked',
                user=self.actor,
                screen_data={'comment': 'First escalation.'},
                transition_id=str(self.transition.id),
            )
        first.refresh_from_db()
        blocker = ItemLink.objects.filter(
            target_item=first, link_type='blocks',
        ).select_related('source_item').get().source_item
        self.assertEqual(blocker.assignee_id, self.actor.id)

        def refuse_write(item, uid, email, update_fields, **_kwargs):
            WorkflowService._record_routability_hold(
                item,
                getattr(item, 'assignment_group', None) or 'engineering',
                ['seat_not_team_member'],
                source='assignee-write-guard',
                attempted_email=email,
                candidate_reasons=[(email, ['seat_not_team_member'])],
            )
            item.unassign(save=False, reason='routability_hold')
            item.assignee_email = ''
            for field in ('assignee_id', 'assignee_email'):
                if field not in update_fields:
                    update_fields.append(field)
            return None, None

        # Reuse path: call the prepare helper directly (same code as a
        # blocked-status re-escalation) so the fixture names the WS reuse
        # branch without depending on from_status availability.
        with patch.object(
            WorkflowService,
            '_resolve_team_assignee',
            return_value=(self.owner.id, self.owner.email),
        ), patch.object(
            WorkflowService,
            '_apply_resolved_assignee',
            side_effect=refuse_write,
        ):
            reused = WorkflowService._prepare_assigned_blocker(
                self.transition,
                first,
                user=self.actor,
                screen_data={'comment': 'Retry escalation.'},
            )

        reused.refresh_from_db()
        # Intent-stable reuse: same child, now group-owned.
        self.assertEqual(reused.pk, blocker.pk)
        self.assertIsNone(reused.assignee_id)
        self.assertTrue(reused.metadata['escalation_group_owned_wait'])
        self.assertEqual(
            reused.metadata['escalation_group_owned_cause'],
            'assignee_write_refused',
        )

    def test_not_applied_alone_must_not_disable_stage2_guard(self):
        """PLS-860 item 12: both returns of _apply_resolved_assignee are 2-tuples.

        A refusal that returns (None, None) is truthy as `applied`; only the
        assignee_id clear triggers the park. Pin that a truthy applied with a
        cleared assignee still parks group-owned (not a silent unassigned child).
        """
        parent = self._parent('14')

        def refuse_but_truthy_tuple(item, uid, email, update_fields, **_kwargs):
            item.unassign(save=False, reason='routability_hold')
            item.assignee_email = ''
            for field in ('assignee_id', 'assignee_email'):
                if field not in update_fields:
                    update_fields.append(field)
            # Deliberately return a truthy 2-tuple — the real function always does.
            return None, None

        with patch.object(
            WorkflowService,
            '_resolve_team_assignee',
            return_value=(self.owner.id, self.owner.email),
        ), patch.object(
            WorkflowService,
            '_apply_resolved_assignee',
            side_effect=refuse_but_truthy_tuple,
        ):
            result = WorkflowService.execute_transition(
                parent,
                'blocked',
                user=self.actor,
                screen_data={'comment': 'Material spend.'},
                transition_id=str(self.transition.id),
            )

        blocker = ItemLink.objects.filter(
            target_item=result, link_type='blocks',
        ).select_related('source_item').get().source_item
        self.assertIsNone(blocker.assignee_id)
        self.assertTrue(
            blocker.metadata.get('escalation_group_owned_wait'),
            msg='truthy applied+(None,None) must still park group-owned via assignee_id',
        )
        self.assertEqual(
            blocker.metadata.get('escalation_group_owned_cause'),
            'assignee_write_refused',
        )

    def test_missing_exact_org_team_fails_with_diagnostic(self):
        parent = self._parent('11')
        TeamRoster.objects.filter(
            organization_id=1,
            team_name='admin-ops',
        ).delete()
        WorkflowService._invalidate_team_roster_caches()

        with patch.object(
            WorkflowService, '_resolve_team_assignee', return_value=(None, None),
        ), self.assertRaises(WorkflowValidationError) as exc:
            WorkflowService._prepare_assigned_blocker(
                self.transition,
                parent,
                user=self.actor,
                screen_data={'comment': 'Material spend decision needed.'},
            )

        self.assertEqual(exc.exception.code, 'BLOCKER_TEAM_UNREGISTERED')
        self.assertIn('organization_id=1', str(exc.exception))
        self.assertIn('team=`admin-ops`', str(exc.exception))

    def test_capability_gap_reroute_does_not_mint_orphan_blocker(self):
        parent = self._parent('20', capability_gap=True)
        # The synthetic catalog reroutes this task to DevOps and the patched
        # resolver below selects ``self.owner`` for that destination. Put that
        # selected owner in the organization-local Team before exercising the
        # context write, matching a real reviewed transfer.
        TeamRoster.objects.update_or_create(
            organization_id=1,
            team_name='devops',
            defaults={
                'organization_slug': 'shizuha',
                'tiers': [[self.owner.email]],
                'is_active': True,
            },
        )
        evidence = {
            'schema': escalation_routing.SCHEMA,
            'request_id': str(uuid.uuid4()),
            'expected_generation': 0,
            'dependency': {
                'evidence_class': 'internal_capability_gap',
                'requested_kind': 'technical_action',
                'requested_subject': 'Restore internal org-member provisioning capability',
                'holder_type': 'service_team',
                'holder_ref': 'devops',
                'acceptance_check': 'The internal admin invitation succeeds.',
                'source_refs': [{'kind': 'task', 'id': parent.item_key}],
            },
            'technical_action': None,
        }

        def apply_assignee(item, uid, email, update_fields, **_kwargs):
            item.assignee_id = uid
            item.assignee_email = email
            update_fields.extend(
                field for field in ('assignee_id', 'assignee_email')
                if field not in update_fields
            )
            return uid, email

        with patch.object(
            WorkflowService,
            '_resolve_team_assignee',
            return_value=(self.owner.id, self.owner.email),
        ), patch.object(
            WorkflowService,
            '_apply_resolved_assignee',
            side_effect=apply_assignee,
        ), patch.object(
            escalation_routing,
            '_resolve_catalog',
            return_value=('devops', 1),
        ):
            result = WorkflowService.execute_transition(
                parent,
                'blocked',
                user=self.actor,
                screen_data={
                    'comment': 'admin_invite_member returned 403 permission denied; need JIT elevation',
                    'escalation_evidence': evidence,
                },
                transition_id=str(self.transition.id),
            )

        result.refresh_from_db()
        self.assertEqual(result.status, 'open')
        self.assertEqual(result.assignment_group, 'devops')
        self.assertFalse(ItemLink.objects.filter(target_item=result, link_type='blocks').exists())
        self.assertFalse(Item.objects.filter(metadata__auto_blocker_for='PBI-20').exists())

    def test_direct_model_save_cannot_enter_blocked_naked(self):
        parent = self._parent('30')
        parent.status = 'blocked'
        parent.workflow_status = 'blocked'

        with self.assertRaises(ValidationError):
            parent.save(update_fields=['status', 'workflow_status'])

        parent.refresh_from_db()
        self.assertEqual(parent.status, 'open')

    def test_active_but_unowned_link_is_not_a_valid_blocker(self):
        parent = self._parent('40')
        blocker = Item.objects.create(
            project=self.project,
            item_key='PBI-41',
            mode='task',
            title='Unowned dependency',
            workflow_name='simple',
            status='pending',
            created_by_id=self.actor.id,
            created_by_email=self.actor.email,
        )
        link = ItemLink(
            source_item=blocker,
            target_item=parent,
            link_type='blocks',
            created_by_id=self.actor.id,
            created_by_email=self.actor.email,
        )
        link._skip_auto_block = True
        link.save()

        self.assertFalse(WorkflowService._has_active_assigned_blocker(parent))
        parent.status = 'blocked'
        parent.workflow_status = 'blocked'
        with self.assertRaises(ValidationError):
            parent.save(update_fields=['status', 'workflow_status'])

    def test_active_team_owned_link_is_a_valid_blocker(self):
        parent = self._parent('401')
        blocker = Item.objects.create(
            project=self.project,
            item_key='PBI-402',
            mode='task',
            title='Team-owned dependency',
            workflow_name='simple',
            status='pending',
            assignment_group='admin-ops',
            created_by_id=self.actor.id,
            created_by_email=self.actor.email,
        )
        link = ItemLink(
            source_item=blocker,
            target_item=parent,
            link_type='blocks',
            created_by_id=self.actor.id,
            created_by_email=self.actor.email,
        )
        link._skip_auto_block = True
        link.save()

        self.assertTrue(WorkflowService._has_active_assigned_blocker(parent))
        parent.status = 'blocked'
        parent.workflow_status = 'blocked'
        parent.save(update_fields=['status', 'workflow_status'])
        parent.refresh_from_db()
        self.assertEqual(parent.status, 'blocked')

    def test_assigned_alert_or_action_is_not_a_valid_blocker_task(self):
        for index, mode in enumerate(('alert', 'action'), start=1):
            with self.subTest(mode=mode):
                parent = self._parent(f'4{index}')
                blocker = Item.objects.create(
                    project=self.project,
                    item_key=f'PBI-4{index}1',
                    mode=mode,
                    title=f'Assigned {mode} is not durable blocker work',
                    workflow_name='simple',
                    status='open',
                    assignee_id=self.owner.id,
                    assignee_email=self.owner.email,
                )
                link = ItemLink(
                    source_item=blocker,
                    target_item=parent,
                    link_type='blocks',
                    created_by_id=self.actor.id,
                    created_by_email=self.actor.email,
                )
                link._skip_auto_block = True
                link.save()

                self.assertFalse(WorkflowService._has_active_assigned_blocker(parent))
                parent.status = 'blocked'
                parent.workflow_status = 'blocked'
                with self.assertRaises(ValidationError):
                    parent.save(update_fields=['status', 'workflow_status'])

    def test_direct_create_cannot_start_blocked(self):
        with self.assertRaises(ValidationError):
            Item.objects.create(
                project=self.project,
                item_key='PBI-50',
                mode='task',
                title='Initial naked block must fail at ORM boundary',
                workflow_name=self.workflow.slug,
                status='blocked',
                workflow_status='blocked',
                assignee_id=self.actor.id,
                assignee_email=self.actor.email,
            )

    def test_seed_rewrites_obsolete_manual_blocker_article(self):
        self.transition.knowledge_articles = [{
            'type': 'inline',
            'title': 'Capability-vs-decision challenge',
            'content': (
                'After firing this transition: call '
                'mcp__shizuha-pulse__pulse_create_blocker_task('
                'parent_id=this_task_id, team_slug="admin-ops", title=..., '
                'description=...) so the owner has a durable task.'
            ),
        }]
        self.transition.save(update_fields=['knowledge_articles'])

        migration = importlib.import_module(
            'tasks.migrations.0100_plat4215_blocked_owned_blocker'
        )
        migration.seed_assigned_blocker_post_functions(django_apps, None)
        self.transition.refresh_from_db()
        articles = self.transition.knowledge_articles
        combined = '\n'.join(str(article.get('content') or '') for article in articles)
        self.assertNotIn('call mcp__shizuha-pulse__pulse_create_blocker_task', combined)
        self.assertIn('transactionally creates, assigns, and links the blocker child', combined)
        self.assertIn('Do not call `pulse_create_blocker_task`', combined)

    def test_reverse_restores_manual_guidance_with_post_function(self):
        migration = importlib.import_module(
            'tasks.migrations.0100_plat4215_blocked_owned_blocker'
        )
        manual = migration._manual_blocker_guidance('admin-ops')
        self.transition.description = manual
        self.transition.knowledge_articles = [{
            'type': 'inline',
            'title': 'Capability-vs-decision challenge',
            'content': manual,
        }]
        # Exercise the migration's own seed rather than the fixture's equivalent
        # post-function so reverse can prove it removes exactly what forward adds.
        self.transition.post_functions = [
            {'type': 'assign_team', 'value': 'admin-ops'},
        ]
        self.transition.save(update_fields=[
            'description', 'knowledge_articles', 'post_functions',
        ])

        migration.seed_assigned_blocker_post_functions(django_apps, None)
        self.transition.refresh_from_db()
        self.assertIn(
            'transactionally creates, assigns, and links the blocker child',
            self.transition.description,
        )

        migration.unseed_assigned_blocker_post_functions(django_apps, None)
        self.transition.refresh_from_db()
        combined = '\n'.join(
            str(article.get('content') or '')
            for article in self.transition.knowledge_articles
        )
        self.assertIn('pulse_create_blocker_task', self.transition.description)
        self.assertIn('team_slug="admin-ops"', self.transition.description)
        self.assertIn('pulse_create_blocker_task', combined)
        self.assertNotIn('transactionally creates, assigns, and links', combined)
        self.assertFalse(any(
            article.get('title') == 'Automatic assigned blocker child (PLAT-4215)'
            for article in self.transition.knowledge_articles
        ))
        self.assertFalse(any(
            pf.get('type') == 'create_blocker_task' and pf.get('source') == 'PLAT-4215'
            for pf in self.transition.post_functions
        ))

    def test_seeded_escalations_declare_transactional_blocker_child(self):
        expected_teams = {
            'Raise to Admin Ops': 'admin-ops',
            'Escalate to Leads': 'ceo-decisions',
            'Escalate to Human Ops': 'human-ops',
        }
        seeded = WorkflowTransition.objects.filter(
            to_status='blocked',
            name__in=expected_teams,
        )
        self.assertTrue(seeded.exists())
        for transition in seeded:
            blockers = [
                pf
                for pf in (transition.post_functions or [])
                if isinstance(pf, dict) and pf.get('type') == 'create_blocker_task'
            ]
            self.assertEqual(
                [pf.get('team') for pf in blockers],
                [expected_teams[transition.name]],
                transition.name,
            )


@skipUnless(connection.vendor == 'postgresql', 'PLAT-4215 constraint is PostgreSQL-only')
class BlockedInvariantDatabaseTests(TransactionTestCase):
    """The deferred trigger closes QuerySet/raw-SQL bypasses below the ORM."""

    reset_sequences = True

    def test_terminal_transition_and_last_dependent_resume_commit_atomically(self):
        """Production autocommit cannot observe terminal blocker + blocked parent."""
        user_model = get_user_model()
        actor = user_model.objects.create_user(
            username='plat4215-autocommit',
            email='plat4215-autocommit@example.com',
        )
        open_status, _ = Status.objects.get_or_create(
            slug='open', defaults={'name': 'Open', 'category': 'todo'},
        )
        done_status, _ = Status.objects.get_or_create(
            slug='done', defaults={'name': 'Done', 'category': 'done'},
        )
        workflow = Workflow.objects.create(
            name='PLAT-4215 Autocommit Workflow',
            slug='plat-4215-autocommit-workflow',
            initial_status=open_status,
            is_active=True,
        )
        WorkflowStatus.objects.create(
            workflow=workflow, status=open_status, position=0,
        )
        WorkflowStatus.objects.create(
            workflow=workflow, status=done_status, position=1,
        )
        finish = WorkflowTransition.objects.create(
            workflow=workflow,
            name='Complete',
            from_status='open',
            to_status='done',
        )
        blocker = Item.objects.create(
            mode='task',
            title='Sole blocker completes in autocommit',
            workflow_name=workflow.slug,
            status='open',
            workflow_status='open',
            assignee_id=actor.id,
            assignee_email=actor.email,
        )
        # PLS-623: parent uses the seeded `simple` workflow, whose statuses are
        # todo/in_progress/done/blocked/deferred/scheduled — not `open`. When
        # reference data is present (the correct production state; also what the
        # PLS-623 flush-restore guarantees under --keepdb), _choose_resume_status
        # rejects an invalid pre_block status and falls through to in_progress.
        # Pin a valid simple-workflow status so the resume target is deterministic.
        parent = create_blocked_item(
            blocker=blocker,
            mode='task',
            title='Dependent resumes in same transaction',
            workflow_name='simple',
            metadata={
                'pre_block_state': {
                    'status': 'in_progress',
                    'workflow_status': 'in_progress',
                    'had_active_blocker': True,
                },
            },
        )

        self.assertFalse(connection.in_atomic_block)
        WorkflowService.execute_transition(
            blocker,
            'done',
            user=actor,
            transition_id=str(finish.id),
        )
        self.assertFalse(connection.in_atomic_block)

        blocker.refresh_from_db()
        parent.refresh_from_db()
        self.assertEqual(blocker.status, 'done')
        self.assertEqual(parent.status, 'in_progress')

    def test_queryset_update_cannot_create_naked_block(self):
        item = Item.objects.create(
            mode='task',
            title='Raw writer must fail closed',
            workflow_name='simple',
            status='pending',
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Item.objects.filter(pk=item.pk).update(status='blocked')

    def test_assigned_alert_cannot_satisfy_database_blocker_predicate(self):
        parent = Item.objects.create(
            mode='task', title='Parent requires blocker task',
            workflow_name='simple', status='pending',
        )
        alert = Item.objects.create(
            mode='alert', title='Assigned alert is not a blocker task',
            workflow_name='simple', status='open',
            assignee_id=4215, assignee_email='alert-owner@example.com',
        )
        link = ItemLink(
            source_item=alert, target_item=parent, link_type='blocks',
            created_by_id=1, created_by_email='test@pulse',
        )
        link._skip_auto_block = True
        link.save()

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Item.objects.filter(pk=parent.pk).update(status='blocked')

    def test_constraint_install_scans_preexisting_blocked_rows(self):
        migration = importlib.import_module(
            'tasks.migrations.0100_plat4215_blocked_owned_blocker'
        )

        class SchemaEditor:
            connection = connection

        with connection.cursor() as cursor:
            cursor.execute(migration.DROP_CONSTRAINTS_SQL)
        item = Item.objects.create(
            mode='task', title='Violation predates trigger installation',
            workflow_name='simple', status='pending',
        )
        Item.objects.filter(pk=item.pk).update(status='blocked')

        try:
            with self.assertRaises(IntegrityError):
                with transaction.atomic():
                    migration.install_database_constraints(django_apps, SchemaEditor())
        finally:
            Item.objects.filter(pk=item.pk).update(status='pending')
            migration.install_database_constraints(django_apps, SchemaEditor())
