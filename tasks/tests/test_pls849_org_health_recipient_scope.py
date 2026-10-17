"""PLS-849: org-health DM recipient selection must respect subject-org read scope.

A scope-blind recipient cannot verify or remediate a capacity-starve/org-health
signal, so the same finding re-pings indefinitely. The DM sink must intersect
the resolved recipient with the subject org's canonical membership at delivery
time, fall back to an in-org owner/admin, and fail loud (no cross-org leak)
when no authorized recipient exists. Every packet carries ``organization_id``
for provenance/audit.
"""
from __future__ import annotations

from unittest import mock

from django.test import SimpleTestCase

from tasks import tasks as tasks_mod


class _FakeResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code


class _FakeWorkflowService:
    """Minimal stand-in exposing only the resolver seam used by the DM sink."""

    def __init__(self, resolved):
        self._resolved = resolved

    def _resolve_team_assignee(self, team_name, exclude_email=None, enforce_cap=True):
        return self._resolved


class OrgHealthRecipientScopeTests(SimpleTestCase):
    """Delivery-time read-scope gate for org-health DMs (PLS-849)."""

    def setUp(self):
        self.ws = _FakeWorkflowService(resolved=(101, 'pm-lead@example.com'))

    def _deliver(self, *, organization_id=1, subject_key='engineering', resolved=None):
        if resolved is not None:
            self.ws._resolved = resolved
        with mock.patch.object(tasks_mod, '_resolve_in_org_owner', return_value=(None, None)) as owner:
            with mock.patch('requests.post', return_value=_FakeResponse(200)) as post:
                ok = tasks_mod._dm_program_management_lead(
                    self.ws,
                    '🩺 [org-health] test signal',
                    organization_id=organization_id,
                    subject_key=subject_key,
                )
        return ok, post, owner

    def test_authorized_recipient_receives_exactly_one_dm_with_org_provenance(self):
        """Least-loaded PM lead with subject-org read scope gets the DM."""
        with mock.patch('tasks.services.admin_service.admin_service.is_org_member', return_value=True) as scope:
            ok, post, owner = self._deliver(organization_id=7, subject_key='finance')
        self.assertTrue(ok)
        self.assertTrue(scope.called)
        self.assertEqual(post.call_count, 1)
        body = post.call_args.kwargs['json']
        self.assertEqual(body['recipient_user_id'], 101)
        self.assertEqual(body['organization_id'], 7)
        self.assertEqual(body['subject_key'], 'finance')
        owner.assert_not_called()

    def test_scope_blind_recipient_is_excluded_and_in_org_owner_receives_dm(self):
        """Two-org fixture: least-loaded seat lacks scope; in-org owner receives it."""
        with mock.patch('tasks.services.admin_service.admin_service.is_org_member', return_value=False):
            with mock.patch.object(
                tasks_mod, '_resolve_in_org_owner', return_value=(202, 'owner@example.com')
            ) as owner:
                with mock.patch('requests.post', return_value=_FakeResponse(200)) as post:
                    ok = tasks_mod._dm_program_management_lead(
                        self.ws,
                        '🩺 [org-health] test signal',
                        organization_id=7,
                        subject_key='finance',
                    )
        self.assertTrue(ok)
        owner.assert_called_once()
        self.assertEqual(post.call_count, 1)
        body = post.call_args.kwargs['json']
        self.assertEqual(body['recipient_user_id'], 202)
        self.assertEqual(body['organization_id'], 7)

    def test_zero_authorized_seat_fails_loud_without_cross_org_dm(self):
        """No authorized recipient: no DM is sent; fail-loud (False) releases retry."""
        with mock.patch('tasks.services.admin_service.admin_service.is_org_member', return_value=False):
            with mock.patch.object(tasks_mod, '_resolve_in_org_owner', return_value=(None, None)):
                with mock.patch('requests.post', return_value=_FakeResponse(200)) as post:
                    ok = tasks_mod._dm_program_management_lead(
                        self.ws,
                        '🩺 [org-health] test signal',
                        organization_id=7,
                        subject_key='finance',
                    )
        self.assertFalse(ok)
        post.assert_not_called()

    def test_no_org_scope_skips_acl_check(self):
        """org 0 / missing org has no scoped subject org; legacy behavior preserved."""
        with mock.patch('tasks.services.admin_service.admin_service.is_org_member') as scope:
            ok, post, owner = self._deliver(organization_id=0, subject_key='')
        self.assertTrue(ok)
        scope.assert_not_called()
        owner.assert_not_called()
        self.assertEqual(post.call_count, 1)
        self.assertNotIn('organization_id', post.call_args.kwargs['json'])

    def test_signal_org_parses_subject_org_from_key(self):
        self.assertEqual(tasks_mod._signal_org('capacity-starve:1:engineering'), 1)
        self.assertEqual(tasks_mod._signal_org('capacity-starve:42:merge'), 42)
        self.assertIsNone(tasks_mod._signal_org('throughput-flatline'))
        self.assertIsNone(tasks_mod._signal_org('capacity-starve:qa:merge'))

    def test_emit_org_health_signal_infers_org_and_threads_provenance(self):
        """Packet provenance: organization_id inferred from key and carried through."""
        from django.utils import timezone
        with mock.patch.object(tasks_mod, '_dm_program_management_lead', return_value=True) as dm:
            ok = tasks_mod.emit_org_health_signal(
                'capacity-starve:3:engineering',
                {'title': 't', 'body': 'b'},
                workflow_service=self.ws,
                now=timezone.now(),
                redis_client=None,
                reping_seconds=21600,
            )
        self.assertTrue(ok)
        self.assertEqual(dm.call_count, 1)
        kwargs = dm.call_args.kwargs
        self.assertEqual(kwargs['organization_id'], 3)
        self.assertEqual(kwargs['subject_key'], 'engineering')
