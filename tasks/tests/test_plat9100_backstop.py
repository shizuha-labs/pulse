"""PLAT-9100: create-time ``assignment_group`` backstop — 8-path regression set.

Merge-gate tests (rei/reika review block on pulse#589): the gate every
API/MCP create flows through must carry committed tests for all 8 resolution
paths, against the stubbed WorkflowService shape from reika's review 5168.
SimpleTestCase: the seat has no Postgres; DB-backed suites run in CI.
"""
from unittest import mock

from django.test import SimpleTestCase
from rest_framework import serializers

from tasks.services import assignment_group_backstop
from tasks.services.assignment_group_backstop import apply_create_time_backstop
from tasks.services.workflow_service import WorkflowService


def _base(**kw):
    data = {
        'title': 't', 'mode': 'task',
        'created_by_email': 'rep@shizuha.com',
        'assignee_email': '',
        'metadata': {},
    }
    data.update(kw)
    return data


class BackstopEightPathTests(SimpleTestCase):
    """Path 1 — explicit assignment_group → canonicalized."""

    def test_1_explicit_group_canonicalized(self):
        data = _base(assignment_group='Engineering')
        with mock.patch.object(WorkflowService, '_canonical_team_name',
                               return_value='engineering') as canon:
            apply_create_time_backstop(data)
        canon.assert_called_once_with('Engineering')
        self.assertEqual(data['assignment_group'], 'engineering')

    def test_1b_canonicalize_falsy_keeps_original(self):
        data = _base(assignment_group='Engineering')
        with mock.patch.object(WorkflowService, '_canonical_team_name',
                               return_value=None):
            apply_create_time_backstop(data)
        # Canonicalize returning falsy must preserve the original value,
        # never blank the group out.
        self.assertEqual(data['assignment_group'], 'Engineering')
        self.assertNotIn('assignment_group_backstop', data.get('metadata') or {})

    def test_2_derivation_resolves_and_ledgers(self):
        data = _base()
        probe = object()
        with mock.patch.object(WorkflowService, 'create_permission_probe_item',
                               return_value=probe) as mk_probe, \
             mock.patch.object(WorkflowService, '_derive_owner_team',
                               return_value='backend') as derive:
            apply_create_time_backstop(data)
        mk_probe.assert_called_once_with(data)
        derive.assert_called_once_with(probe)
        self.assertEqual(data['assignment_group'], 'backend')
        self.assertEqual(
            data['metadata']['assignment_group_backstop'], 'auto-resolved:derived')

    def test_3_derivation_infra_error_fails_open_not_loud(self):
        # Teamless, ownerless, mode=task — the loud-400 shape — but the
        # derivation raised an infra error, so the backstop downgrades to a
        # ledgered fail-open and must NOT 400.
        data = _base()
        with mock.patch.object(WorkflowService, 'create_permission_probe_item',
                               side_effect=RuntimeError('roster db down')), \
             mock.patch.object(assignment_group_backstop, '_team_of_email',
                               return_value=None):
            apply_create_time_backstop(data)  # no raise
        self.assertEqual(
            data['metadata']['assignment_group_backstop'], 'unresolved')
        self.assertNotIn('assignment_group', data)

    def test_4_reporter_team_resolution(self):
        data = _base()
        with mock.patch.object(WorkflowService, 'create_permission_probe_item',
                               return_value=object()), \
             mock.patch.object(WorkflowService, '_derive_owner_team',
                               return_value=None), \
             mock.patch.object(assignment_group_backstop, '_team_of_email',
                               return_value='qa') as team_of:
            apply_create_time_backstop(data)
        team_of.assert_called_once_with('rep@shizuha.com')
        self.assertEqual(data['assignment_group'], 'qa')
        self.assertEqual(
            data['metadata']['assignment_group_backstop'], 'auto-resolved:reporter-team')

    def test_5_assignee_team_resolution(self):
        data = _base(assignee_email='Worker@Agents.Shizuha.io')
        with mock.patch.object(WorkflowService, 'create_permission_probe_item',
                               return_value=object()), \
             mock.patch.object(WorkflowService, '_derive_owner_team',
                               return_value=None), \
             mock.patch.object(assignment_group_backstop, '_team_of_email',
                               side_effect=lambda e: 'devops' if 'worker' in e.lower() else None) as team_of:
            apply_create_time_backstop(data)
        team_of.assert_has_calls([
            mock.call('rep@shizuha.com'),        # reporter leg first
            mock.call('worker@agents.shizuha.io'),  # then assignee leg
        ])
        self.assertEqual(data['assignment_group'], 'devops')
        self.assertEqual(
            data['metadata']['assignment_group_backstop'], 'auto-resolved:assignee-team')

    def test_6_teamless_ownerless_task_loud_400(self):
        data = _base()
        with mock.patch.object(WorkflowService, 'create_permission_probe_item',
                               return_value=object()), \
             mock.patch.object(WorkflowService, '_derive_owner_team',
                               return_value=None), \
             mock.patch.object(assignment_group_backstop, '_team_of_email',
                               return_value=None):
            with self.assertRaises(serializers.ValidationError) as ctx:
                apply_create_time_backstop(data)
        msg = str(ctx.exception.detail.get('assignment_group'))
        self.assertIn('PLAT-9100', msg)
        self.assertIn('assignment_group', msg)
        self.assertNotIn('assignment_group', data)

    def test_7a_direct_assignee_ledgered_not_blocked(self):
        data = _base(assignee_email='own@shizuha.com')
        with mock.patch.object(WorkflowService, 'create_permission_probe_item',
                               return_value=object()), \
             mock.patch.object(WorkflowService, '_derive_owner_team',
                               return_value=None), \
             mock.patch.object(assignment_group_backstop, '_team_of_email',
                               return_value=None):
            apply_create_time_backstop(data)  # no raise — HIVE-1223 path
        self.assertEqual(
            data['metadata']['assignment_group_backstop'], 'owner-only-unresolved')
        self.assertNotIn('assignment_group', data)

    def test_7b_alert_mode_never_400s(self):
        data = _base(mode='alert', source='alert-router')
        with mock.patch.object(WorkflowService, 'create_permission_probe_item',
                               return_value=object()), \
             mock.patch.object(WorkflowService, '_derive_owner_team',
                               return_value=None), \
             mock.patch.object(assignment_group_backstop, '_team_of_email',
                               return_value=None):
            apply_create_time_backstop(data)  # no raise — ingestion stays open
        self.assertEqual(
            data['metadata']['assignment_group_backstop'], 'unresolved')
        self.assertNotIn('assignment_group', data)

    def test_8_unexpected_exception_fails_open(self):
        # Alert mode (no loud-fail branch reachable) + _ledger exploding →
        # the OUTER fail-open catch must log and pass through, never block
        # creation. (Task mode + teamless ownerless raises the loud-400
        # BEFORE the ledger, so alert mode is the correct trigger lane.)
        data = _base(mode='alert')
        with mock.patch.object(WorkflowService, 'create_permission_probe_item',
                               return_value=object()), \
             mock.patch.object(WorkflowService, '_derive_owner_team',
                               return_value=None), \
             mock.patch.object(assignment_group_backstop, '_team_of_email',
                               return_value=None), \
             mock.patch.object(assignment_group_backstop, '_ledger',
                               side_effect=RuntimeError('boom')):
            apply_create_time_backstop(data)  # outer fail-open: no raise
