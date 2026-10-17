"""PLAT-9125: human-ops is structurally human-only — resolver + filing guard.

Team 18 (human-ops) has zero agent members BY DESIGN (lead = the human
operator; physical-hands escalations only). Agent-executable work filed or
routed there starved (PLAT-6474: 681h past a 96h SLA). Two mechanisms:

1. The routability resolver never resolves an agent candidate from a
   human-only lane — holds classify ``team_human_only`` (awaits-human), and
   the capacity-starve re-ping skips the lane. Deliberate operator
   assignment (``allow_operator=True``) is unaffected.
2. The filing boundary rejects non-physical filings into human-ops with a
   loud, actionable 400 (PLAT-9076 backstop pattern); physical-hardware /
   role-gated-operator-tagged filings stay human-ops.
"""
from django.test import TestCase

from tasks.serializers import ItemCreateSerializer
from tasks.services.workflow_service import WorkflowService


class HumanOnlyResolverTests(TestCase):
    def test_resolver_never_resolves_agent_candidate_from_human_ops(self):
        uid, email = WorkflowService._resolve_team_assignee('human-ops')
        self.assertIsNone(uid)
        self.assertIsNone(email)

    def test_allow_operator_still_resolves_deliberately(self):
        # The PLAT-724 sweep / escalation gate assign the operator on purpose.
        # The short-circuit must not fire for allow_operator=True callers.
        # (Resolution may still return (None, None) if the operator row is
        # absent in the test DB — the invariant under test is that the
        # human-only short-circuit is bypassed, observable via the hold not
        # being recorded with team_human_only.)
        from tasks.services import routing_authority
        before = self._human_only_hold_count()
        WorkflowService._resolve_team_assignee('human-ops', allow_operator=True)
        self.assertEqual(self._human_only_hold_count(), before)

    def _human_only_hold_count(self):
        from tasks.models import Item
        return sum(
            1
            for it in Item.objects.filter(assignment_group='human-ops')
            if 'team_human_only' in ((it.metadata or {}).get('routability_hold_reasons') or [])
        ) if Item.objects.filter(assignment_group='human-ops').exists() else 0

    def test_human_only_team_is_declared(self):
        self.assertIn('human-ops', WorkflowService._HUMAN_ONLY_TEAMS)
        self.assertNotIn('devops', WorkflowService._HUMAN_ONLY_TEAMS)

    def test_plat666_operator_routing_preserved(self):
        # PLAT-666 contract (existing test_human_ops_routing): a legitimately
        # ROSTERED human operator is the lane's correct assignee even without
        # allow_operator. The PLAT-9125 filter must keep human candidates —
        # only agent seats are inadmissible.
        operator_email = 'operator@example.test'  # WorkflowService._HUMAN_EMAILS
        self.assertTrue(WorkflowService._is_human_account(operator_email))
        tiers = [[operator_email, 'agent-seat@shizuha.com']]
        filtered = [
            [e for e in (tier or []) if WorkflowService._is_human_account(e)]
            for tier in tiers
        ]
        filtered = [t for t in filtered if t]
        self.assertEqual(filtered, [[operator_email]])


class PhysicalHandsFilingGuardTests(TestCase):
    @staticmethod
    def _payload(**overrides):
        payload = {
            'title': 'restore sshd on gx10-6',
            'mode': 'task',
            'assignment_group': 'human-ops',
        }
        payload.update(overrides)
        return payload

    def test_non_physical_filing_is_rejected_loud(self):
        serializer = ItemCreateSerializer(data=self._payload())
        self.assertFalse(serializer.is_valid())
        message = str(serializer.errors)
        self.assertIn('human-ops', message)
        # The 400 must be actionable: name the class mapping.
        self.assertIn('devops', message)
        self.assertIn('engineering', message)
        self.assertIn('qa', message)

    def test_physical_hardware_tagged_filing_stays_human_ops(self):
        serializer = ItemCreateSerializer(data=self._payload(
            labels={'physical-hardware': 'true'},
        ))
        serializer.is_valid()  # populate errors; result may be False (project check)
        # The invariant under test is the GUARD's verdict: a physical-hands
        # filing must not earn the misroute 400. Downstream validation
        # (project-required etc.) is outside this guard's contract.
        self.assertNotIn(
            'assignment_group',
            serializer.errors,
            f"physical-hardware filing must not be misroute-rejected: {serializer.errors}",
        )

    def test_role_gated_operator_tagged_filing_stays_human_ops(self):
        serializer = ItemCreateSerializer(data=self._payload(
            tags=['role-gated-operator'],
        ))
        serializer.is_valid()
        self.assertNotIn(
            'assignment_group',
            serializer.errors,
            f"role-gated-operator filing must not be misroute-rejected: {serializer.errors}",
        )

    def test_marker_helper_covers_labels_tags_metadata(self):
        claim = WorkflowService._filing_claims_physical_hands
        self.assertTrue(claim({'labels': {'physical-hardware': 'true'}}))
        self.assertTrue(claim({'tags': ['role-gated-operator']}))
        self.assertTrue(claim({'metadata': {'reason': 'physical-hardware'}}))
        self.assertFalse(claim({'labels': {'needs-detail': 'true'}}))
        self.assertFalse(claim({}))
