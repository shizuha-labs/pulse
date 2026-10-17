"""PLAT-9427: seat-accessible reclassification of a mis-filed acceptance_mode
on record-class (retro-finding) rows, plus the create-time filing-mode warning.

Datum (shion's live triage, retro week of 19 Sep): PLAT-9160 was the only
retro row filed with ``acceptance_mode=live`` (siblings ``code``); the field
is read-only post-create with no seat-accessible reclassification path, and
the waiver mint refuses seat-side on the lapsed delegation bearer — so a
correctly-triaged record row stranded behind an operator action for a
filing-time typo.

Contract locked here:
- the reclass is DOWNGRADE-ONLY (``live → code``); ``deployed`` is never
  rewritten and no upgrade path exists;
- it is RECORD-CLASS ONLY: the item must carry the ``retro-finding`` tag and
  have NO open linked PRs — the structural proof that the live standard is
  unsatisfiable by construction for this deliverable;
- it is PRE-SUBMIT ONLY (todo/open/in_progress/blocked); submitted rows close
  through the normal governed path;
- the actor must be the assignee, creator, or reporter, never automation;
- every refusal is a typed WorkflowValidationError and success mints an
  immutable mechanical receipt;
- create-time warning: a retro-finding row filed ``live`` gets a loud
  non-blocking warning receipt; ``code`` filings get none;
- the PLS-1249 read-only-on-update guard is untouched.
"""
from unittest import mock

from django.test import TestCase

from tasks.services.workflow_service import (
    WorkflowService,
    WorkflowValidationError,
)


class ReclassifyAcceptanceModeTests(TestCase):
    """PLAT-9427 — the narrow reclassification service contract."""

    def setUp(self):
        from tasks.models import Item, Project

        # Item.save() fences assignee org-membership against the admin API.
        # Run 7109: the unmocked lookup hit the CI blackhole (127.0.0.1:9),
        # failed closed, and raised ValidationError on create. Same stub
        # contract as test_assignment.py's AssignmentPermissionTests.setUp.
        admin_patcher = mock.patch('tasks.services.admin_service.admin_service')
        self.mock_admin = admin_patcher.start()
        self.addCleanup(admin_patcher.stop)
        self.mock_admin.get_organization.return_value = {
            'id': 1, 'slug': 'shizuha', 'name': 'Shizuha',
        }
        self.mock_admin.is_org_member.return_value = True
        self.mock_admin.check_org_membership.return_value = {
            'is_member': True, 'role': 'member',
        }
        self.mock_admin.get_user_organization_ids.return_value = [1]
        self.mock_admin.get_user_organizations.return_value = [{'id': 1}]
        # The no-user_id fence path intersects aliases over the org member
        # list — it must contain the fixture emails or the fence still fails
        # closed (get_org_members()->[] == "not a member").
        self.mock_admin.get_org_members.return_value = [
            {'user_email': 'kai@shizuha.com'},
            {'user_email': 'sora@shizuha.com'},
            {'user_email': 'shizuha@agents.shizuha.com'},
        ]

        self.project = Project.objects.create(
            name='Platform', key='PLAT', org_id=1,
            access_level='private', is_active=True,
        )
        self._next_item_number = 1

    class _Actor:
        def __init__(self, email):
            self.email = email

    def _item(self, *, acceptance_mode='live', tags='retro-finding',
              status='in_progress', prs='none', assignee_email=None,
              created_by_email='shizuha@agents.shizuha.com', **kw):
        from tasks.models import Item

        metadata = {'pull_requests': []}
        if prs == 'open':
            metadata = {'pull_requests': [{
                'url': 'https://origin.shizuha.com/shizuha-labs/x/pulls/2',
                'repo': 'shizuha-labs/x', 'number': 2, 'status': 'open',
                'head_sha': 'abc123open',
            }]}
        kw.setdefault('item_key', f'PLAT-{self._next_item_number}')
        self._next_item_number += 1
        return Item.objects.create(
            project=self.project,
            title='[Retro] mis-filed acceptance_mode strands record row',
            description='Retro finding; deliverable is a record.',
            tags=tags,
            status=status,
            workflow_name='simple',
            item_type='task',
            created_by_id=1,
            created_by_email=created_by_email,
            assignee_email=assignee_email,
            acceptance_mode=acceptance_mode,
            metadata=metadata,
            **kw,
        )

    def _reclass(self, item, actor_email='shizuha@agents.shizuha.com', **kw):
        return WorkflowService.reclassify_acceptance_mode(
            item, actor=self._Actor(actor_email), **kw)

    # ── happy path ──────────────────────────────────────────────────────────

    def test_assignee_reclassifies_live_retro_row_to_code_with_receipt(self):
        item = self._item(assignee_email='kai@shizuha.com')
        comment = self._reclass(item, 'kai@shizuha.com',
                                reason='retro deliverable is a record, not live code')
        item.refresh_from_db()
        self.assertEqual(item.acceptance_mode, 'code')
        self.assertEqual(item.metadata.get('acceptance_mode'), 'code')
        self.assertEqual(comment.origin_event,
                         'receipt.acceptance_mode_reclassification')
        payload = comment.origin_payload
        self.assertEqual(payload['from_mode'], 'live')
        self.assertEqual(payload['to_mode'], 'code')
        self.assertEqual(payload['actor_email'], 'kai@shizuha.com')

    def test_creator_may_reclass_own_row(self):
        item = self._item()
        comment = self._reclass(item, 'shizuha@agents.shizuha.com',
                                reason='filing typo at create')
        self.assertEqual(comment.origin_payload['from_mode'], 'live')

    # ── refusals: gate preservation ─────────────────────────────────────────

    def test_refuses_target_deployed(self):
        item = self._item()
        with self.assertRaises(WorkflowValidationError) as ctx:
            self._reclass(item, target_mode='deployed', reason='x')
        self.assertIn('downgrade-only', str(ctx.exception))

    def test_refuses_when_current_mode_is_not_live(self):
        for mode in ('code', 'deployed', None):
            item = self._item(acceptance_mode=mode)
            with self.assertRaises(WorkflowValidationError) as ctx:
                self._reclass(item, reason='x')
            self.assertIn('only to rows whose acceptance_mode is live',
                          str(ctx.exception))

    def test_refuses_when_row_has_open_linked_prs(self):
        item = self._item(prs='open')
        with self.assertRaises(WorkflowValidationError) as ctx:
            self._reclass(item, reason='x')
        self.assertIn('open linked PRs', str(ctx.exception))

    def test_refuses_when_not_record_class(self):
        item = self._item(tags='urgent')
        with self.assertRaises(WorkflowValidationError) as ctx:
            self._reclass(item, reason='x')
        self.assertIn('not record-class', str(ctx.exception))

    def test_refuses_past_pre_submit_window(self):
        from tasks.models import Workflow

        wf = Workflow.resolve('simple')
        # Item.save() heals statuses that the workflow does not define, so
        # intersect the probe list with the workflow's real statuses — the
        # contract is that every REAL past-window status refuses.
        valid = set(wf.workflow_statuses.values_list('status__slug', flat=True)
                    if hasattr(wf, 'workflow_statuses') else [])
        if not valid:
            from tasks.models import WorkflowStatus
            valid = set(WorkflowStatus.objects.filter(
                workflow=wf).values_list('status__slug', flat=True))
        probed = [st for st in ('in_review', 'awaiting_deploy', 'done',
                                'cancelled') if st in valid]
        self.assertTrue(probed,
                        'simple workflow exposes no past-window status to probe')
        for st in probed:
            item = self._item(status=st)
            with self.assertRaises(WorkflowValidationError) as ctx:
                self._reclass(item, reason='x')
            self.assertIn('past the', str(ctx.exception))

    # ── refusals: actor + input hygiene ─────────────────────────────────────

    def test_refuses_unauthorized_actor(self):
        item = self._item(assignee_email='kai@shizuha.com')
        with self.assertRaises(WorkflowValidationError) as ctx:
            self._reclass(item, 'sora@shizuha.com', reason='x')
        # Assert the service's canonical refusal message (workflow_service.py:
        # 'reclassification requires the item assignee or creator/reporter') —
        # the earlier draft asserted a different wording than the contract
        # string (run 7551).
        self.assertIn('requires the item assignee or creator/reporter', str(ctx.exception))

    def test_refuses_automation_actor(self):
        item = self._item()
        with self.assertRaises(WorkflowValidationError) as ctx:
            self._reclass(item, 'system@shizuha.com', reason='x')
        self.assertIn('automation cannot', str(ctx.exception))

    def test_refuses_empty_reason(self):
        item = self._item()
        with self.assertRaises(WorkflowValidationError) as ctx:
            self._reclass(item, reason='   ')
        self.assertIn('non-empty reason', str(ctx.exception))

    def test_refuses_unauthenticated_actor(self):
        item = self._item()
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService.reclassify_acceptance_mode(
                item, actor=self._Actor(''), reason='x')
        self.assertIn('authenticated actor email', str(ctx.exception))


class RetroCreateWarningTests(TestCase):
    """PLAT-9427 — create-time filing-mode warning (loud, non-blocking)."""

    def setUp(self):
        from tasks.models import Project

        self.project = Project.objects.create(
            name='Platform', key='PLAT', org_id=1,
            access_level='private', is_active=True,
        )

    def _create(self, acceptance_mode, tags='retro-finding'):
        from tasks.serializers import ItemSerializer

        class _Req:
            user = None

        serializer = ItemSerializer(
            data={
                'mode': 'task',
                'title': '[Retro] warning fixture',
                'description': 'd',
                'project': self.project.id,
                'workflow_name': 'simple',
                'tags': tags,
                'acceptance_mode': acceptance_mode,
                'assignment_group': 'engineering',
            },
            context={'request': _Req()},
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        return serializer.save()

    def test_retro_row_filed_live_gets_warning_receipt(self):
        from tasks.models import Comment

        item = self._create('live')
        self.assertTrue(Comment.objects.filter(
            item=item, origin_event='warning.retro_acceptance_mode_live',
        ).exists())

    def test_retro_row_filed_code_gets_no_warning(self):
        from tasks.models import Comment

        item = self._create('code')
        self.assertFalse(Comment.objects.filter(
            item=item, origin_event='warning.retro_acceptance_mode_live',
        ).exists())

    def test_non_retro_row_filed_live_gets_no_warning(self):
        from tasks.models import Comment

        item = self._create('live', tags='')
        # NOTE: the persisted acceptance_mode for non-retro rows is existing
        # create-time defaulting behavior (Item.save, service-affecting/user-
        # facing derivation) and is deliberately NOT asserted here — this
        # test's contract is the WARNING gate keyed on the record-class tag,
        # not the platform's defaulting outcome.
        self.assertFalse(Comment.objects.filter(
            item=item, origin_event='warning.retro_acceptance_mode_live',
        ).exists())

    def test_update_rejection_guard_untouched(self):
        """PLS-1249 stays loud: PATCHing acceptance_mode is a 400."""
        from tasks.models import Item
        from tasks.serializers import ItemSerializer

        item = Item.objects.create(
            project=self.project,
            title='[Retro] update guard fixture',
            description='d',
            tags='retro-finding',
            status='in_progress',
            workflow_name='simple',
            item_type='task',
            created_by_id=1,
            created_by_email='shizuha@agents.shizuha.com',
            acceptance_mode='live',
            metadata={'pull_requests': []},
        )
        serializer = ItemSerializer(item, data={'acceptance_mode': 'code'},
                                    partial=True)
        self.assertFalse(serializer.is_valid())
        self.assertIn('acceptance_mode', serializer.errors)
