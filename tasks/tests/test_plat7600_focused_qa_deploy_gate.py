"""PLAT-7600: focused-QA legs verifying non-deployable targets close without waivers.

The PLS-675/PLAT-1351 v4 deploy-verify gate demanded merged-head-SHA /
DeploymentEvent evidence from focused-QA legs whose verification target is
non-deployable bookkeeping (waiver mints, people-ops changes, research
dossiers, CI-only scanner findings) — structurally unsatisfiable classes that
forced waiver-for-waiver recursion on every such leg.

Fix under test:
1. Class-aware acceptance: a focused-QA leg whose origin is non-deployable
   resolves to ``code`` (QA Operating Model governs the leg) and the gate's
   deploy evidence classes are exempt — durable even for rows whose
   acceptance_mode field was persisted ``live``.
2. Correlation inheritance: a leg with a DEPLOYABLE origin inherits the
   origin's deployment correlation via the ingest's own spawn binding.
3. Fail-closed preserved: a deployable leg with no origin chain still fails.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase

from tasks.models import DeploymentEvent, Item, Project
from tasks.services.workflow_service import WorkflowService

MERGED_SHA = 'deadbeefcafe1234567890abcdef1234567890ab'
REPO = 'shizuha-labs/x'

User = get_user_model()


class Plat7600Base(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username='p7600-admin', email='p7600-admin@agents.shizuha.io',
            password='x', is_staff=True,
        )
        self.project = Project.objects.create(
            name='P7600', key='P76', org_id=1, access_level='open',
        )

    def _leg(self, key, *, origin=None, title='Focused QA: verify', source='focused-qa-automation',
             item_type='task.focused_qa', acceptance_mode='', labels=None, tags=None):
        metadata = {}
        if origin is not None:
            metadata['focused_qa_origin_item_id'] = str(origin.pk)
            metadata['focused_qa_origin_item_key'] = origin.item_key or ''
        return Item.objects.create(
            project=self.project,
            item_key=key,
            title=title,
            mode='task',
            item_type=item_type,
            source=source,
            status='in_progress',
            workflow_name='focused-qa',
            workflow_status='in_progress',
            assignment_group='qa',
            acceptance_mode=acceptance_mode,
            labels=labels or {},
            tags=tags or [],
            metadata=metadata,
            created_by_email=self.admin.email,
            created_by_id=self.admin.id,
        )

    def _origin(self, key, title, *, labels=None, tags=None):
        return Item.objects.create(
            project=self.project,
            item_key=key,
            title=title,
            mode='task',
            status='done',
            workflow_name='autonomous-bug',
            workflow_status='done',
            assignment_group='engineering',
            labels=labels or {},
            tags=tags or [],
            created_by_email=self.admin.email,
            created_by_id=self.admin.id,
        )


class NonDeployableLegExemptionTests(Plat7600Base):
    """Acceptance 1: non-deployable verification targets close without waivers."""

    def test_waiver_mint_origin_leg_resolves_code_and_exempts_deploy_classes(self):
        origin = self._origin('P76-O1', 'Deploy-verify waiver mint for HIVE-1401')
        leg = self._leg('P76-Q1', origin=origin,
                        labels={'risk': 'user-facing'})
        self.assertEqual(
            WorkflowService._resolve_acceptance_mode(leg),
            WorkflowService._ACCEPTANCE_MODE_CODE,
        )
        self.assertEqual(
            WorkflowService._collect_deploy_verify_missing(leg, 'live'),
            [],
        )

    def test_imperative_mint_title_leg_exempt_frg66_shape(self):
        """Live repro FRG-68 (kei 2026-09-07): origin FRG-66 'Mint role-gated
        deploy-verify waiver for FRG-61 …'.

        The mint-executor carries NO waiver receipt itself (the receipt lands
        on the waiver TARGET) and no non-deploy label tokens — only the
        imperative mint title identifies the class. The prior fixture used
        'Deploy-verify waiver mint …' which matched the pre-existing
        'deploy-verify ' prefix and masked the real-world title shape.
        """
        origin = self._origin(
            'P76-O10',
            'Mint role-gated deploy-verify waiver for FRG-61 '
            '(no identifiable merged head; live fix verified — endpoint call documented)',
            tags=['blocker', 'decision-required', 'escalation', 'auth',
                  'infra-control-plane', 'security-sensitive'],
        )
        leg = self._leg('P76-Q10', origin=origin, acceptance_mode='live',
                        labels={'risk': 'auth'})
        self.assertEqual(
            WorkflowService._resolve_acceptance_mode(leg),
            WorkflowService._ACCEPTANCE_MODE_CODE,
        )
        self.assertEqual(
            WorkflowService._collect_deploy_verify_missing(leg, 'live'),
            [],
        )

    def test_mint_title_variants_exempt(self):
        """Observed mint-executor title shapes (HIVE-2261 / PLS-1126 / DRV-155)."""
        titles = [
            'Mint deploy-verify waiver for HIVE-1401 '
            '(test-discovery artifact, DeploymentEvent recorded) and close',
            'Mint deploy-verify waiver for PLS-619 '
            '(CI-only artifact, DeploymentEvent recorded) and close',
            'Mint the typed deploy-verify waiver on the DRV-155 target and close',
        ]
        for i, title in enumerate(titles):
            origin = self._origin(f'P76-O2{i}', title)
            leg = self._leg(f'P76-Q2{i}', origin=origin, acceptance_mode='live')
            self.assertEqual(
                WorkflowService._collect_deploy_verify_missing(leg, 'live'),
                [],
                title,
            )

    def test_waiver_endpoint_builder_task_stays_governed(self):
        """Negative: a CODE task that BUILDS the mint endpoint is deployable.

        The exemption is anchored to the imperative 'Mint …' title form; an
        'Add …' implementation task keeps the gate armed.
        """
        origin = self._origin('P76-O23', 'Add role-gated deploy-verify waiver mint endpoint',
                              labels={'risk': 'auth'})
        # Leg marker follows the module convention (user-facing → governed
        # live default); the origin's 'risk: auth' domain label is not a
        # recognized acceptance marker, so the leg would otherwise default to
        # 'code' and the negative control would assert nothing. Never
        # executable before 2026-09-14 — the fork-PR secret crash (PLAT-8568)
        # killed the suite at 2s on #467, and main's push lane skips it.
        leg = self._leg('P76-Q23', origin=origin, labels={'user-facing': True})
        self.assertNotEqual(
            WorkflowService._resolve_acceptance_mode(leg),
            WorkflowService._ACCEPTANCE_MODE_CODE,
        )
        missing = WorkflowService._collect_deploy_verify_missing(leg, 'live')
        self.assertIn(WorkflowService._MISSING_MERGED_HEAD, missing)

    def test_process_meta_origin_leg_exempt(self):
        origin = self._origin('P76-O2', 'Formal review of deploy#562 verdict')
        leg = self._leg('P76-Q2', origin=origin)
        self.assertEqual(
            WorkflowService._resolve_acceptance_mode(leg),
            WorkflowService._ACCEPTANCE_MODE_CODE,
        )
        self.assertEqual(
            WorkflowService._collect_deploy_verify_missing(leg, 'live'),
            [],
        )

    def test_research_origin_leg_exempt(self):
        origin = self._origin('P76-O3', 'SCLI-346 research dossier',
                              labels={'research': True})
        leg = self._leg('P76-Q3', origin=origin)
        self.assertEqual(
            WorkflowService._collect_deploy_verify_missing(leg, 'live'),
            [],
        )

    def test_ci_only_scanner_origin_leg_exempt(self):
        origin = self._origin('P76-O4', 'npm HIGH remediation round 2',
                              tags=['security-ci', 'deps'])
        leg = self._leg('P76-Q4', origin=origin)
        self.assertEqual(
            WorkflowService._collect_deploy_verify_missing(leg, 'live'),
            [],
        )

    def test_persisted_live_mode_still_exempt(self):
        """The exemption is durable for rows whose field was persisted live."""
        origin = self._origin('P76-O5', 'EVOL-65 people-ops team retirement',
                              labels={'people-ops': True})
        leg = self._leg('P76-Q5', origin=origin, acceptance_mode='live')
        self.assertEqual(
            WorkflowService._collect_deploy_verify_missing(leg, 'live'),
            [],
        )

    def test_deployable_origin_leg_stays_governed(self):
        """Negative: a leg whose origin IS deployable keeps the gate armed.

        The spawn copies the origin's risk labels onto the leg (that is the
        inheritance path the defect describes), so the leg carries them here.
        """
        origin = self._origin('P76-O6', 'Fix the login crash',
                              labels={'risk': 'user-facing'})
        leg = self._leg('P76-Q6', origin=origin, labels={'user-facing': True})
        self.assertNotEqual(
            WorkflowService._resolve_acceptance_mode(leg),
            WorkflowService._ACCEPTANCE_MODE_CODE,
        )
        missing = WorkflowService._collect_deploy_verify_missing(leg, 'live')
        self.assertIn(WorkflowService._MISSING_MERGED_HEAD, missing)

    def test_non_leg_item_unaffected(self):
        """A plain task with the same title shape is NOT exempt."""
        plain = self._origin('P76-O7', 'Deploy-verify waiver mint lookalike')
        plain.source = ''
        plain.save()
        missing = WorkflowService._collect_deploy_verify_missing(plain, 'live')
        self.assertIn(WorkflowService._MISSING_MERGED_HEAD, missing)


class CorrelationInheritanceTests(Plat7600Base):
    """Acceptance 2: the originating task's deploy chain satisfies the leg."""

    def test_leg_without_origin_chain_still_fails_closed(self):
        """Negative: inheritance never fabricates evidence."""
        origin = self._origin('P76-O8', 'Fix the login crash',
                              labels={'risk': 'auth'})
        leg = self._leg('P76-Q8', origin=origin)
        missing = WorkflowService._collect_deploy_verify_missing(leg, 'live')
        self.assertIn(WorkflowService._MISSING_MERGED_HEAD, missing)

    def test_origin_binding_mismatch_never_projects(self):
        """A forged/mismatched origin key projects nothing."""
        origin = self._origin('P76-O9', 'Fix the login crash',
                              labels={'risk': 'auth'})
        leg = self._leg('P76-Q9', origin=origin)
        leg.metadata['focused_qa_origin_item_key'] = 'P76-OTHER'
        leg.save()
        missing = WorkflowService._collect_deploy_verify_missing(leg, 'live')
        self.assertIn(WorkflowService._MISSING_MERGED_HEAD, missing)


class OriginDeployEvidenceCarriedTests(Plat7600Base):
    """PLAT-7506: a focused-QA leg whose ORIGIN carries the deploy proof is exempt.

    A focused-QA leg verifies; it never deploys. When the originating dev task
    has a correlated production DeploymentEvent, the deploy evidence is
    task-correlated to the ORIGIN and can never be self-correlated to the leg
    — demanding it made every deployable-origin QA leg unclosable without an
    individual waiver (9 waiver round-trips in one session, 2026-09-02). The
    exemption uses the SAME correlation machinery the gate applies to the
    origin itself, so it is exactly as strong as the origin's own deploy
    evidence — no weaker, just correctly addressed.
    """

    def _origin_with_merged_pr(self, key, title, *, sha=MERGED_SHA, repo=REPO,
                               labels=None, tags=None):
        origin = self._origin(key, title, labels=labels, tags=tags)
        origin.metadata = dict(origin.metadata or {})
        origin.metadata['pull_requests'] = [{
            'url': f'https://origin.shizuha.com/{repo}/pulls/1',
            'repo': repo,
            'number': 1,
            'status': 'merged',
            'merged_at': '2026-09-01T00:00:00Z',
            'head_sha': sha,
        }]
        origin.save()
        return origin

    def _deploy_event(self, *, origin, sha=MERGED_SHA, repo=REPO):
        return DeploymentEvent.objects.create(
            idempotency_key=f'p7506-{origin.item_key}-{sha[:12]}',
            project=self.project,
            repo=repo,
            commit_sha=sha,
            service='x',
            environment='production',
            state='verified_by_qa',
            smoke_result='passed',
            image='registry.example/x:abc',
            image_digest='sha256:' + 'ab' * 32,
            source_task_keys=[origin.item_key],
            metadata={
                'replicas_ready': True,
                'live_probe': {
                    'schema': 'shizuha.pulse.live-probe.v1',
                    'result': 'passed',
                    'evidence': 'https://prod/x health 200',
                    'verified_by': 'qa@shizuha.com',
                },
            },
        )

    def test_deployable_origin_with_correlated_event_leg_exempt(self):
        origin = self._origin_with_merged_pr('P76-O8', 'Fix the login crash',
                                             labels={'risk': 'user-facing'})
        self._deploy_event(origin=origin)
        leg = self._leg('P76-Q8', origin=origin, labels={'user-facing': True})
        self.assertEqual(
            WorkflowService._resolve_acceptance_mode(leg),
            WorkflowService._ACCEPTANCE_MODE_CODE,
        )
        self.assertEqual(
            WorkflowService._collect_deploy_verify_missing(leg, 'live'),
            [],
        )

    def test_deployable_origin_without_event_leg_stays_governed(self):
        """Merged PR but no correlated event: the leg keeps the strict path."""
        origin = self._origin_with_merged_pr('P76-O9', 'Fix the login crash',
                                             labels={'risk': 'user-facing'})
        leg = self._leg('P76-Q9', origin=origin, labels={'user-facing': True})
        self.assertNotEqual(
            WorkflowService._resolve_acceptance_mode(leg),
            WorkflowService._ACCEPTANCE_MODE_CODE,
        )
        missing = WorkflowService._collect_deploy_verify_missing(leg, 'live')
        self.assertIn(WorkflowService._MISSING_MERGED_HEAD, missing)

    def test_uncorrelated_event_never_exempts(self):
        """Fail-closed: an event for an unrelated SHA exempts nothing."""
        origin = self._origin_with_merged_pr('P76-O10', 'Fix the login crash',
                                             labels={'risk': 'user-facing'})
        self._deploy_event(origin=origin, sha='0123456789abcdef0123456789abcdef01234567')
        leg = self._leg('P76-Q10', origin=origin, labels={'user-facing': True})
        self.assertNotEqual(
            WorkflowService._resolve_acceptance_mode(leg),
            WorkflowService._ACCEPTANCE_MODE_CODE,
        )
