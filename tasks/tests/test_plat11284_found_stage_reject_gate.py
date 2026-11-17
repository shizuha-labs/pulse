"""PLAT-11284 regression: the live-boundary evidence gate must be
disposition-consistent across security-finding reject paths.

Migration 0163 (PLAT-6192) added the `live_boundary_evidence` validator to
transitions named iexact 'Reject' only. The found-stage
`Verification Complete - Reject` transition lands on the same rejected
terminal status but never received the validator — so the same
reject-as-fixed verdict that path A denies for missing evidence sails
through path B (observed 2026-10-05: ren 0-for-5 on path A vs 4-for-4 on
path B with equivalent payloads; the seat-wedge hypothesis is disproven —
the 400 is this content validator, and path B simply lacks it).

0191 extends the validator to the found-stage reject transition.

Test design note: data migrations run BEFORE test fixtures, so seeded rows
never receive migration effects for free. The coverage test therefore loads
the REAL 0163/0191 forward functions via importlib and applies them to the
seeded rows, mirroring the production ordering (0163 name-match first, then
0191's extension). The behavior tests then mirror the post-migration state
manually (as test_plat6192_live_boundary_evidence_gate.py does for 0163).
"""
from importlib import import_module
from types import SimpleNamespace

from django.test import TestCase

from tasks.models import (
    Comment, Item, Project, Status, Workflow, WorkflowStatus, WorkflowTransition,
)
from tasks.services.workflow_service import WorkflowService, WorkflowValidationError

M0163 = 'tasks.migrations.0163_plat6192_live_boundary_evidence_gate'
M0191 = 'tasks.migrations.0191_plat11284_found_stage_reject_live_boundary_gate'

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


def _apps_shim(model_cls):
    """Minimal apps.get_model shim so real migration functions run against
    the test database's live model class."""
    return SimpleNamespace(get_model=lambda *a, **k: model_cls)


def _has_validator(transition):
    return any(
        v.get('type') == 'live_boundary_evidence'
        for v in (transition.validators or [])
    )


class FoundStageRejectGateConsistencyTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(
            name='Dojo', key='DOJO', org_id=1,
            access_level='private', is_active=True,
        )
        self._seed_security_finding_workflow()
        self.actor = type('Actor', (), {'id': 7, 'email': 'security@shizuha.com'})()

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
        # Path A: needs_triage -> rejected (the transition 0163 gates by name).
        # from_status/to_status are CharField slugs (models.py:5781/5785).
        self.path_a = WorkflowTransition.objects.filter(
            workflow=workflow, name='Reject', from_status='needs_triage',
        ).first()
        if self.path_a is None:
            self.path_a = WorkflowTransition.objects.create(
                id='c102e019-abf9-44d8-b4fb-b202b32ef223',
                workflow=workflow,
                name='Reject',
                from_status='needs_triage',
                to_status='rejected',
                position=1,
            )
        # Path B: verification -> rejected (the found-stage transition that
        # 0163's name-match missed and 0191 extends to).
        self.path_b = WorkflowTransition.objects.create(
            workflow=workflow,
            name='Verification Complete - Reject',
            from_status='verification',
            to_status='rejected',
            position=2,
        )

    def _mirror_0163_then_0191(self):
        """Apply the REAL forward functions in production order."""
        m0163 = import_module(M0163)
        m0191 = import_module(M0191)
        apps = _apps_shim(WorkflowTransition)
        m0163.add_live_boundary_validator(apps, None)
        m0191.add_found_stage_reject_validator(apps, None)
        self.path_a.refresh_from_db()
        self.path_b.refresh_from_db()

    def _attach_validator(self, transition):
        transition.validators = [
            {
                'type': 'live_boundary_evidence',
                'message': 'Reject-as-fixed denied (PLAT-6192): post ## Live-boundary evidence.',
            },
        ]
        transition.save(update_fields=['validators'])

    def _item_at(self, status):
        return Item.objects.create(
            project=self.project,
            title='[SECURITY] Prompt leak in interviewer',
            description='Fresh session exposes internal prompt.',
            labels=['security-sensitive'],
            tags='prompt-leak,security',
            status=status,
            workflow_name='security-finding',
            item_type='task',
            created_by_id=1,
            created_by_email='kai@shizuha.com',
        )

    def _comment(self, item, content):
        Comment.objects.create(item=item, content=content, author_id=1)

    def test_0191_extends_gate_to_found_stage_reject(self):
        """The REAL 0191 forward function (after 0163's) lands the validator
        on BOTH reject paths — name-based coverage becomes
        disposition-based — and is idempotent on re-run."""
        self.assertFalse(_has_validator(self.path_a))
        self.assertFalse(_has_validator(self.path_b))
        self._mirror_0163_then_0191()
        self.assertTrue(_has_validator(self.path_a), 'Reject')
        self.assertTrue(_has_validator(self.path_b),
                        'Verification Complete - Reject')
        # Idempotency: a second pass must not duplicate the validator.
        self._mirror_0163_then_0191()
        self.assertEqual(
            sum(1 for v in (self.path_b.validators or [])
                if v.get('type') == 'live_boundary_evidence'),
            1,
        )

    def test_renamed_found_stage_reject_still_gated(self):
        """A rename drops the name match. The fallback must still gate it
        because it shares the workflow and the rejected to_status, even
        though its from_status is not needs_triage."""
        self.path_b.name = 'Disposition - Reject'
        self.path_b.save(update_fields=['name'])
        self._mirror_0163_then_0191()
        self.path_b.refresh_from_db()
        self.assertTrue(_has_validator(self.path_b))

    def test_helper_only_verdict_denied_on_both_paths(self):
        """The DOJO-40 class can no longer route around the gate by stage
        choice: helper/unit proof alone is denied on BOTH paths."""
        self._attach_validator(self.path_a)
        self._attach_validator(self.path_b)
        item_a = self._item_at('needs_triage')
        with self.assertRaises(WorkflowValidationError):
            WorkflowService.execute_transition(
                item_a, 'rejected', user=self.actor,
                transition_id=self.path_a.id,
            )
        item_b = self._item_at('verification')
        with self.assertRaises(WorkflowValidationError):
            WorkflowService.execute_transition(
                item_b, 'rejected', user=self.actor,
                transition_id=self.path_b.id,
            )
        item_a.refresh_from_db()
        item_b.refresh_from_db()
        self.assertEqual(item_a.status, 'needs_triage')
        self.assertEqual(item_b.status, 'verification')

    def test_compliant_verdict_passes_found_stage_path(self):
        """A verdict carrying the heading + caller surface + persisted-layer
        assertion passes the found-stage reject path (the 4-for-4 shape now
        gated, not blocked)."""
        self._attach_validator(self.path_b)
        item = self._item_at('verification')
        self._comment(item, LIVE_BOUNDARY_EVIDENCE)
        WorkflowService.execute_transition(
            item, 'rejected', user=self.actor,
            transition_id=self.path_b.id,
        )
        item.refresh_from_db()
        self.assertEqual(item.status, 'rejected')
