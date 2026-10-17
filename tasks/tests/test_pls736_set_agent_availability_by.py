"""PLS-736 regression: set_agent_availability must not silently stamp the sacred
``updated_by='operator'`` value, and must reject contradictory flag/--status
combinations instead of resolving them fail-open.

Contract (from the ticket):
1. A write WITHOUT ``--by`` must FAIL (no silent ``operator`` default that makes
   the row permanently exempt from automated recovery).
2. ``updated_by='operator'`` is only ever written when the caller passes
   ``--by operator`` explicitly and deliberately.
3. ``--inactive --status active`` (and ``--active --status inactive|on_leave|
   disabled``) must be REJECTED, not resolved to ACTIVE / a mismatched row.
4. Normal explicit invocations still write correctly.
"""
from io import StringIO

from django.core.management import call_command, CommandError
from django.test import TestCase

from tasks.models import AgentAvailability


class SetAgentAvailabilityNoByGuardTest(TestCase):
    def test_without_by_fails_and_writes_nothing(self):
        """The core PLS-736 regression: no --by → error, and NO row stamped
        with the sacred updated_by='operator' (the ticket's acceptance #1/#3)."""
        out = StringIO()
        with self.assertRaises(CommandError) as ctx:
            call_command(
                'set_agent_availability', 'kai@shizuha.com', '--inactive',
                '--reason', 'test clear', stdout=out,
            )
        self.assertIn('--by', str(ctx.exception))
        # No row was created with the sacred owner…
        self.assertFalse(AgentAvailability.objects.filter(
            email='kai@shizuha.com', updated_by__iexact='operator').exists())
        # …or with ANY owner (nothing written at all).
        self.assertFalse(AgentAvailability.objects.filter(email='kai@shizuha.com').exists())

    def test_explicit_by_operator_is_the_only_path_to_sacred_row(self):
        """Acceptance #2: operator stamping requires an explicit deliberate
        argument."""
        out = StringIO()
        call_command(
            'set_agent_availability', 'kai@shizuha.com', '--inactive',
            '--by', 'operator', '--reason', 'deliberate force off',
            stdout=out,
        )
        row = AgentAvailability.objects.get(email='kai@shizuha.com')
        self.assertFalse(row.active)
        self.assertEqual(row.updated_by, 'operator')

    def test_explicit_non_operator_by_writes_normal_recoverable_row(self):
        out = StringIO()
        call_command(
            'set_agent_availability', 'san@shizuha.com', '--inactive',
            '--by', 'pls734-manual-clear', '--reason', 'runbook clear',
            stdout=out,
        )
        row = AgentAvailability.objects.get(email='san@shizuha.com')
        self.assertFalse(row.active)
        self.assertEqual(row.updated_by, 'pls734-manual-clear')
        # Recovery path treats non-operator rows as auto-restorable: assert the
        # row does NOT match the sacred-exemption predicate.
        self.assertFalse(AgentAvailability.objects.filter(
            email='san@shizuha.com', updated_by__iexact='operator').exists())

    def test_inactive_with_status_active_is_rejected_fail_closed(self):
        """The ticket-title contradiction: --inactive --status active must not
        mark the agent ACTIVE."""
        out = StringIO()
        with self.assertRaises(CommandError) as ctx:
            call_command(
                'set_agent_availability', 'ryo@shizuha.com', '--inactive',
                '--status', 'active', '--by', 'pls736-test', stdout=out,
            )
        self.assertIn('contradicts', str(ctx.exception))
        self.assertFalse(AgentAvailability.objects.filter(email='ryo@shizuha.com').exists())

    def test_active_with_status_inactive_is_rejected(self):
        out = StringIO()
        with self.assertRaises(CommandError) as ctx:
            call_command(
                'set_agent_availability', 'ryo@shizuha.com', '--active',
                '--status', 'inactive', '--by', 'pls736-test', stdout=out,
            )
        self.assertIn('contradicts', str(ctx.exception))
        self.assertFalse(AgentAvailability.objects.filter(email='ryo@shizuha.com').exists())

    def test_consistent_combo_still_writes(self):
        """--inactive --status on_leave agrees and must still work."""
        out = StringIO()
        call_command(
            'set_agent_availability', 'nagi@shizuha.com', '--inactive',
            '--status', 'on_leave', '--by', 'pls736-test',
            '--reason', 'leave', stdout=out,
        )
        row = AgentAvailability.objects.get(email='nagi@shizuha.com')
        self.assertFalse(row.active)
        self.assertEqual(row.status, 'on_leave')
        self.assertEqual(row.updated_by, 'pls736-test')

    def test_active_flag_with_status_active_agrees(self):
        out = StringIO()
        call_command(
            'set_agent_availability', 'sara@shizuha.com', '--active',
            '--status', 'active', '--by', 'pls736-test', stdout=out,
        )
        row = AgentAvailability.objects.get(email='sara@shizuha.com')
        self.assertTrue(row.active)
        self.assertEqual(row.status, 'active')
        self.assertEqual(row.updated_by, 'pls736-test')
