"""PLAT-7422: gate-layer defect fixes (completion invariant + deploy-verify matcher).

Two independent gate-layer defects, fixed and pinned here:

Defect 1 — completion invariant cited a terminal, unlinked task.
    ``pulse_complete_task`` (POST /api/items/{id}/complete/) wrote the terminal
    status with NO dependent reconciliation, so every ``blocked`` dependent whose
    last active blocker was the completing item stayed blocked beside a terminal
    blocker and the deferred PLAT-4215 constraint rejected the COMMIT. The
    middleware's static 409 detail cited "(PLAT-4215)" — the POLICY provenance —
    which five separate investigations misread as a blocker task citation
    (PLAT-7389 4x, PLAT-7406 1x, 2026-09-01/02). Fixes pinned here:
      - the complete endpoint reconciles dependents in-transaction (same
        fail-closed contract as execute_transition / PLS-953);
      - a dependent held only by active-but-UNOWNED blockers fails closed with a
        typed 409 naming the dependent and its unowned blocker(s);
      - the middleware 409 names the ACTUAL naked item(s) parsed from the
        trigger message and labels PLAT-4215 as the policy, not a citation.

Defect 2 — deploy-verify matcher refused well-formed production events.
    Ryo's four ingest-accepted production DeploymentEvents on PLAT-7267
    (merge-commit sha / PR head sha / metadata.merged_head_sha shapes, all
    repo=cortex, source_task_keys=[PLAT-7267], state=verified_by_qa) were all
    refused ``missing_correlated_deploy_event`` on 2026-09-01. The constant
    dimensions, not the SHA shapes, refused them: basename-vs-owner/repo repo
    identity, case-sensitive env/task-key candidate queries, and the model-
    default ``unknown`` smoke_result on verified_by_qa events. The four-event
    matrix is reproduced here as the regression fixture.
"""
from types import SimpleNamespace
from unittest import mock

from django.db import IntegrityError, transaction
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient, APITestCase

from tasks.middleware import StructuredApiExceptionMiddleware
from tasks.models import (
    DeploymentEvent,
    Item,
    ItemLink,
    Project,
    ProjectMembership,
)
from tasks.services.workflow_service import WorkflowService


MERGED_SHA = 'deadbeefcafe1234567890abcdef1234567890ab'
HEAD_SHA = 'feedface00001234567890abcdef1234567890ab'
CORTEX_REPO = 'shizuha-labs/cortex'


class DeployVerifyMatcherCorrelationTests(TestCase):
    """Defect 2: ryo's PLAT-7267 fixture matrix must correlate."""

    def setUp(self):
        self.project = Project.objects.create(
            name='Platform', key='PLAT', org_id=1,
            access_level='private', is_active=True,
        )
        self._next_item_number = 7400

    # ── factories ────────────────────────────────────────────────────────────

    def _item(self, *, repo=CORTEX_REPO, pr_number=158):
        item = Item.objects.create(
            mode='task', project=self.project,
            item_key=f'PLAT-{self._next_item_number}',
            title='cortex deploy close', description='x',
            status='awaiting_deploy', workflow_name='autonomous-bug',
            created_by_id=1, created_by_email='ryo@shizuha.com',
            metadata={'pull_requests': [{
                'url': f'https://origin.shizuha.com/{repo}/pulls/{pr_number}',
                'repo': repo,
                'number': pr_number,
                'status': 'merged',
                'merged_at': '2026-09-01T00:00:00Z',
                'head_sha': HEAD_SHA,
                'merge_commit_sha': MERGED_SHA,
            }]},
        )
        self._next_item_number += 1
        return item

    def _deploy_event(
        self, item, *, repo='cortex', environment='live', state='verified_by_qa',
        smoke_result='unknown', commit_sha=MERGED_SHA, source_task_keys=None,
        image='registry.shizuha.com/cortex:multi-arch',
    ):
        return DeploymentEvent.objects.create(
            idempotency_key=(
                f'k-{item.item_key}-{repo}-{environment}-{state}-'
                f'{commit_sha[:12]}-{DeploymentEvent.objects.count()}'
            ),
            project=self.project,
            repo=repo,
            commit_sha=commit_sha,
            service='cortex',
            environment=environment,
            state=state,
            smoke_result=smoke_result,
            image=image,
            source_task_keys=(
                source_task_keys if source_task_keys is not None
                else [item.item_key]
            ),
            metadata={'replicas_ready': True},
        )

    # ── repo identity ────────────────────────────────────────────────────────

    def test_ryo_fixture_basename_event_repo_correlates(self):
        """Event repo recorded as bare basename vs PR owner/repo — ryo shape 1."""
        item = self._item()
        self._deploy_event(item, repo='cortex')
        heads = WorkflowService._merged_pr_heads(item)
        self.assertTrue(heads)
        self.assertIsNotNone(
            WorkflowService._find_correlated_deploy_event(item, heads),
        )
        self.assertEqual(
            WorkflowService._collect_deploy_verify_missing(item, 'deployed'),
            [],
        )

    def test_ryo_fixture_exact_owner_repo_event_still_correlates(self):
        """Full owner/repo on both sides remains the primary match path."""
        item = self._item()
        self._deploy_event(item, repo=CORTEX_REPO)
        heads = WorkflowService._merged_pr_heads(item)
        self.assertIsNotNone(
            WorkflowService._find_correlated_deploy_event(item, heads),
        )

    def test_cross_org_slashed_repo_never_correlates(self):
        """Two slashed repos in different orgs stay rejected (Rei P1-4)."""
        item = self._item()
        self._deploy_event(item, repo='other-org/cortex')
        heads = WorkflowService._merged_pr_heads(item)
        self.assertIsNone(
            WorkflowService._find_correlated_deploy_event(item, heads),
        )
        self.assertIn(
            'missing_correlated_deploy_event',
            WorkflowService._collect_deploy_verify_missing(item, 'deployed'),
        )

    def test_basename_mismatch_never_correlates(self):
        """A basename that does not name the PR repo's basename is rejected."""
        item = self._item()
        self._deploy_event(item, repo='unrelated')
        heads = WorkflowService._merged_pr_heads(item)
        self.assertIsNone(
            WorkflowService._find_correlated_deploy_event(item, heads),
        )

    # ── env / task-key case drift ────────────────────────────────────────────

    def test_ryo_fixture_environment_case_drift_correlates(self):
        """Postgres ``environment__in`` is case-sensitive; 'Live' must match."""
        item = self._item()
        self._deploy_event(item, environment='Live')
        heads = WorkflowService._merged_pr_heads(item)
        self.assertIsNotNone(
            WorkflowService._find_correlated_deploy_event(item, heads),
        )

    def test_ryo_fixture_task_key_case_drift_correlates(self):
        """JSONB containment is case-sensitive; 'plat-7267' must match."""
        item = self._item()
        self._deploy_event(item, source_task_keys=[item.item_key.lower()])
        heads = WorkflowService._merged_pr_heads(item)
        self.assertIsNotNone(
            WorkflowService._find_correlated_deploy_event(item, heads),
        )

    # ── smoke dimension ──────────────────────────────────────────────────────

    def test_ryo_fixture_verified_by_qa_with_unknown_smoke_correlates(self):
        """Model-default smoke_result='unknown' must not void a QA verdict."""
        item = self._item()
        self._deploy_event(item, state='verified_by_qa', smoke_result='unknown')
        heads = WorkflowService._merged_pr_heads(item)
        self.assertIsNotNone(
            WorkflowService._find_correlated_deploy_event(item, heads),
        )
        self.assertEqual(
            WorkflowService._collect_deploy_verify_missing(item, 'deployed'),
            [],
        )

    def test_failed_smoke_still_refused(self):
        """An explicit failed smoke is a real failure signal — fail closed."""
        item = self._item()
        self._deploy_event(item, smoke_result='failed')
        heads = WorkflowService._merged_pr_heads(item)
        self.assertIsNone(
            WorkflowService._find_correlated_deploy_event(item, heads),
        )

    def test_smoke_passed_state_requires_ok_smoke_dimension(self):
        """state='smoke_passed' with smoke='unknown' stays refused."""
        item = self._item()
        self._deploy_event(item, state='smoke_passed', smoke_result='unknown')
        heads = WorkflowService._merged_pr_heads(item)
        self.assertIsNone(
            WorkflowService._find_correlated_deploy_event(item, heads),
        )

    def test_ryo_fixture_head_sha_event_correlates(self):
        """Feature-head SHA identity (legacy producers) still correlates."""
        item = self._item()
        self._deploy_event(item, commit_sha=HEAD_SHA)
        heads = WorkflowService._merged_pr_heads(item)
        self.assertIsNotNone(
            WorkflowService._find_correlated_deploy_event(item, heads),
        )


class CompletionReconciliationTests(APITestCase):
    """Defect 1: completing a blocker must reconcile its blocked dependents
    in-transaction (the legacy mark_completed path reconciled nothing)."""

    def setUp(self):
        from django.contrib.auth import get_user_model

        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username='plat7422', email='plat7422@shizuha.com',
            password='testpass123',
        )
        self.user.organization_memberships = {1: 'member'}
        self.project = Project.objects.create(
            name='Platform', key='PLT', org_id=1,
            access_level='private', is_active=True,
        )
        ProjectMembership.objects.create(
            project=self.project,
            user_id=self.user.id,
            user_email=self.user.email,
            role='member',
        )
        # Seed the engineering roster so the resume path's team-member
        # resolution has a real member (mirrors pls913's _set_test_owner).
        from tasks.models import TeamRoster
        TeamRoster.objects.update_or_create(
            organization_id=1,
            team_name='engineering',
            defaults={
                'organization_slug': 'shizuha',
                'tiers': [[self.user.email]],
                'is_active': True,
            },
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def _task(self, item_key, title, status, **fields):
        """Create a task and stamp ownership via queryset update — the model
        save path validates assignee org membership against the (unreachable
        in tests) admin service, so fixtures bypass it like pls913 does.
        The DB assignment-org guard (migration 0105) passes a stamped assignee
        only when the item's assignment_group resolves to a routing team or a
        seeded roster member, so assignee stamps default to the seeded
        'engineering' group."""
        item = Item.objects.create(
            mode='task', project=self.project, item_key=item_key,
            title=title, status=status,
            created_by_id=self.user.id, created_by_email=self.user.email,
        )
        ownership = {
            k: v for k, v in fields.items()
            if k in ('assignee_id', 'assignee_email', 'assignment_group')
        }
        if ownership:
            if ownership.get('assignee_id') and not ownership.get('assignment_group'):
                ownership['assignment_group'] = 'engineering'
            Item.objects.filter(pk=item.pk).update(**ownership)
            item.refresh_from_db()
        return item

    def _blocker_and_dependent(self, *, dependent_extra_blocker=None):
        blocker = self._task(
            'PLT-9001', 'blocker', 'in_progress',
            assignee_id=self.user.id, assignee_email=self.user.email,
        )
        # The model choke-point rejects creating an Item directly in `blocked`
        # (PLAT-4215); enter blocked via QuerySet.update — the exact writer the
        # deferred trigger covers — after stamping the captured pre-block state
        # (auto-block normally writes it when the link parks the dependent).
        dependent = self._task(
            'PLT-9002', 'dependent', 'in_progress',
            assignee_id=self.user.id, assignee_email=self.user.email,
        )
        Item.objects.filter(pk=dependent.pk).update(
            status='blocked',
            metadata={
                'pre_block_state': {
                    'status': 'in_progress',
                    'had_active_blocker': True,
                    'blocked_by_email': 'system',
                },
            },
        )
        dependent.refresh_from_db()
        ItemLink.objects.create(
            source_item=blocker, target_item=dependent, link_type='blocks',
            created_by_id=self.user.id,
        )
        if dependent_extra_blocker is not None:
            ItemLink.objects.create(
                source_item=dependent_extra_blocker, target_item=dependent,
                link_type='blocks', created_by_id=self.user.id,
            )
        return blocker, dependent

    def test_completing_blocker_resumes_blocked_dependent(self):
        """The PLAT-7389/PLAT-7406 shape: complete -> dependent auto-resumes."""
        blocker, dependent = self._blocker_and_dependent()
        resp = self.client.post(f'/api/items/{blocker.id}/complete/', {}, format='json')
        self.assertEqual(resp.status_code, 200, resp.content[:400])
        blocker.refresh_from_db()
        dependent.refresh_from_db()
        # The model coerces 'completed' to the workflow's terminal slug
        # ('done' on the project default workflow) — assert terminality.
        self.assertTrue(
            WorkflowService._status_is_terminal(blocker.status),
            f"blocker status {blocker.status!r} is not terminal",
        )
        self.assertEqual(dependent.status, 'in_progress')

    def test_dependent_behind_unowned_remaining_blocker_fails_typed(self):
        """Dependent kept blocked ONLY by an unowned active blocker -> typed 409
        naming the dependent, not the opaque constraint error."""
        unowned = self._task('PLT-9003', 'unowned blocker', 'in_progress')
        blocker, dependent = self._blocker_and_dependent(
            dependent_extra_blocker=unowned,
        )
        resp = self.client.post(f'/api/items/{blocker.id}/complete/', {}, format='json')
        self.assertEqual(resp.status_code, 409, resp.content[:400])
        body = resp.json()
        self.assertEqual(body.get('code'), 'blocked_dependent_unowned_blocker')
        self.assertIn('PLT-9002', str(body.get('error')))
        self.assertIn('PLT-9003', str(body.get('error')))
        blocker.refresh_from_db()
        dependent.refresh_from_db()
        # Rolled back: the completion never committed.
        self.assertEqual(blocker.status, 'in_progress')
        self.assertEqual(dependent.status, 'blocked')

    def test_dependent_behind_owned_remaining_blocker_stays_blocked(self):
        """A team-routed remaining blocker owns the dependent — completion
        succeeds and the dependent legitimately stays blocked."""
        team_blocker = self._task(
            'PLT-9004', 'team blocker', 'in_progress',
            assignment_group='engineering',
        )
        blocker, dependent = self._blocker_and_dependent(
            dependent_extra_blocker=team_blocker,
        )
        resp = self.client.post(f'/api/items/{blocker.id}/complete/', {}, format='json')
        self.assertEqual(resp.status_code, 200, resp.content[:400])
        dependent.refresh_from_db()
        self.assertEqual(dependent.status, 'blocked')


class NakedItemCitationMiddlewareTests(TestCase):
    """Defect 1 (message layer): the 409 must name the ACTUAL naked item(s)
    from the trigger message and label PLAT-4215 as the policy — never leave
    "(PLAT-4215)" readable as a blocker task citation."""

    def setUp(self):
        self.middleware = StructuredApiExceptionMiddleware(lambda r: None)
        self.request = SimpleNamespace(path='/api/items/1/complete/')

    def _naked_item(self, item_key, title):
        # The model choke-point rejects creating an Item directly in `blocked`
        # (PLAT-4215); enter blocked via QuerySet.update like the trigger does.
        # Callers wrap the whole test in an atomic block that is rolled back
        # (set_rollback) so the naked row and its queued deferred-trigger event
        # never reach the teardown's SET CONSTRAINTS ALL IMMEDIATE.
        item = Item.objects.create(
            mode='task', title=title, status='open',
            item_key=item_key, created_by_id=1,
            created_by_email='x@shizuha.com',
        )
        Item.objects.filter(pk=item.pk).update(status='blocked')
        item.refresh_from_db()
        return item

    @staticmethod
    def _json(resp):
        import json
        return json.loads(resp.content)

    def test_409_names_resolved_naked_item_and_policy_label(self):
        with transaction.atomic():
            naked = self._naked_item('PLT-9010', 'naked dependent')
            exc = IntegrityError(
                'blocked_item_requires_active_assigned_blocker: '
                f'item_id={naked.id}\nDETAIL: PLAT-4215 requires every blocked '
                'task to have an active linked blocker.'
            )
            resp = self.middleware.process_exception(self.request, exc)
            self.assertIsNotNone(resp)
            self.assertEqual(resp.status_code, 409)
            body = self._json(resp)
            self.assertEqual(
                body['error'], 'blocked_item_requires_active_assigned_blocker')
            self.assertEqual(body['blocked_items'][0]['item_key'], 'PLT-9010')
            self.assertEqual(body['blocked_items'][0]['item_id'], naked.id)
            # The policy token is explicitly disambiguated from a citation.
            self.assertIn('POLICY', body['detail'])
            self.assertIn('PLT-9010', body['detail'])
            self.assertIn('NOT the blocker task', body['detail'])
            # Discard the naked row + its queued deferred-trigger event.
            transaction.set_rollback(True)

    def test_409_survives_unparsable_trigger_message(self):
        exc = IntegrityError('blocked_item_requires_active_assigned_blocker')
        resp = self.middleware.process_exception(self.request, exc)
        self.assertIsNotNone(resp)
        self.assertEqual(resp.status_code, 409)
        body = self._json(resp)
        self.assertEqual(body['blocked_items'], [])
        self.assertIn('unresolved', body['detail'])

    def test_409_resolves_via_exception_cause_chain(self):
        with transaction.atomic():
            naked = self._naked_item('PLT-9011', 'naked dependent 2')
            cause = IntegrityError(
                'blocked_item_requires_active_assigned_blocker: '
                f'item_id={naked.id}'
            )
            exc = IntegrityError('violates foreign key constraint')
            exc.__cause__ = cause
            resp = self.middleware.process_exception(self.request, exc)
            self.assertIsNotNone(resp)
            body = self._json(resp)
            self.assertEqual(body['blocked_items'][0]['item_key'], 'PLT-9011')
            # Discard the naked row + its queued deferred-trigger event.
            transaction.set_rollback(True)
