"""HIVE-1153 P1 #3 (HLD PLS-277 v20 §5.2, aoi's Option-B ruling): the model-side
authorizing workflow-policy envelope + its content-derived digest.

The security-load-bearing invariant: a DISPLAY-only edit (name/description/
knowledge/screen) keeps `workflow_policy_version` STABLE, while ANY authorizing/
assignment-relevant edit (topology, role/assignee/reporter gate, validator,
condition, post-function, membership) ROTATES it — so signed evidence bound to an
old policy can never authorize under a changed one, and a cosmetic edit never
needlessly invalidates fresh evidence.
"""
from django.test import TestCase

from tasks.models import Status, Workflow, WorkflowStatus, WorkflowTransition
from tasks.services import capability_evidence as ce
from tasks.services.workflow_service import WorkflowService


class WorkflowPolicyEnvelopeTests(TestCase):
    def setUp(self):
        self.open = Status.objects.create(name='WPE Open', slug='wpe_open', category='todo')
        self.prog = Status.objects.create(name='WPE Prog', slug='wpe_prog', category='in_progress')
        self.done = Status.objects.create(name='WPE Done', slug='wpe_done', category='done')
        self.wf = Workflow.objects.create(
            name='Sample WF', slug='sample-wf', initial_status=self.open)
        for pos, st in enumerate((self.open, self.prog, self.done)):
            WorkflowStatus.objects.create(workflow=self.wf, status=st, position=pos)
        self.t = WorkflowTransition.objects.create(
            workflow=self.wf, name='Start', from_status='wpe_open', to_status='wpe_prog',
            assignee_only=True, allowed_roles=['member'])

    def _digest(self):
        self.wf.refresh_from_db()
        return ce.workflow_policy_version(WorkflowService._workflow_policy_envelope(self.wf))

    def test_digest_is_content_addressed_sha256(self):
        d = self._digest()
        self.assertRegex(d, r'^sha256:[0-9a-f]{64}$')
        self.assertEqual(d, self._digest())  # deterministic

    # --- display-only edits keep the digest STABLE ---
    def test_workflow_display_edits_are_stable(self):
        base = self._digest()
        self.wf.name = 'Totally Renamed Workflow'
        self.wf.description = 'new docs that do not affect authority'
        self.wf.knowledge_base = [{'type': 'inline', 'title': 'x', 'content': 'y'}]
        self.wf.save()
        self.assertEqual(base, self._digest())

    def test_transition_display_edits_are_stable(self):
        base = self._digest()
        self.t.name = 'Renamed Transition'
        self.t.description = 'cosmetic'
        self.t.button_style = 'danger'
        self.t.knowledge_articles = [{'type': 'inline', 'title': 'a', 'content': 'b'}]
        self.t.position = 99
        self.t.save()
        self.assertEqual(base, self._digest())

    # --- authorizing edits ROTATE the digest ---
    def test_screen_fields_change_rotates(self):
        # HLD v20 §5.2: required screen semantics are authorization-relevant.
        base = self._digest()
        self.t.screen_fields = [{'field': 'comment', 'required': True}]
        self.t.save()
        self.assertNotEqual(base, self._digest())
    def test_assignee_only_gate_change_rotates(self):
        base = self._digest()
        self.t.assignee_only = False
        self.t.save()
        self.assertNotEqual(base, self._digest())

    def test_allowed_roles_change_rotates(self):
        base = self._digest()
        self.t.allowed_roles = ['admin']
        self.t.save()
        self.assertNotEqual(base, self._digest())

    def test_topology_change_rotates(self):
        base = self._digest()
        self.t.to_status = 'wpe_done'
        self.t.save()
        self.assertNotEqual(base, self._digest())

    def test_validator_change_rotates(self):
        base = self._digest()
        self.t.validators = [{'type': 'field_required', 'field': 'assignee_id'}]
        self.t.save()
        self.assertNotEqual(base, self._digest())

    def test_post_function_change_rotates(self):
        base = self._digest()
        self.t.post_functions = [{'type': 'assign_to', 'target': 'project_lead'}]
        self.t.save()
        self.assertNotEqual(base, self._digest())

    def test_new_transition_rotates(self):
        base = self._digest()
        WorkflowTransition.objects.create(
            workflow=self.wf, name='Finish', from_status='wpe_prog', to_status='wpe_done')
        self.assertNotEqual(base, self._digest())

    def test_membership_change_rotates(self):
        base = self._digest()
        extra = Status.objects.create(name='WPE Blocked', slug='wpe_blocked', category='in_progress')
        WorkflowStatus.objects.create(workflow=self.wf, status=extra, position=5)
        self.assertNotEqual(base, self._digest())

    def test_initial_stage_change_rotates(self):
        base = self._digest()
        self.wf.initial_status = self.prog
        self.wf.save()
        self.assertNotEqual(base, self._digest())

    # --- rk2 key builds end-to-end for an item ---
    def test_requirement_key_v2_builds_for_workflow(self):
        rk = WorkflowService._requirement_key_v2_for_item(
            type('I', (), {'workflow_name': 'sample-wf', 'status': 'wpe_prog',
                           'workflow_status': None})(),
            org_id=1, intent='assign')
        self.assertRegex(
            rk,
            r'^rk2:org=1;wf=[0-9a-f-]{36};pol=sha256:[0-9a-f]{64};'
            r'stage=[0-9a-f-]{36};intent=assign$')
        # stage UUID is the in_progress WorkflowStatus membership id
        ws = WorkflowStatus.objects.get(workflow=self.wf, status=self.prog)
        self.assertIn(f'stage={ws.id}', rk)
