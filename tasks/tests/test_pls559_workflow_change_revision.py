"""PLS-559 regression: typed workflow-change revision and apply recovery.

Production-order fixture mapping:
  proposed metadata(set) -> Request Revision/proposed -> top-level MCP revise
  (append + fresh seal) -> approval activity -> top-level MCP apply.

The fixture spans the actual proposal/revision and executor entry points rather
than repeatedly calling the digest helper, because the regression lived in the
state hand-off between those callers.
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import patch

from asgiref.sync import async_to_sync
from django.apps import apps as django_apps
from django.db import connection
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from tasks.models import (
    Comment,
    Item,
    ItemActivity,
    Project,
    RoutingTeam,
    Status,
    Workflow,
    WorkflowStatus,
    TeamRoster,
    WorkflowTransition,
)
from tasks.serializers import ItemUpdateSerializer
from tasks.services.workflow_service import WorkflowService, WorkflowValidationError
from tasks.typed_change_integrity import (
    extract_typed_envelope,
    format_seal,
    ops_digest,
)
from tasks.views import TransitionExecuteView


REPO_ROOT = Path(__file__).resolve().parents[2]
MCP_DIR = REPO_ROOT / 'mcp-server'
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))
MODULE_PATH = MCP_DIR / 'pulse_server.py'
SPEC = importlib.util.spec_from_file_location('pulse_server_pls559', MODULE_PATH)
pulse_server = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = pulse_server
SPEC.loader.exec_module(pulse_server)


OLD_ARTICLES = [
    {'type': 'inline', 'title': 'Merge guide', 'content': 'keep one'},
    {'type': 'inline', 'title': 'Wiki close gate', 'content': 'keep two'},
    {'type': 'inline', 'title': 'Merge discipline', 'content': 'keep three'},
]
OLD_ENV = {
    'version': 1,
    'operations': [{
        'op': 'set_knowledge_article',
        'workflow': 'autonomous-bug',
        'transition': 'Merge & Close',
        'content': 'new Origin guidance',
    }],
    'rationale': 'initial replacement proposal',
}
NEW_ENV = {
    'version': 1,
    'operations': [{
        'op': 'append_knowledge_article',
        'workflow': 'autonomous-bug',
        'transition': 'Merge & Close',
        'title': 'Origin-only merge',
        'content': 'new Origin guidance',
    }],
    'rationale': 'preserve existing articles',
}


def _description(envelope):
    return f"""## Workflow Change Request

**Target Workflow**: `autonomous-bug`
**Spec format**: TYPED (op-typed contract — tier from op types)

### Reason
Origin alignment

### Impact
Merge agents

### Change Specification
```json
{json.dumps(envelope, indent=2)}
```

### Rollback
Restore the exact prior list.
"""


def _workflow_detail():
    return {
        'id': 'target-workflow',
        'slug': 'autonomous-bug',
        'statuses': [],
        'transitions': [{
            'id': 'merge-close-transition',
            'name': 'Merge & Close',
            'knowledge_articles': list(OLD_ARTICLES),
        }],
    }


class _RevisionUser:
    id = 101
    pk = 101
    email = 'proposer@shizuha.com'
    is_authenticated = True
    is_staff = True
    is_superuser = True


class TypedWorkflowChangeRevisionTests(TestCase):
    def setUp(self):
        # PLS-737: the capacity classifier raises CapacityClassificationUnavailable
        # when a Status row is absent rather than silently assuming a ready seat.
        # Seed the workflow-change statuses so the transition path classifies them
        # as assigned-ready (category 'todo' → 'ready'), matching the pre-PLS-737
        # fallback this fixture was written against.
        Status.objects.update_or_create(
            slug='proposed', defaults={'name': 'Proposed', 'category': 'todo'},
        )
        Status.objects.update_or_create(
            slug='under_review', defaults={'name': 'Under Review', 'category': 'todo'},
        )
        self.item = Item.objects.create(
            mode='task',
            title='[Workflow Change] revise merge guidance',
            description=_description(OLD_ENV),
            workflow_name='workflow-change',
            status='proposed',
            workflow_status='Proposed',
            tags='no-pr,workflow-change,target:autonomous-bug,tier:routine,typed',
            metadata={'typed_change_spec': OLD_ENV, 'preserve_me': 'yes'},
            created_by_id=101,
            created_by_email='proposer@shizuha.com',
            assignee_id=101,
            assignee_email='proposer@shizuha.com',
            assignment_group='engineering',
        )

    def _payload(self):
        self.item.refresh_from_db()
        return {
            'id': self.item.id,
            'item_key': self.item.item_key,
            'workflow_name': self.item.workflow_name,
            'status': self.item.status,
            'workflow_status': self.item.workflow_status,
            'tags': self.item.tags,
            'metadata': self.item.metadata,
            'description': self.item.description,
            'created_by_email': self.item.created_by_email,
            'assignee_email': self.item.assignee_email,
        }

    def test_exact_org_admin_ops_backup_atomically_revises_and_audits(self):
        project = Project.objects.create(
            name='PLAT6037', key='P6037', org_id=1, access_level='open',
        )
        # The seeded test DB already materializes the engineering routing
        # aggregate. Remove that synthetic aggregate so this fixture exercises
        # the legacy exact-org TeamRoster source used by the requested seam.
        RoutingTeam.objects.filter(
            organization_id=1, key='engineering',
        ).delete()
        engineering_roster, _ = TeamRoster.objects.get_or_create(
            team_name='engineering', organization_slug='shizuha', organization_id=1,
            defaults={'tiers': []},
        )
        engineering_roster.tiers = [['proposer@shizuha.com']]
        engineering_roster.updated_by = 'test:plat6037'
        engineering_roster.save(update_fields=['tiers', 'updated_by', 'updated_at'])
        # Build a valid project-bound CR from the outset. The one-use proof is
        # the same explicit boundary production writers use for an assignment
        # whose owner is not resolved from the task team's routing roster.
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('pulse.assignment_org_proof', %s, true)",
                [f'1|{project.pk}|101|proposer@shizuha.com|engineering'],
            )
        self.item = Item.objects.create(
            mode='task',
            title='[Workflow Change] delegated revision',
            description=_description(OLD_ENV),
            project=project,
            item_key=project.get_next_item_key(),
            workflow_name='workflow-change',
            status='proposed',
            workflow_status='Proposed',
            tags='no-pr,workflow-change,target:autonomous-bug,tier:routine,typed',
            metadata={'typed_change_spec': OLD_ENV, 'preserve_me': 'yes'},
            created_by_id=101,
            created_by_email='creator@shizuha.com',
            assignee_id=101,
            assignee_email='proposer@shizuha.com',
            assignment_group='engineering',
        )
        backup_email = 'backup@shizuha.com'
        roster, _ = TeamRoster.objects.get_or_create(
            team_name='admin-ops', organization_slug='shizuha', organization_id=1,
            defaults={'tiers': []},
        )
        roster.tiers = [[backup_email]]
        roster.updated_by = 'test:plat6037'
        roster.save(update_fields=['tiers', 'updated_by', 'updated_at'])

        def revise_api(method, path, data=None, **_kwargs):
            if method == 'GET':
                return self._payload()
            if method == 'POST' and path.endswith('/execute-transition/'):
                self.item.status = 'under_review'
                self.item.workflow_status = 'Under Review'
                self.item.save(update_fields=['status', 'workflow_status', 'updated_at'])
                return {'assignee_email': 'reviewer@shizuha.com'}
            if method == 'POST' and path.endswith('/assign/'):
                return {'assignee_email': 'governance@shizuha.com'}
            raise AssertionError((method, path, data))

        with (
            patch.object(pulse_server, 'api_request', side_effect=revise_api),
            patch.object(pulse_server, '_get_my_user_id', return_value=(303, backup_email)),
            patch.object(pulse_server, '_fetch_workflow_by_slug', return_value=_workflow_detail()),
        ):
            result = async_to_sync(pulse_server.revise_workflow_change)(
                self.item.id, json.dumps(NEW_ENV),
            )

        self.assertIn('Revised and re-submitted', result)
        self.item.refresh_from_db()
        self.assertEqual(self.item.metadata['typed_change_spec'], NEW_ENV)
        seal = self.item.comments.get()
        self.assertEqual(seal.author_email, backup_email)
        self.assertIn('Delegated Admin Ops execution (PLAT-6037)', seal.content)
        self.assertIn(ops_digest(NEW_ENV['operations']), seal.content)

    def test_real_sequence_revises_authority_reseals_and_applies_append(self):
        """Two top-level cycles prove the exact production caller sequence."""
        applied_patches = []

        def revise_api(method, path, data=None, **_kwargs):
            if method == 'GET':
                return self._payload()
            if method == 'POST' and path.endswith('/execute-transition/'):
                self.item.refresh_from_db()
                # Production exposes the review status only after the revised
                # envelope, projection, and seal have committed together.
                seals = list(self.item.comments.all())
                self.assertEqual(len(seals), 1)
                self.assertIn(ops_digest(NEW_ENV['operations']), seals[0].content)
                self.item.status = 'under_review'
                self.item.workflow_status = 'Under Review'
                self.item.save(update_fields=['status', 'workflow_status', 'updated_at'])
                return {'assignee_email': 'reviewer@shizuha.com'}
            if method == 'POST' and path.endswith('/assign/'):
                return {'assignee_email': 'governance@shizuha.com'}
            raise AssertionError((method, path, data))

        with (
            patch.object(pulse_server, 'api_request', side_effect=revise_api),
            patch.object(pulse_server, '_get_my_user_id', return_value=(101, 'proposer@shizuha.com')),
            patch.object(pulse_server, '_fetch_workflow_by_slug', return_value=_workflow_detail()),
        ):
            revised = async_to_sync(pulse_server.revise_workflow_change)(
                self.item.id,
                json.dumps(NEW_ENV),
            )

        self.assertIn('Revised and re-submitted', revised)
        self.item.refresh_from_db()
        self.assertEqual(self.item.metadata['typed_change_spec'], NEW_ENV)
        self.assertEqual(self.item.metadata['preserve_me'], 'yes')
        self.assertEqual(extract_typed_envelope(self.item.description), NEW_ENV)
        self.assertIn('tier:routine', self.item.tags.split(','))
        self.assertEqual(self.item.status, 'under_review')
        seal_comment = self.item.comments.get()
        self.assertIn(ops_digest(NEW_ENV['operations']), seal_comment.content)

        # Fresh approval occurs after the fresh seal. The executor must consume
        # the same NEW_ENV and append to, not replace, the three prior articles.
        self.item.status = 'approved'
        self.item.workflow_status = 'Approved'
        self.item.save(update_fields=['status', 'workflow_status', 'updated_at'])
        ItemActivity.objects.create(
            item=self.item,
            action='status_changed',
            field_name='Status',
            old_value='under_review',
            new_value='approved',
            user_id=202,
            user_email='reviewer@shizuha.com',
        )

        def apply_api(method, path, data=None, **_kwargs):
            if method == 'GET' and '/activity/' in path:
                return [{
                    'action': activity.action,
                    'new_value': activity.new_value,
                    'created_at': activity.created_at.isoformat(),
                } for activity in self.item.activities.all()]
            if method == 'GET' and path.startswith('/comments/'):
                return [{
                    'content': comment.content,
                    'created_at': comment.created_at.isoformat(),
                } for comment in self.item.comments.all()]
            if method == 'GET':
                return self._payload()
            if method == 'PATCH':
                applied_patches.append((path, data))
                return {'ok': True}
            raise AssertionError((method, path, data))

        with (
            patch.object(pulse_server, 'api_request', side_effect=apply_api),
            patch.object(pulse_server, '_fetch_workflow_by_slug', return_value=_workflow_detail()),
        ):
            applied = pulse_server.admin_apply_change_request(self.item.id)

        self.assertIn('append_knowledge_article: OK', applied)
        self.assertIn('All 1 operation(s) applied', applied)
        self.assertEqual(len(applied_patches), 1)
        sent_articles = applied_patches[0][1]['knowledge_articles']
        self.assertEqual(sent_articles[:3], OLD_ARTICLES)
        self.assertEqual(sent_articles[3]['title'], 'Origin-only merge')

    def test_submit_response_failure_reports_durable_sealed_review_state(self):
        """A post-transition failure cannot expose an unsealed review state."""
        post_seen = False

        def failing_submit_api(method, path, data=None, **_kwargs):
            nonlocal post_seen
            if method == 'GET':
                return self._payload()
            if method == 'POST' and path.endswith('/execute-transition/'):
                post_seen = True
                self.item.refresh_from_db()
                seals = list(self.item.comments.all())
                self.assertEqual(len(seals), 1)
                self.assertIn(ops_digest(NEW_ENV['operations']), seals[0].content)

                request = APIRequestFactory().post(
                    f'/items/{self.item.pk}/execute-transition/',
                    data,
                    format='json',
                )
                force_authenticate(request, user=_RevisionUser())
                real_comment_create = Comment.objects.create

                def fail_transition_comment(*args, **kwargs):
                    if kwargs.get('content') == data['comment']:
                        raise RuntimeError('injected comment persistence failure')
                    return real_comment_create(*args, **kwargs)

                # Exercise the real production boundary: WorkflowService commits
                # the row-locked transition before TransitionExecuteView persists
                # the optional comment.  The failure is intentionally after the
                # durable status write, not a hand-simulated status mutation.
                with patch(
                    'tasks.views.Comment.objects.create',
                    side_effect=fail_transition_comment,
                ):
                    with self.assertRaisesRegex(
                        RuntimeError,
                        'injected comment persistence failure',
                    ):
                        TransitionExecuteView.as_view()(request, item_id=self.item.pk)
                return {'error': 'comment persistence failed after status commit'}
            raise AssertionError((method, path, data))

        with (
            patch.object(pulse_server, 'api_request', side_effect=failing_submit_api),
            patch.object(pulse_server, '_get_my_user_id', return_value=(101, 'proposer@shizuha.com')),
            patch.object(pulse_server, '_fetch_workflow_by_slug', return_value=_workflow_detail()),
        ):
            outcome = async_to_sync(pulse_server.revise_workflow_change)(
                self.item.id,
                json.dumps(NEW_ENV),
            )

        self.assertTrue(post_seen)
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, 'under_review')
        seal = self.item.comments.get()
        self.assertIn(ops_digest(NEW_ENV['operations']), seal.content)
        self.assertIn('durable state reached under_review', outcome)
        self.assertIn('Do not retry', outcome)

    def test_generic_patch_cannot_diverge_projection_or_authority(self):
        changed_description = _description(NEW_ENV)
        serializer = ItemUpdateSerializer(
            self.item,
            data={'description': changed_description},
            partial=True,
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn('pulse_revise_workflow_change', str(serializer.errors))

        serializer = ItemUpdateSerializer(
            self.item,
            data={'metadata': {'typed_change_spec': NEW_ENV}},
            partial=True,
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn('pulse_revise_workflow_change', str(serializer.errors))

    def test_revision_rechecks_editor_from_locked_item_after_reassignment(self):
        """An editor authorized by the GET snapshot may lose ownership before lock."""
        self.item.created_by_email = 'creator@shizuha.com'
        self.item.assignee_email = 'former-assignee@shizuha.com'
        self.item.save(update_fields=['created_by_email', 'assignee_email', 'updated_at'])

        def stale_snapshot_api(method, path, data=None, **_kwargs):
            if method != 'GET':
                raise AssertionError((method, path, data))
            snapshot = self._payload()
            Item.objects.filter(pk=self.item.pk).update(
                assignee_email='new-assignee@shizuha.com'
            )
            return snapshot

        with (
            patch.object(pulse_server, 'api_request', side_effect=stale_snapshot_api),
            patch.object(
                pulse_server,
                '_get_my_user_id',
                return_value=(303, 'former-assignee@shizuha.com'),
            ),
            patch.object(
                pulse_server,
                '_fetch_workflow_by_slug',
                return_value=_workflow_detail(),
            ),
        ):
            outcome = async_to_sync(pulse_server.revise_workflow_change)(
                self.item.id,
                json.dumps(NEW_ENV),
            )

        self.assertIn('revision REJECTED', outcome)
        self.assertIn('current assignee', outcome)
        self.item.refresh_from_db()
        self.assertEqual(self.item.assignee_email, 'new-assignee@shizuha.com')
        self.assertEqual(self.item.metadata['typed_change_spec'], OLD_ENV)
        self.assertEqual(extract_typed_envelope(self.item.description), OLD_ENV)

    def test_apply_refuses_missing_authoritative_metadata_without_description_fallback(self):
        payload = self._payload()
        payload['status'] = 'approved'
        payload['workflow_status'] = 'Approved'
        payload['metadata'] = {}
        with patch.object(pulse_server, 'api_request', return_value=payload):
            outcome = pulse_server.admin_apply_change_request(self.item.id)
        self.assertIn('no authoritative metadata.typed_change_spec', outcome)
        self.assertIn('never parsed for execution', outcome)

    def test_revise_runs_from_real_async_mcp_boundary_without_sync_only_operation(self):
        """PLAT-6050: the live async MCP caller must not raise SynchronousOnlyOperation.

        FastMCP executes sync tools INLINE in the async event loop (no worker
        thread), so a tool that touches the Django ORM directly fails with
        "You cannot call this from an async context". This regression drives the
        tool through the same async dispatch boundary the production MCP server
        uses (the coroutine is awaited from an event loop), and asserts the ORM
        write inside the tool completes — proving the ORM block is bridged to a
        worker thread via sync_to_async rather than running in the loop.
        """
        def revise_api(method, path, data=None, **_kwargs):
            if method == 'GET':
                return self._payload()
            if method == 'POST' and path.endswith('/execute-transition/'):
                self.item.refresh_from_db()
                self.item.status = 'under_review'
                self.item.workflow_status = 'Under Review'
                self.item.save(update_fields=['status', 'workflow_status', 'updated_at'])
                return {'assignee_email': 'reviewer@shizuha.com'}
            if method == 'POST' and path.endswith('/assign/'):
                return {'assignee_email': 'governance@shizuha.com'}
            raise AssertionError((method, path, data))

        with (
            patch.object(pulse_server, 'api_request', side_effect=revise_api),
            patch.object(pulse_server, '_get_my_user_id', return_value=(101, 'proposer@shizuha.com')),
            patch.object(pulse_server, '_fetch_workflow_by_slug', return_value=_workflow_detail()),
        ):
            # async_to_sync awaits the coroutine from a real event loop — the
            # same dispatch shape as FastMCP's async call_tool -> tool.run ->
            # call_fn_with_arg_validation path. Before the PLAT-6050 fix this
            # raised SynchronousOnlyOperation from the inline ORM block.
            revised = async_to_sync(pulse_server.revise_workflow_change)(
                self.item.id,
                json.dumps(NEW_ENV),
            )

        self.assertIn('Revised and re-submitted', revised)
        self.item.refresh_from_db()
        self.assertEqual(self.item.metadata['typed_change_spec'], NEW_ENV)
        self.assertEqual(self.item.status, 'under_review')
        seal = self.item.comments.get()
        self.assertIn(ops_digest(NEW_ENV['operations']), seal.content)


class WorkflowChangeRecoveryMigrationTests(TestCase):
    def setUp(self):
        self.proposed, _ = Status.objects.get_or_create(
            slug='proposed', defaults={'name': 'Proposed', 'category': 'todo'}
        )
        self.apply_failed, _ = Status.objects.get_or_create(
            slug='apply_failed', defaults={'name': 'Apply Failed', 'category': 'in_progress'}
        )
        self.applying, _ = Status.objects.get_or_create(
            slug='applying', defaults={'name': 'Applying', 'category': 'in_progress'}
        )
        self.workflow, _ = Workflow.objects.get_or_create(
            slug='workflow-change',
            defaults={
                'name': 'Workflow Change',
                'initial_status': self.proposed,
                'is_system': True,
            },
        )
        for position, status in enumerate((self.proposed, self.apply_failed, self.applying)):
            WorkflowStatus.objects.get_or_create(
                workflow=self.workflow,
                status=status,
                defaults={'position': position},
            )
        self.retry, _ = WorkflowTransition.objects.get_or_create(
            workflow=self.workflow,
            name='Retry Apply',
            from_status='apply_failed',
            to_status='applying',
            defaults={'validators': []},
        )
        migration = importlib.import_module(
            'tasks.migrations.0160_pls559_workflow_change_revision'
        )
        migration.add_revision_recovery(django_apps, None)
        self.retry.refresh_from_db()

    def test_recovery_transition_and_retry_digest_gate_are_seeded(self):
        recovery = WorkflowTransition.objects.get(
            workflow=self.workflow,
            name='Revise After Apply Failure',
            from_status='apply_failed',
            to_status='proposed',
        )
        self.assertTrue(any(
            post_function.get('target') == 'reporter'
            for post_function in recovery.post_functions
        ))
        self.assertIn(
            'typed_spec_matches_trusted_seal',
            [validator.get('type') for validator in self.retry.validators],
        )

    def test_retry_rejects_stale_payload_and_accepts_matching_payload(self):
        item = Item.objects.create(
            mode='task',
            title='failed typed CR',
            description=_description(NEW_ENV),
            workflow_name='workflow-change',
            status='apply_failed',
            workflow_status='Apply Failed',
            metadata={'typed_change_spec': NEW_ENV},
            created_by_id=101,
            created_by_email='proposer@shizuha.com',
        )
        Comment.objects.create(
            item=item,
            author_id=101,
            author_email='proposer@shizuha.com',
            content=format_seal(OLD_ENV['operations'], 'routine'),
        )
        ItemActivity.objects.create(
            item=item,
            action='status_changed',
            field_name='Status',
            old_value='under_review',
            new_value='approved',
            user_id=202,
            user_email='reviewer@shizuha.com',
        )

        with self.assertRaises(WorkflowValidationError) as raised:
            WorkflowService._run_validators(self.retry, item)
        self.assertIn('Revise After Apply Failure', str(raised.exception))

        item.metadata = {'typed_change_spec': OLD_ENV}
        item.save(update_fields=['metadata', 'updated_at'])
        WorkflowService._run_validators(self.retry, item)
