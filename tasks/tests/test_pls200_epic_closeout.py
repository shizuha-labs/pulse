"""PLS-200 / PLS-533: epic close-out on last-child completion (event-driven).

PLS-200 shipped an event hook that, when a terminal transition left an epic with
no live children, emitted a comment + idempotency flag. PLS-533 corrected the
regression that a comment is NOT delivery: the hook now MATERIALIZES a first-class,
owner-routed, queue-visible `task.epic_closeout_assessment` task, narrows scope to
canonical `parent_of`/`child_of` + FK children only, dedups by (epic, scope
generation), supersedes stale assessments on scope change/reopen, and fails LOUD +
durable on delivery failure. It still never auto-resolves the epic.
"""
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.db import IntegrityError, close_old_connections
from django.test import TestCase, TransactionTestCase, skipUnlessDBFeature

from tasks.models import Comment, Item, ItemLink, Project, TeamRoster
from tasks.services.actionable_items import filter_active_queue_items
from tasks.services.workflow_service import WorkflowService

ASSESSMENT_TYPE = 'task.epic_closeout_assessment'


class EpicCloseoutTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name='Pulse closeout', key='P200', org_id=1)

    def _epic(self, **kw):
        d = dict(title='[EPIC] pls200 target', mode='task', item_type='epic',
                 status='backlog', workflow_name='simple', assignment_group='architecture',
                 project=self.project, item_key=None)
        d.update(kw)
        if d.get('item_key') is None and d.get('project') is not None:
            d['item_key'] = d['project'].get_next_item_key()
        return Item.objects.create(**d)

    def _child(self, epic=None, link_type=None, **kw):
        """Create a child. link_type=None -> FK child; else attach via that link type
        (child_of/parent_of = canonical scope; relates_to/blocks = adjacency)."""
        d = dict(title='pls200 child story', mode='task', item_type='task.generic',
                 status='done', workflow_name='simple', assignment_group='engineering',
                 project=self.project)
        d.update(kw)
        if d.get('item_key') is None and d.get('project') is not None:
            d['item_key'] = d['project'].get_next_item_key()
        if epic is not None and link_type is None:
            d['parent_item'] = epic
        kid = Item.objects.create(**d)
        if epic is not None and link_type is not None:
            if link_type == 'child_of':
                ItemLink.objects.create(source_item=kid, target_item=epic,
                                        link_type='child_of', created_by_id=0)
            elif link_type == 'parent_of':
                ItemLink.objects.create(source_item=epic, target_item=kid,
                                        link_type='parent_of', created_by_id=0)
            else:
                ItemLink.objects.create(source_item=kid, target_item=epic,
                                        link_type=link_type, created_by_id=0)
        return kid

    def _assessments(self, epic):
        return Item.objects.filter(item_type=ASSESSMENT_TYPE, closeout_parent=epic)

    # --- materialization ---------------------------------------------------

    def test_last_child_materializes_assessment(self):
        epic = self._epic()
        kid = self._child(epic)
        WorkflowService._request_epic_closeout_if_last_child(kid)
        epic.refresh_from_db()
        a = self._assessments(epic).get()
        self.assertEqual(a.mode, 'task')
        self.assertEqual(a.item_type, ASSESSMENT_TYPE)
        self.assertEqual(a.status, 'todo')            # non-backlog, non-terminal
        self.assertEqual(a.closeout_generation, 0)
        self.assertTrue(epic.metadata.get('closeout_requested'))
        self.assertEqual(epic.metadata.get('closeout_assessment_key'), a.item_key or str(a.pk))
        # epic never auto-resolved (owner confirms scope).
        self.assertIn(epic.status, ('backlog', 'todo'))
        self.assertIsNone(epic.resolution_id)

    def test_production_order_execute_transition_materializes(self):
        # FIN-4 regression: the REAL top-level transition entry point, not a helper.
        epic = self._epic()
        kid = self._child(epic, status='in_progress')
        WorkflowService.execute_transition(kid, 'done')
        kid.refresh_from_db()
        epic.refresh_from_db()
        self.assertEqual(kid.status, 'done')
        self.assertEqual(self._assessments(epic).count(), 1)
        self.assertTrue(epic.metadata.get('closeout_requested'))

    def test_assessment_is_visible_in_owner_actionable_queue(self):
        # The gap PLS-200 left: notification emitted != actionable assessment
        # materialized. The row MUST pass the actionable-queue filter.
        TeamRoster.objects.update_or_create(
            organization_slug='shizuha', team_name='architecture',
            defaults={'organization_id': 1, 'tiers': [['aoi@shizuha.com']]})
        epic = self._epic()
        kid = self._child(epic)
        with patch.object(WorkflowService, '_resolve_team_lead_email', return_value='aoi@shizuha.com'), \
             patch('tasks.user_service.user_service.get_user_by_email',
                   return_value={'id': 42, 'email': 'aoi@shizuha.com'}):
            WorkflowService._request_epic_closeout_if_last_child(kid)
        a = self._assessments(epic).get()
        self.assertEqual(a.assignee_email, 'aoi@shizuha.com')
        self.assertEqual(a.assignee_id, 42)
        visible = filter_active_queue_items(
            Item.objects.filter(assignee_email='aoi@shizuha.com', mode='task'))
        self.assertIn(a.pk, list(visible.values_list('pk', flat=True)))

    def test_live_sibling_blocks_closeout(self):
        epic = self._epic()
        kid = self._child(epic)
        self._child(epic, status='in_progress')
        WorkflowService._request_epic_closeout_if_last_child(kid)
        self.assertFalse(self._assessments(epic).exists())

    def test_canonical_link_child_counts(self):
        # roadmap epics carry slices as child_of/parent_of links, not FK children
        epic = self._epic()
        kid = self._child(epic, link_type='child_of')
        WorkflowService._request_epic_closeout_if_last_child(kid)
        self.assertEqual(self._assessments(epic).count(), 1)

    # --- canonical-scope invariant (I1) ------------------------------------

    def test_non_scope_link_does_not_trigger(self):
        # a `blocks`/`relates_to` link to an epic is adjacency, not a child
        epic = self._epic()
        for lt in ('blocks', 'relates_to'):
            kid = self._child(epic, link_type=lt)
            WorkflowService._request_epic_closeout_if_last_child(kid)
        self.assertFalse(self._assessments(epic).exists())

    def test_non_scope_link_does_not_block(self):
        # a LIVE relates_to neighbour must NOT block close-out of a delivered epic
        epic = self._epic()
        kid = self._child(epic)                              # FK child, done
        self._child(epic, link_type='relates_to', status='in_progress')  # live neighbour
        WorkflowService._request_epic_closeout_if_last_child(kid)
        self.assertEqual(self._assessments(epic).count(), 1)

    # --- dedup / idempotency (I4) ------------------------------------------

    def test_idempotent_retry_single_assessment(self):
        epic = self._epic()
        kid = self._child(epic)
        WorkflowService._request_epic_closeout_if_last_child(kid)
        WorkflowService._request_epic_closeout_if_last_child(kid)
        self.assertEqual(self._assessments(epic).count(), 1)

    def test_non_epic_parent_ignored(self):
        parent = Item.objects.create(title='ordinary parent task', mode='task',
                                     item_type='task.generic', status='open',
                                     workflow_name='simple', assignment_group='engineering',
                                     project=self.project, item_key=self.project.get_next_item_key())
        kid = self._child(parent)
        WorkflowService._request_epic_closeout_if_last_child(kid)
        self.assertFalse(self._assessments(parent).exists())

    def test_live_grooming_child_blocks_closeout(self):
        epic = self._epic()
        kid = self._child(epic)
        self._child(epic, title='Groom & decompose: [EPIC] pls200 target', status='open')
        WorkflowService._request_epic_closeout_if_last_child(kid)
        self.assertFalse(self._assessments(epic).exists())

    def test_groom_only_children_do_not_trigger(self):
        epic = self._epic()
        groom = self._child(epic, title='Groom & decompose: [EPIC] pls200 target', status='done')
        WorkflowService._request_epic_closeout_if_last_child(groom)
        self.assertFalse(self._assessments(epic).exists())

    def test_terminal_epic_skipped(self):
        epic = self._epic(status='done')
        kid = self._child(epic)
        WorkflowService._request_epic_closeout_if_last_child(kid)
        self.assertFalse(self._assessments(epic).exists())

    # --- owner routing -----------------------------------------------------

    def test_closeout_routes_assessment_to_team_lead(self):
        TeamRoster.objects.update_or_create(
            organization_slug='shizuha', team_name='architecture',
            defaults={'organization_id': 1, 'tiers': [['aoi@shizuha.com']]})
        epic = self._epic()
        kid = self._child(epic)
        with patch.object(WorkflowService, '_resolve_team_lead_email', return_value='aoi@shizuha.com'), \
             patch('tasks.user_service.user_service.get_user_by_email',
                   return_value={'id': 42, 'email': 'aoi@shizuha.com'}):
            WorkflowService._request_epic_closeout_if_last_child(kid)
        a = self._assessments(epic).get()
        self.assertEqual(a.assignee_email, 'aoi@shizuha.com')
        self.assertEqual(a.assignee_id, 42)
        self.assertEqual(a.assignment_group, 'architecture')
        # the EPIC itself is left as-is (not force-moved onto the lead's queue)
        epic.refresh_from_db()
        self.assertIsNone(epic.resolution_id)

    # --- manifest (I3) -----------------------------------------------------

    def test_manifest_enumerates_children_and_marks_missing_evidence(self):
        epic = self._epic()
        with_pr = self._child(epic, title='child with PR',
                              metadata={'pull_requests': [{'url': 'https://origin.shizuha.com/x/y/pulls/9'}]})
        without_pr = self._child(epic, title='child no PR')
        WorkflowService._request_epic_closeout_if_last_child(with_pr)
        a = self._assessments(epic).get()
        body = a.description
        self.assertIn(with_pr.item_key, body)
        self.assertIn(without_pr.item_key, body)
        self.assertIn('https://origin.shizuha.com/x/y/pulls/9', body)
        self.assertIn('⚠️ none linked', body)      # without_pr has no PR
        self.assertIn('none recorded', body)         # no verification urls

    # --- supersede on scope change / reopen (I5) ---------------------------

    def test_new_fk_child_bumps_generation_and_supersedes(self):
        epic = self._epic()
        kid = self._child(epic)
        WorkflowService._request_epic_closeout_if_last_child(kid)
        gen0 = self._assessments(epic).get()
        self.assertEqual(gen0.closeout_generation, 0)
        # a NEW canonical child arrives (via the real post_save signal path)
        self._child(epic, status='open')
        epic.refresh_from_db()
        gen0.refresh_from_db()
        self.assertEqual(epic.scope_generation, 1)
        self.assertNotIn('closeout_requested', epic.metadata or {})
        self.assertEqual(gen0.status, 'cancelled')       # stale assessment withdrawn
        self.assertIsNotNone(gen0.resolution_id)

    def test_reopen_supersedes_then_regenerates(self):
        epic = self._epic()
        kid = self._child(epic)
        WorkflowService._request_epic_closeout_if_last_child(kid)
        gen0 = self._assessments(epic).get()
        # scope change (reopen equivalent) supersedes gen0, bumps generation
        WorkflowService._clear_epic_closeout_requested(epic, reason='child reopened')
        epic.refresh_from_db()
        gen0.refresh_from_db()
        self.assertEqual(epic.scope_generation, 1)
        self.assertEqual(gen0.status, 'cancelled')
        # re-terminalization materializes a DISTINCT gen-1 assessment
        WorkflowService._request_epic_closeout_if_last_child(kid)
        gen1 = self._assessments(epic).exclude(pk=gen0.pk).get()
        self.assertEqual(gen1.closeout_generation, 1)
        self.assertEqual(gen1.status, 'todo')

    def test_non_scope_link_add_does_not_supersede(self):
        # adding a relates_to link to a delivered epic must not touch its assessment
        epic = self._epic()
        kid = self._child(epic)
        WorkflowService._request_epic_closeout_if_last_child(kid)
        gen0 = self._assessments(epic).get()
        neighbour = self._child(status='open')
        ItemLink.objects.create(source_item=neighbour, target_item=epic,
                                link_type='relates_to', created_by_id=0)
        epic.refresh_from_db()
        gen0.refresh_from_db()
        self.assertEqual(epic.scope_generation, 0)
        self.assertEqual(gen0.status, 'todo')
        self.assertTrue(epic.metadata.get('closeout_requested'))

    # --- fail-loud (I7) ----------------------------------------------------

    def test_delivery_failure_is_loud_and_durable(self):
        epic = self._epic()
        kid = self._child(epic)
        with patch.object(WorkflowService, '_materialize_closeout_assessment',
                          side_effect=RuntimeError('db exploded')):
            WorkflowService._request_epic_closeout_if_last_child(kid)
        epic.refresh_from_db()
        self.assertFalse(self._assessments(epic).exists())          # no half-created row
        self.assertNotIn('closeout_requested', epic.metadata or {})  # NOT marked done
        failed = (epic.metadata or {}).get('closeout_assessment_failed')
        self.assertTrue(failed)
        self.assertIn('db exploded', failed.get('error', ''))
        self.assertTrue(Comment.objects.filter(
            item=epic, content__contains='delivery FAILED').exists())

    def test_nondedup_integrity_error_fails_loud(self):
        # P2-1: the `except IntegrityError` branch exists to resolve the dedup
        # unique-race, but IntegrityError is broad — a NON-dedup integrity failure
        # (NOT NULL / FK / other constraint) inside materialization leaves NO row
        # for the dedup-key re-fetch. That must NOT be swallowed as a silent
        # `assessment is None -> return False`; it routes to the loud, durable
        # failure path (invariant 7), exactly like the generic-exception path.
        epic = self._epic()
        kid = self._child(epic)
        with patch.object(WorkflowService, '_materialize_closeout_assessment',
                          side_effect=IntegrityError('null value in column "assignee_id"')):
            WorkflowService._request_epic_closeout_if_last_child(kid)
        epic.refresh_from_db()
        self.assertFalse(self._assessments(epic).exists())           # no row materialized
        self.assertNotIn('closeout_requested', epic.metadata or {})  # NOT marked done
        failed = (epic.metadata or {}).get('closeout_assessment_failed')
        self.assertTrue(failed)                                      # durable fail signal
        self.assertIn('assignee_id', failed.get('error', ''))
        self.assertTrue(Comment.objects.filter(
            item=epic, content__contains='delivery FAILED').exists())  # loud 🔴 comment

    def test_execute_transition_assessment_visible_in_owner_queue(self):
        # P2-3 / FIN-4 end-to-end in ONE test: the last scoped child terminalizes
        # via the REAL execute_transition entry point AND the materialized
        # assessment is visible in the resolved owner's UNFILTERED actionable queue.
        TeamRoster.objects.update_or_create(
            organization_slug='shizuha', team_name='architecture',
            defaults={'organization_id': 1, 'tiers': [['aoi@shizuha.com']]})
        epic = self._epic()
        kid = self._child(epic, status='in_progress')
        with patch.object(WorkflowService, '_resolve_team_lead_email', return_value='aoi@shizuha.com'), \
             patch('tasks.user_service.user_service.get_user_by_email',
                   return_value={'id': 42, 'email': 'aoi@shizuha.com'}):
            WorkflowService.execute_transition(kid, 'done')
        kid.refresh_from_db()
        self.assertEqual(kid.status, 'done')
        a = self._assessments(epic).get()
        self.assertEqual(a.assignee_email, 'aoi@shizuha.com')
        visible = filter_active_queue_items(
            Item.objects.filter(assignee_email='aoi@shizuha.com', mode='task'))
        self.assertIn(a.pk, list(visible.values_list('pk', flat=True)))

    # --- backfill ----------------------------------------------------------

    def test_backfill_command_materializes_all_terminal_and_skips_live(self):
        delivered = self._epic(title='[EPIC] delivered')
        self._child(delivered, status='done')
        live = self._epic(title='[EPIC] still live')
        self._child(live, status='done')
        self._child(live, status='in_progress')

        out = StringIO()
        with patch.object(WorkflowService, '_resolve_team_lead_email', return_value=None):
            call_command('backfill_epic_closeouts', stdout=out)

        self.assertEqual(self._assessments(delivered).count(), 1)
        self.assertFalse(self._assessments(live).exists())
        self.assertIn('requested=1', out.getvalue())


class EpicCloseoutConcurrencyTests(TransactionTestCase):
    """P2-2: the dedup race belt (contract point 4) — two writers concurrently
    mint the same-generation assessment; the (closeout_parent, closeout_generation)
    partial-unique constraint lets exactly one win, and the loser's IntegrityError
    resolves to the winner's row rather than forking a duplicate or failing loud."""

    reset_sequences = True

    def setUp(self):
        self.project = Project.objects.create(name='Pulse closeout race', key='P2R', org_id=1)

    def _epic(self):
        return Item.objects.create(
            title='[EPIC] race', mode='task', item_type='epic', status='backlog',
            workflow_name='simple', assignment_group='architecture',
            project=self.project, item_key=self.project.get_next_item_key())

    def _child(self, epic):
        return Item.objects.create(
            title='race child', mode='task', item_type='task.generic', status='done',
            workflow_name='simple', assignment_group='engineering', project=self.project,
            parent_item=epic, item_key=self.project.get_next_item_key())

    @skipUnlessDBFeature('has_select_for_update')
    def test_concurrent_mint_dedups_to_single_assessment(self):
        import threading

        epic = self._epic()
        kid = self._child(epic)
        barrier = threading.Barrier(2)
        errors = []

        def mint():
            try:
                barrier.wait()
                WorkflowService._request_epic_closeout_if_last_child(kid)
            except Exception as exc:  # noqa: BLE001 — recorded, asserted below
                errors.append(exc)
            finally:
                close_old_connections()

        threads = [threading.Thread(target=mint), threading.Thread(target=mint)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        # Neither writer raises: the loser's IntegrityError is caught and resolved.
        self.assertEqual(errors, [])
        # Exactly one assessment — never two, never zero.
        self.assertEqual(
            Item.objects.filter(item_type=ASSESSMENT_TYPE, closeout_parent=epic).count(), 1)
        # The dedup-race resolution is NOT a delivery failure.
        epic.refresh_from_db()
        self.assertFalse((epic.metadata or {}).get('closeout_assessment_failed'))
