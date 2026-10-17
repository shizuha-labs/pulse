"""PLS-1218: the deploy-verify close gate is task-class-aware for no-PR
verification classes.

Regression watchlists and standing cadence/live-verification tasks verify the
LIVE stack and have no merged head or PR of their own, so the merged-head
evidence class (PLS-675/PLAT-1351 v4) is structurally unreachable for them —
verified-complete work stranded in_review and seats burned turns on evidence
surgery (CTX-933/CTX-936, PLS-1215) or hit impossible demands (PLS-1212's
``missing_merged_head_sha``).

Contract locked here:
- typed regression watchlists project their producer-persisted event and a
  ``verified_by_qa`` correlated event IS the class's typed QA verdict (the
  live-probe receipt mint requires a merged PR head — unreachable here);
- explicitly-marked cadence/live-verification tasks (label/tag marker or
  metadata ``verification_class=live``) correlate via the structured
  ``source_task_keys`` ingest relationship only — never mutable prose
  (PLS-888 preserved), org-isolated, prod success;
- a task with NO correlated event reports ``missing_correlated_deploy_event``
  (the reachable missing class), never ``missing_merged_head_sha``;
- NO over-exemption: any task with a merged PR head stays on the full
  PLS-675 path, and unmarked no-PR work still fails closed.
"""
from django.test import TestCase

from tasks.services.workflow_service import WorkflowService


MERGED_SHA = 'deadbeefcafe1234567890abcdef1234567890ab'
REPO = 'shizuha-labs/x'


class NoPrVerificationCloseTests(TestCase):
    """PLS-1218 — task-class-aware evidence resolution.

    Deliberately NOT a subclass of DeployVerifyGateV4Tests: subclassing would
    re-run that entire suite a second time. Only the fixture factories are
    replicated here (kept in sync with the PLAT-1351 file).
    """

    def setUp(self):
        from tasks.models import Project

        self.project = Project.objects.create(
            name='Platform', key='PLAT', org_id=1,
            access_level='private', is_active=True,
        )
        self._next_item_number = 1

    # ── factories (mirrored from test_plat1351_deploy_verify_gate) ──────────

    def _merged_pr(self, *, sha=MERGED_SHA, repo=REPO):
        return {
            'url': f'https://origin.shizuha.com/{repo}/pulls/1',
            'repo': repo,
            'number': 1,
            'status': 'merged',
            'merged_at': '2026-07-03T00:00:00Z',
            'head_sha': sha,
        }

    def _item(
        self,
        *,
        labels=None,
        tags='',
        workflow_name='autonomous-bug',
        acceptance_mode=None,
        prs='merged',
        status='in_review',
        metadata=None,
        **kw,
    ):
        from tasks.models import Item

        if metadata is None:
            metadata = {}
        else:
            metadata = dict(metadata)
        if prs == 'merged':
            metadata.setdefault('pull_requests', [self._merged_pr()])
        elif prs == 'open':
            metadata.setdefault('pull_requests', [{
                'url': f'https://origin.shizuha.com/{REPO}/pulls/2',
                'repo': REPO, 'number': 2, 'status': 'open',
                'head_sha': 'abc123open',
            }])
        elif prs == 'none':
            metadata.setdefault('pull_requests', [])
        elif isinstance(prs, list):
            metadata.setdefault('pull_requests', prs)
        kw.setdefault('item_key', f'PLAT-{self._next_item_number}')
        self._next_item_number += 1
        create_kw = dict(
            project=self.project,
            title='[BUG] prod 500 on close path',
            description='Service-affecting regression in production.',
            labels=labels if labels is not None else [],
            tags=tags,
            status=status,
            workflow_name=workflow_name,
            item_type='task',
            created_by_id=1,
            created_by_email='jun@shizuha.com',
            metadata=metadata,
        )
        if acceptance_mode is not None:
            create_kw['acceptance_mode'] = acceptance_mode
        create_kw.update(kw)
        return Item.objects.create(**create_kw)

    def _deploy_event(
        self,
        item,
        *,
        environment='production',
        state='smoke_passed',
        smoke_result='passed',
        commit_sha=MERGED_SHA,
        repo=REPO,
        image='registry.example/x:abc',
        replicas_ready=True,
        source_task_keys=None,
    ):
        from tasks.models import DeploymentEvent as DE

        meta = {}
        if replicas_ready is True:
            meta['replicas_ready'] = True
        elif replicas_ready is False:
            meta['replicas_ready'] = False
        return DE.objects.create(
            idempotency_key=(
                f'k-{item.item_key}-{environment}-{state}-'
                f'{commit_sha[:12]}-{DE.objects.count()}'
            ),
            project=self.project,
            repo=repo,
            commit_sha=commit_sha,
            service='x',
            environment=environment,
            state=state,
            smoke_result=smoke_result,
            image=image or '',
            source_task_keys=(
                source_task_keys if source_task_keys is not None
                else [item.item_key]
            ),
            metadata=meta,
        )

    def _watchlist_item(self, *, event=None, metadata=None):
        meta = {'deployment_qa_kind': 'regression-watchlist'}
        if event is not None:
            meta['deployment_event_id'] = str(event.id)
        if metadata:
            meta.update(metadata)
        return self._item(
            labels={'risk_label': 'auth', 'environment': 'live'},
            tags='user-facing',
            prs='none',
            acceptance_mode='live',
            metadata=meta,
            item_type='task.regression_watchlist',
            source='deployment-event',
        )

    def _cadence_item(self, *, metadata=None):
        meta = {'verification_class': 'live'}
        if metadata:
            meta.update(metadata)
        return self._item(
            labels={'risk_label': 'auth', 'environment': 'live'},
            tags='user-facing,live-verification',
            prs='none',
            acceptance_mode='live',
            metadata=meta,
        )

    def _verified_event(self, item, **kw):
        kw.setdefault('state', 'verified_by_qa')
        kw.setdefault('smoke_result', 'unknown')
        return self._deploy_event(item, **kw)

    # ── typed regression watchlist (CTX-933 / PLS-1215 pattern) ─────────────

    def test_watchlist_with_verified_projected_event_closes(self):
        event = self._verified_event(
            self._item(prs='none'),  # producer event; FK wired below
        )
        item = self._watchlist_item(event=event)
        missing = WorkflowService._collect_deploy_verify_missing(item, 'live')
        self.assertEqual(missing, [])

    def test_watchlist_without_event_reports_correlated_deploy(self):
        item = self._watchlist_item()
        missing = WorkflowService._collect_deploy_verify_missing(item, 'live')
        self.assertEqual(
            missing, [WorkflowService._MISSING_CORRELATED_DEPLOY],
        )
        self.assertNotIn(WorkflowService._MISSING_MERGED_HEAD, missing)

    def test_watchlist_event_missing_image_still_demands_image(self):
        event = self._verified_event(self._item(prs='none'), image='')
        item = self._watchlist_item(event=event)
        missing = WorkflowService._collect_deploy_verify_missing(item, 'live')
        self.assertIn(WorkflowService._MISSING_IMAGE, missing)
        self.assertNotIn(WorkflowService._MISSING_LIVE_PROBE, missing)

    def test_watchlist_event_missing_replicas_still_demands_replicas(self):
        event = self._verified_event(
            self._item(prs='none'), replicas_ready=False,
        )
        item = self._watchlist_item(event=event)
        missing = WorkflowService._collect_deploy_verify_missing(item, 'live')
        self.assertIn(WorkflowService._MISSING_REPLICAS_READY, missing)

    def test_watchlist_unverified_event_state_fails_closed(self):
        event = self._verified_event(
            self._item(prs='none'), state='created', smoke_result='unknown',
        )
        item = self._watchlist_item(event=event)
        missing = WorkflowService._collect_deploy_verify_missing(item, 'live')
        self.assertEqual(
            missing, [WorkflowService._MISSING_CORRELATED_DEPLOY],
        )

    # ── cadence / live-verification marker (PLS-1212 pattern) ───────────────

    def test_cadence_with_task_named_verified_event_closes(self):
        item = self._cadence_item()
        self._verified_event(item, source_task_keys=[item.item_key])
        missing = WorkflowService._collect_deploy_verify_missing(item, 'live')
        self.assertEqual(missing, [])

    def test_cadence_source_task_keys_match_is_case_insensitive(self):
        item = self._cadence_item()
        self._verified_event(item, source_task_keys=[item.item_key.lower()])
        missing = WorkflowService._collect_deploy_verify_missing(item, 'live')
        self.assertEqual(missing, [])

    def test_cadence_without_named_event_reports_correlated_deploy(self):
        item = self._cadence_item()
        # A prod success event exists but does NOT name this task — the
        # merely-related-event inheritance PLS-888 forbids.
        self._verified_event(self._item(prs='none'))
        missing = WorkflowService._collect_deploy_verify_missing(item, 'live')
        self.assertEqual(
            missing, [WorkflowService._MISSING_CORRELATED_DEPLOY],
        )
        self.assertNotIn(WorkflowService._MISSING_MERGED_HEAD, missing)

    def test_cadence_event_in_other_org_projects_nothing(self):
        item = self._cadence_item()
        other_project_event = self._verified_event(
            self._item(prs='none'), source_task_keys=[item.item_key],
        )
        other_project_event.project = None
        other_project_event.save()
        missing = WorkflowService._collect_deploy_verify_missing(item, 'live')
        self.assertEqual(
            missing, [WorkflowService._MISSING_CORRELATED_DEPLOY],
        )

    def test_cadence_non_prod_env_does_not_correlate(self):
        item = self._cadence_item()
        self._verified_event(
            item, source_task_keys=[item.item_key], environment='staging',
        )
        missing = WorkflowService._collect_deploy_verify_missing(item, 'live')
        self.assertEqual(
            missing, [WorkflowService._MISSING_CORRELATED_DEPLOY],
        )

    # ── no over-exemption (rigor preserved for code-PR work) ────────────────

    def test_marker_does_not_exempt_merged_head_work(self):
        # A task with a merged PR head stays on the full PLS-675 path even
        # when explicitly marked — the marker only ever covers no-PR classes.
        item = self._cadence_item()
        item.metadata['pull_requests'] = [self._merged_pr()]
        item.save()
        missing = WorkflowService._collect_deploy_verify_missing(item, 'live')
        self.assertIn(WorkflowService._MISSING_CORRELATED_DEPLOY, missing)

    def test_unmarked_no_pr_work_still_fails_closed(self):
        # PLS-888: ordinary no-PR work without the explicit marker keeps
        # failing closed with the merged-head class — no silent widening.
        item = self._item(
            labels={'risk_label': 'auth', 'environment': 'live'},
            tags='user-facing',
            prs='none',
            acceptance_mode='live',
        )
        self._verified_event(item, source_task_keys=[item.item_key])
        missing = WorkflowService._collect_deploy_verify_missing(item, 'live')
        self.assertIn(WorkflowService._MISSING_MERGED_HEAD, missing)

    def test_marker_alone_without_event_does_not_pass(self):
        item = self._cadence_item()
        missing = WorkflowService._collect_deploy_verify_missing(item, 'live')
        self.assertEqual(missing, [WorkflowService._MISSING_CORRELATED_DEPLOY])

    def test_focused_qa_legs_keep_plat7600_governance(self):
        # The no-PR verification class must not swallow focused-QA legs —
        # PLAT-7600's origin-correlation inheritance governs those.
        item = self._cadence_item()
        item.source = 'focused-qa-automation'
        item.item_type = 'task.focused_qa'
        # Valid-format UUID that does not exist (a malformed string would
        # trip UUIDField prep instead of exercising the projection path).
        item.metadata['focused_qa_deployment_event_id'] = (
            '00000000-0000-0000-0000-000000000000'
        )
        item.save()
        missing = WorkflowService._collect_deploy_verify_missing(item, 'live')
        self.assertIn(WorkflowService._MISSING_MERGED_HEAD, missing)
