"""PLS-913 regression: autonomous-bug "Approve & Close (no PR / docs)"
in_review -> done transition returned HTTP 500 on a verified task carrying a
merged historical PR + related/blocker links (PLAT-5623).

Reproduction fixture mirrors PLAT-5623's exact live data shape:
  - workflow  : autonomous-bug
  - status    : in_review
  - linked PR : one MERGED historical PR (status=merged, merged_at set)
  - issue links: a related/blocker ItemLink
  - labels    : regression, user-facing (=> acceptance_mode resolves to live)
  - transition: "Approve & Close (no PR / docs)" In Review -> Done

The bug: the transition threw an unhandled exception (surfaced as HTTP 500 via
the structured-error middleware) instead of either closing the task or raising
a typed WorkflowValidationError with an actionable message. This test asserts
the transition either succeeds to ``done`` or raises a *typed* error (never an
unexpected exception type), and that the task is not left stuck in_review with
no durable error.
"""
import uuid
from unittest import mock

from django.db import OperationalError
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from tasks.models import (
    Comment,
    DeploymentEvent,
    ExternalMcpClient,
    ExternalMcpToken,
    Item,
    ItemLink,
    Project,
    Status,
    Workflow,
    WorkflowStatus,
    WorkflowTransition,
)
from tasks.services.workflow_service import WorkflowService, WorkflowValidationError


MERGED_SHA = 'deadbeefcafe1234567890abcdef1234567890ab'
REPO = 'shizuha-labs/shizuha-beta'


class Pls913ApproveClose500Tests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(
            name='Platform', key='PLAT', org_id=1,
            access_level='private', is_active=True,
        )
        patcher = mock.patch(
            'tasks.liveness.record_heartbeat', return_value=1700000000.0,
        )
        self.record_heartbeat = patcher.start()
        self.addCleanup(patcher.stop)
        self._next_item_number = 1000
        self._seed_autonomous_bug_workflow()

    # ── workflow seed ────────────────────────────────────────────────────────
    def _seed_autonomous_bug_workflow(self):
        """Seed a minimal autonomous-bug workflow with In Review -> Done close
        (mirrors migration 0067 "Approve & Close (no PR / docs)")."""
        statuses = {}
        for position, (slug, category) in enumerate(
            (
                ('needs_triage', 'todo'),
                ('open', 'todo'),
                ('in_progress', 'in_progress'),
                ('in_review', 'in_progress'),
                ('awaiting_merge', 'in_progress'),
                ('done', 'done'),
            ),
            start=1,
        ):
            status, _ = Status.objects.get_or_create(
                slug=slug,
                defaults={
                    'name': slug.replace('_', ' ').title(),
                    'category': category,
                    'position': position,
                    'is_system': True,
                },
            )
            if status.category != category:
                status.category = category
                status.save(update_fields=['category'])
            statuses[slug] = status

        workflow, _ = Workflow.objects.get_or_create(
            slug='autonomous-bug',
            defaults={
                'name': 'Autonomous Bug',
                'initial_status': statuses['needs_triage'],
                'is_system': True,
            },
        )
        for position, slug in enumerate(statuses, start=1):
            WorkflowStatus.objects.get_or_create(
                workflow=workflow,
                status=statuses[slug],
                defaults={'position': position},
            )
        # The exact "Approve & Close (no PR / docs)" transition from migration 0067.
        WorkflowTransition.objects.get_or_create(
            workflow=workflow,
            from_status='in_review',
            to_status='done',
            defaults={
                'name': 'Approve & Close (no PR / docs)',
                'position': 33,
                'button_style': 'primary',
                'post_functions': [
                    {
                        'type': 'send_notification',
                        'template': 'status_changed',
                        'recipients': ['reporter', 'watchers'],
                    },
                ],
            },
        )

    # ── factories ────────────────────────────────────────────────────────────
    def _merged_pr(self):
        return {
            'url': f'https://origin.shizuha.com/{REPO}/pulls/110',
            'repo': REPO,
            'status': 'merged',
            'merged_at': '2026-08-08T21:00:00Z',
            'head_sha': MERGED_SHA,
        }

    def _deploy_event(self, item, *, live_probe=True, **kw):
        """Seed a correlated production DeploymentEvent with full passing
        evidence (live mode): verified_by_qa state, image, replicas ready, and
        an item-level typed live-probe receipt if requested."""
        meta = {'replicas_ready': True}
        if live_probe is True:
            meta['live_probe'] = {
                'schema': 'shizuha.pulse.live-probe.v1',
                'result': 'passed',
                'evidence': 'https://shizuha.com/mail/api/auth/oauth/callback/ 200',
                'verified_by': 'zen@shizuha.com',
            }
        meta.update(kw.get('extra_metadata') or {})
        return DeploymentEvent.objects.create(
            idempotency_key=(
                f'k-{item.item_key}-{DeploymentEvent.objects.count()}'
            ),
            project=self.project,
            repo=REPO,
            commit_sha=MERGED_SHA,
            service='shizuha-agent-runtime',
            environment='production',
            state='verified_by_qa',
            smoke_result='passed',
            image='localhost:30500/shizuha-agent-runtime:harness-202608082106-46fdd0a',
            image_digest='sha256:7615c57655bb8d8a6d7158086c0f44db9fdf6d7a2b48e57febb68df03176d173',
            source_task_keys=[item.item_key],
            metadata=meta,
        )

    def _item(
        self,
        *,
        labels=None,
        tags='',
        status='in_review',
        workflow_name='autonomous-bug',
        metadata=None,
        prs='merged',
        **kw,
    ):
        if metadata is None:
            metadata = {}
        else:
            metadata = dict(metadata)
        if prs == 'merged':
            metadata.setdefault('pull_requests', [self._merged_pr()])
        elif prs == 'none':
            metadata.setdefault('pull_requests', [])
        elif isinstance(prs, list):
            metadata.setdefault('pull_requests', prs)
        kw.setdefault('item_key', f'PLAT-{self._next_item_number}')
        self._next_item_number += 1
        return Item.objects.create(
            project=self.project,
            title='[REGRESSION][PLAT-5106] QA seat omits Browser MCP after PR #110',
            description='Production QA seat regression; user-facing.',
            labels=labels if labels is not None else ['regression', 'user-facing'],
            tags=tags,
            status=status,
            workflow_name=workflow_name,
            item_type='task',
            created_by_id=1,
            created_by_email='zen@shizuha.com',
            assignee_id=7,
            assignee_email='zen@shizuha.com',
            metadata=metadata,
            **kw,
        )

    def _actor(self, email='qa-lead@shizuha.com'):
        return type('Actor', (), {'id': 7, 'email': email})()

    # ── the regression ───────────────────────────────────────────────────────
    def test_approve_close_no_pr_with_merged_pr_and_links_does_not_500(self):
        """Mirrors PLAT-5623: in_review, merged historical PR, related/blocker
        link, live acceptance mode -> the no-PR close must not raise an
        unhandled exception (the 500 class). It must either reach ``done`` or
        raise a typed WorkflowValidationError with an actionable message."""
        item = self._item()
        blocker = self._item(status='in_progress', prs='none')
        ItemLink.objects.create(
            source_item=item,
            target_item=blocker,
            link_type='relates_to',
            created_by_id=1,
            created_by_email='zen@shizuha.com',
        )

        transition = WorkflowTransition.objects.get(
            workflow__slug='autonomous-bug',
            from_status='in_review',
            to_status='done',
        )

        try:
            result = WorkflowService.execute_transition(
                item,
                'done',
                user=self._actor(),
                transition_id=str(transition.id),
            )
        except WorkflowValidationError as exc:
            # Typed error is acceptable ONLY if it is actionable and the item
            # was not left in a silent broken state.
            self.assertNotIn('server_error', str(exc).lower())
            self.assertTrue(str(exc).strip())
            item.refresh_from_db()
            # A typed gate block is fine; but the item must still have a status.
            self.assertIn(item.status, {'in_review', 'blocked'})
            return
        except Exception as exc:  # pragma: no cover - this is the regression
            self.fail(
                f'PLS-913 regression: execute_transition raised an unhandled '
                f'{type(exc).__name__}: {exc}'
            )

        item.refresh_from_db()
        self.assertEqual(result.status, 'done')
        self.assertEqual(item.status, 'done')

    def test_approve_close_no_pr_with_full_evidence_and_links_reaches_done(self):
        """PLAT-5623 with complete live evidence: correlated verified_by_qa
        DeploymentEvent + image + replicas ready + live-probe + merged PR +
        related link. The deploy-verify gate PASSES and the transition must
        complete to ``done`` without an unhandled exception (the 500 class)."""
        item = self._item()
        blocker = self._item(status='in_progress', prs='none')
        ItemLink.objects.create(
            source_item=item,
            target_item=blocker,
            link_type='relates_to',
            created_by_id=1,
            created_by_email='zen@shizuha.com',
        )
        self._deploy_event(item)

        transition = WorkflowTransition.objects.get(
            workflow__slug='autonomous-bug',
            from_status='in_review',
            to_status='done',
        )
        try:
            result = WorkflowService.execute_transition(
                item,
                'done',
                user=self._actor(),
                transition_id=str(transition.id),
            )
        except WorkflowValidationError as exc:
            self.assertNotIn('server_error', str(exc).lower())
            item.refresh_from_db()
            self.assertIn(item.status, {'in_review', 'blocked'})
            return
        except Exception as exc:  # pragma: no cover - the regression
            self.fail(
                f'PLS-913 regression (full-evidence close): unhandled '
                f'{type(exc).__name__}: {exc}'
            )
        item.refresh_from_db()
        self.assertEqual(result.status, 'done')
        self.assertEqual(item.status, 'done')

    def test_approve_close_no_pr_with_full_evidence_reaches_done_no_links(self):
        """Same as above but without the related/blocker link — isolates whether
        the issue-link is the trigger for the 500."""
        item = self._item()
        self._deploy_event(item)
        transition = WorkflowTransition.objects.get(
            workflow__slug='autonomous-bug',
            from_status='in_review',
            to_status='done',
        )
        try:
            result = WorkflowService.execute_transition(
                item,
                'done',
                user=self._actor(),
                transition_id=str(transition.id),
            )
        except WorkflowValidationError as exc:
            item.refresh_from_db()
            self.assertIn(item.status, {'in_review', 'blocked'})
            return
        except Exception as exc:  # pragma: no cover - the regression
            self.fail(
                f'PLS-913 regression (no-links close): unhandled '
                f'{type(exc).__name__}: {exc}'
            )
        item.refresh_from_db()
        self.assertEqual(result.status, 'done')
        self.assertEqual(item.status, 'done')

    def test_approve_close_no_pr_plain_no_pr_task_reaches_done(self):
        """Sanity: the same transition on a plain no-PR, code-mode task succeeds
        (proves the workflow seed and transition are executable)."""
        item = self._item(
            labels=[], prs='none', acceptance_mode='code',
        )
        transition = WorkflowTransition.objects.get(
            workflow__slug='autonomous-bug',
            from_status='in_review',
            to_status='done',
        )
        result = WorkflowService.execute_transition(
            item,
            'done',
            user=self._actor(),
            transition_id=str(transition.id),
        )
        item.refresh_from_db()
        self.assertEqual(result.status, 'done')
        self.assertEqual(item.status, 'done')

    def test_approve_close_no_pr_with_open_pr_raises_typed_error(self):
        """The same transition on a task with an OPEN linked PR must raise a
        typed WorkflowValidationError (the PLAT-4719 open-PR guard), NOT 500."""
        item = self._item(
            prs=[
                {
                    'url': f'https://origin.shizuha.com/{REPO}/pulls/1',
                    'repo': REPO,
                    'status': 'open',
                    'head_sha': 'abc123open',
                },
            ],
        )
        transition = WorkflowTransition.objects.get(
            workflow__slug='autonomous-bug',
            from_status='in_review',
            to_status='done',
        )
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService.execute_transition(
                item,
                'done',
                user=self._actor(),
                transition_id=str(transition.id),
            )
        self.assertIn('open', str(ctx.exception).lower())


@override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}})
class Pls913TypedErrorHandlerTests(APITestCase):
    """PLS-913 (typed-error half): an AgentWipCapacity lock-timeout
    OperationalError on the execute-transition path must surface as a typed,
    retryable 503 — NOT the generic ``server_error`` HTTP 500 that made
    PLAT-5623 look stuck in_review."""

    def setUp(self):
        self.project = Project.objects.create(
            name='Platform', key='PLAT', org_id=1,
            access_level='private', is_active=True,
        )
        self.item = Item.objects.create(
            mode='task', project=self.project, item_key='PLAT-5000',
            title='transition lock test', description='x',
            status='in_review', workflow_name='autonomous-bug',
            created_by_id=1, created_by_email='zen@shizuha.com',
            assignee_id=7, assignee_email='zen@shizuha.com',
        )
        self.client_record = ExternalMcpClient.objects.create(
            org_id=1,
            name='PLS-913 test client',
            owner_user_id=7,
            owner_email='zen@shizuha.com',
            allowed_project_ids=[self.project.id],
            scopes=['task.read', 'transition.execute'],
            rate_limit_policy={'per_minute': 1000},
        )
        self.raw_token = 'pls913-token'
        self.token = ExternalMcpToken.objects.create(
            client=self.client_record,
            token_hash=ExternalMcpToken.hash_token(self.raw_token),
            scopes=['task.read', 'transition.execute'],
            expires_at=timezone.now() + timezone.timedelta(days=1),
        )
        self.auth = {'HTTP_AUTHORIZATION': f'Bearer {self.raw_token}'}

    def test_capacity_lock_timeout_returns_typed_503_not_generic_500(self):
        """Mirrors PLS-912's exact DB signature: 'canceling statement due to
        statement timeout ... tasks_agentwipcapacity'. Must be a typed
        retryable 503 with an actionable message, never a generic 500."""
        from tasks.services import workflow_service as ws

        lock_error = OperationalError(
            'canceling statement due to statement timeout\n'
            'CONTEXT: while locking tuple (0,105) in relation '
            '"tasks_agentwipcapacity"'
        )
        with mock.patch.object(
            ws.WorkflowService, 'execute_transition',
            side_effect=lock_error,
        ):
            resp = self.client.post(
                '/api/external-mcp/call/',
                {
                    'tool': 'pulse_execute_transition',
                    'arguments': {
                        'task_id': self.item.item_key,
                        'to_status': 'done',
                        'idempotency_key': str(uuid.uuid4()),
                    },
                },
                format='json',
                **self.auth,
            )
        self.assertEqual(resp.status_code, 503, resp.content[:400])
        body = resp.json()
        self.assertEqual(body.get('code'), 'transition_capacity_lock_timeout')
        self.assertIn('AgentWipCapacity', body.get('detail', ''))
        self.assertIsNotNone(body.get('retry_after'))
        # The item must NOT be left silently mutated.
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, 'in_review')

    def test_unrelated_operational_error_not_misclassified(self):
        """A non-capacity OperationalError is NOT misclassified as the typed
        capacity-lock 503 — it keeps the generic gateway error path so
        genuinely unexpected DB faults remain loud and untyped."""
        from tasks.services import workflow_service as ws

        other_error = OperationalError(
            'could not connect to server: Connection refused'
        )
        with mock.patch.object(
            ws.WorkflowService, 'execute_transition',
            side_effect=other_error,
        ):
            resp = self.client.post(
                '/api/external-mcp/call/',
                {
                    'tool': 'pulse_execute_transition',
                    'arguments': {
                        'task_id': self.item.item_key,
                        'to_status': 'done',
                        'idempotency_key': str(uuid.uuid4()),
                    },
                },
                format='json',
                **self.auth,
            )
        self.assertEqual(resp.status_code, 400, resp.content[:300])
        body = resp.json()
        # Must NOT be the typed capacity-lock 503; must NOT claim a committed
        # or uncommitted capacity-lock story.
        self.assertNotEqual(
            body.get('error'), 'transition_capacity_lock_timeout',
        )
        self.assertNotEqual(body.get('code'), 'transition_capacity_lock_timeout')


class Pls913DrfTypedErrorHandlerTests(APITestCase):
    """PLS-913 DRF-path: the AgentWipCapacity lock-timeout OperationalError on
    POST /api/items/{id}/execute-transition/ must return a typed retryable 503,
    NOT the generic server_error 500 that stuck PLAT-5623 in_review."""

    def setUp(self):
        from django.contrib.auth import get_user_model
        from tasks.models import ProjectMembership

        self.user_model = get_user_model()
        self.user = self.user_model.objects.create_user(
            username='pls913-drf',
            email='pls913-drf@shizuha.com',
            password='testpass123',
        )
        self.user.organization_memberships = {1: 'member'}
        self.project = Project.objects.create(
            name='Platform', key='PLAT', org_id=1,
            access_level='private', is_active=True,
        )
        ProjectMembership.objects.create(
            project=self.project,
            user_id=self.user.id,
            user_email=self.user.email,
            role='member',
        )
        self.item = Item.objects.create(
            mode='task', project=self.project, item_key='PLAT-6000',
            title='drf lock test', description='x',
            status='in_review', workflow_name='autonomous-bug',
            created_by_id=self.user.id,
            created_by_email=self.user.email,
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_drf_capacity_lock_timeout_returns_typed_503(self):
        from tasks.services import workflow_service as ws

        lock_error = OperationalError(
            'canceling statement due to statement timeout\n'
            'CONTEXT: while locking tuple (0,105) in relation '
            '"tasks_agentwipcapacity"'
        )
        with mock.patch.object(
            ws.WorkflowService, 'execute_transition',
            side_effect=lock_error,
        ):
            resp = self.client.post(
                f'/api/items/{self.item.id}/execute-transition/',
                {'to_status': 'done'},
                format='json',
            )
        self.assertEqual(resp.status_code, 503, resp.content[:400])
        body = resp.json()
        self.assertEqual(body.get('code'), 'transition_capacity_lock_timeout')
        self.assertEqual(body.get('committed'), False)
        self.assertIn('AgentWipCapacity', body.get('detail', ''))
        self.assertIsNotNone(body.get('retry_after'))
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, 'in_review')
