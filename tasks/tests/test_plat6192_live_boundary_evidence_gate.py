"""PLAT-6192 (retro 14 Aug 2026): reject-as-fixed requires live-boundary evidence.

DOJO-40 was marked rejected (already-fixed) on helper/unit proof — sanitizer
denylist tests fed crafted marker strings — while the real create-session caller
still leaked paraphrased internals in production (DOJO-54 P1 live). This test
proves the new `live_boundary_evidence` validator on the security-finding Reject
transition denies the DOJO-40-with-fresh-vantage case (helper-only proof, no
rendered/persisted live run) and allows it once a `## Live-boundary evidence`
comment names the real caller sequence + persisted-layer assertion.
"""
from django.test import TestCase

from tasks.models import Comment, Item, Project, Status, Workflow, WorkflowStatus, WorkflowTransition
from tasks.services.workflow_service import WorkflowService, WorkflowValidationError

REJECT_TRANSITION_ID = 'c102e019-abf9-44d8-b4fb-b202b32ef223'

HELPER_ONLY_PROOF = (
    "Fix verified: _sanitize_interviewer_content denylist rejects all 6 markers — "
    "8/8 unit tests green. Rejecting as already-fixed."
)

LIVE_BOUNDARY_EVIDENCE = (
    "## Live-boundary evidence\n"
    "Real caller sequence exercised: `POST /api/sessions/` (role=swe) with the "
    "QA-captured paraphrased leak → opener turn generated → `GET /api/sessions/{id}/` "
    "re-read → `POST /api/turns/`. Persisted rows (interview_turn.content) asserted "
    "safe and equal to the serialized API response — no internal instructions/scratchpad "
    "in either layer."
)


class LiveBoundaryEvidenceGateTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(
            name='Dojo', key='DOJO', org_id=1,
            access_level='private', is_active=True,
        )
        self._seed_security_finding_workflow()
        self.item = Item.objects.create(
            project=self.project,
            title='[SECURITY] Prompt leak in interviewer',
            description='Fresh session exposes internal prompt.',
            labels=['security-sensitive'],
            tags='prompt-leak,security',
            status='needs_triage',
            workflow_name='security-finding',
            item_type='task',
            created_by_id=1,
            created_by_email='kai@shizuha.com',
        )
        self.actor = type('Actor', (), {'id': 7, 'email': 'security-lead@shizuha.com'})()

    def _seed_security_finding_workflow(self):
        statuses = {}
        for position, (slug, category) in enumerate(
            (
                ('needs_triage', 'todo'),
                ('rejected', 'done'),
                ('verification', 'in_progress'),
                ('completed', 'done'),
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
            statuses[slug] = status
        workflow, _ = Workflow.objects.get_or_create(
            slug='security-finding',
            defaults={
                'name': 'Security Finding',
                'initial_status': statuses['needs_triage'],
                'is_system': True,
            },
        )
        for position, slug in enumerate(statuses, start=1):
            WorkflowStatus.objects.get_or_create(
                workflow=workflow, status=statuses[slug],
                defaults={'position': position},
            )
        reject = WorkflowTransition.objects.filter(
            workflow=workflow, name='Reject', from_status='needs_triage',
        ).first()
        if reject is None:
            reject = WorkflowTransition.objects.create(
                workflow=workflow,
                name='Reject',
                from_status='needs_triage',
                to_status='rejected',
                position=2,
            )
        reject.validators = [
            {
                'type': 'live_boundary_evidence',
                'message': 'Reject-as-fixed denied (PLAT-6192): post ## Live-boundary evidence.',
            },
        ]
        reject.save(update_fields=['validators'])
        self.reject_transition_id = str(reject.id)

    def _comment(self, text):
        return Comment.objects.create(
            item=self.item, content=text, author_id=1,
            author_email='kai@shizuha.com',
        )

    def test_helper_only_proof_is_denied(self):
        """The DOJO-40-with-fresh-vantage case: helper/unit proof alone is denied."""
        self._comment(HELPER_ONLY_PROOF)
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService.execute_transition(
                self.item, 'rejected', user=self.actor,
                transition_id=self.reject_transition_id,
            )
        self.assertIn('Live-boundary evidence', str(ctx.exception))
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, 'needs_triage')

    def test_no_evidence_at_all_is_denied(self):
        with self.assertRaises(WorkflowValidationError):
            WorkflowService.execute_transition(
                self.item, 'rejected', user=self.actor,
                transition_id=self.reject_transition_id,
            )
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, 'needs_triage')

    def test_live_boundary_evidence_allows_reject(self):
        self._comment(LIVE_BOUNDARY_EVIDENCE)
        WorkflowService.execute_transition(
            self.item, 'rejected', user=self.actor,
            transition_id=self.reject_transition_id,
        )
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, 'rejected')

    def test_bare_heading_without_surface_persist_is_denied(self):
        self._comment('## Live-boundary evidence\nIt is fixed now.')
        with self.assertRaises(WorkflowValidationError):
            WorkflowService.execute_transition(
                self.item, 'rejected', user=self.actor,
                transition_id=self.reject_transition_id,
            )
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, 'needs_triage')

    def test_helper_detects_evidence_across_comments(self):
        self._comment('## Live-boundary evidence\ncurl POST /api/sessions/ ... persisted row asserted safe == rendered.')
        self.assertTrue(WorkflowService._has_live_boundary_evidence(self.item))

    # --- PLAT-10343: dependency/packaging caller surfaces ---

    DEPS_CLASS_EVIDENCE = (
        "## Live-boundary evidence showing the fix through the REAL caller "
        "sequence with persisted layer asserted safe == rendered\n"
        "Caller (real sequence): frontend/package.json @ main 9e7c4721 declares "
        "devDependencies['@playwright/test'] = '^1.55.1'; the caller's install "
        "path is npm ci, which materializes exactly the lockfile entries.\n"
        "Persisted layer (read at main 9e7c4721): frontend/package-lock.json "
        "packages keys — node_modules/playwright -> 1.62.1 (>= fixed 1.55.1 for "
        "GHSA-7mvr-c777-76hp).\n"
        "Rendered assertion: npm ci renders the persisted entries verbatim, so "
        "persisted layer asserted safe == rendered."
    )

    def test_deps_class_evidence_without_url_tokens_is_allowed(self):
        """PLAT-4450 regression: compliant lockfile-class evidence (npm ci →
        package-lock → persisted versions) contains NO http/api/browser/curl
        token, yet is real caller-sequence proof. The old surface family
        always-denied it; the packaging surfaces must satisfy the check."""
        self._comment(self.DEPS_CLASS_EVIDENCE)
        WorkflowService.execute_transition(
            self.item, 'rejected', user=self.actor,
            transition_id=self.reject_transition_id,
        )
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, 'rejected')

    def test_generic_advisory_prose_is_not_a_caller_surface(self):
        """dependency/lockfile/devDependencies/npm-space tokens substring-match
        advisory prose and must not satisfy the live-boundary gate."""
        self._comment(
            '## Live-boundary evidence\n'
            'The dependency advisory says the lockfile and devDependencies '
            'are fixed; npm is mentioned only as prose. persisted row stored.'
        )
        with self.assertRaises(WorkflowValidationError):
            WorkflowService.execute_transition(
                self.item, 'rejected', user=self.actor,
                transition_id=self.reject_transition_id,
            )
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, 'needs_triage')

    def test_deps_evidence_without_persisted_assertion_still_denied(self):
        """A deps-surface mention alone (no persisted-layer assertion) stays
        denied — the bar is a caller surface AND the persisted assertion."""
        self._comment(
            '## Live-boundary evidence\n'
            'npm ci resolves the lockfile; the advisory is fixed now.'
        )
        with self.assertRaises(WorkflowValidationError):
            WorkflowService.execute_transition(
                self.item, 'rejected', user=self.actor,
                transition_id=self.reject_transition_id,
            )
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, 'needs_triage')
