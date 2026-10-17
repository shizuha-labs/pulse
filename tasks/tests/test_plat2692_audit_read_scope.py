"""PLAT-2692: opt-in, least-privilege audit-read scope.

A named audit identity (the PLAT-1149 QA evidence-completeness auditor) may
BROWSE real qa_verification_sweep cadence tasks across projects/orgs —
read-only — so the audit can sample per-verification evidence without a 403.
The identity setting defaults empty (no-op until an operator opts in). These
are DB-free unit tests: the audit branch of can_access_item returns before any
org/membership lookup.
"""
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from tasks import qa_cadence
from tasks.services.permissions import PermissionService


def _item(project=True, **overrides):
    proj = SimpleNamespace(org_id=99, access_level='private') if project else None
    fields = {
        "project": proj,
        "workflow_name": "autonomous-dev",
        "assignee_id": None,
        "created_by_id": None,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _qa_cadence_item(project=True, **overrides):
    proj = SimpleNamespace(org_id=99, access_level='private') if project else None
    svc = qa_cadence.QA_TIERS["T1"]["services"][0]
    fields = qa_cadence.build_task_fields("T1", svc, proj, date(2026, 7, 4))
    fields.update({
        "project": proj,
        "assignee_id": None,
        "created_by_id": None,
    })
    fields.update(overrides)
    return SimpleNamespace(**fields)


class IsAuditReaderTests(SimpleTestCase):
    @override_settings(PULSE_AUDIT_READ_IDENTITIES=['rei@shizuha.com'])
    def test_audit_identity_and_real_qa_cadence_task(self):
        self.assertTrue(PermissionService._is_audit_reader('rei@shizuha.com', _qa_cadence_item()))
        # case-insensitive on the email
        self.assertTrue(PermissionService._is_audit_reader('Rei@Shizuha.com', _qa_cadence_item()))

    @override_settings(PULSE_AUDIT_READ_IDENTITIES=['rei@shizuha.com'])
    def test_non_audit_identity_denied(self):
        self.assertFalse(PermissionService._is_audit_reader('someone@shizuha.com', _qa_cadence_item()))

    @override_settings(PULSE_AUDIT_READ_IDENTITIES=['rei@shizuha.com'])
    def test_autonomous_dev_non_cadence_task_denied(self):
        self.assertFalse(PermissionService._is_audit_reader(
            'rei@shizuha.com',
            _item(workflow_name='autonomous-dev', source='qa_cadence_sweep',
                  item_type='task', labels=['qa-cadence']),
        ))
        self.assertFalse(PermissionService._is_audit_reader(
            'rei@shizuha.com',
            _item(workflow_name='autonomous-dev', source='manual',
                  item_type='qa.cadence', labels=['qa-cadence']),
        ))
        self.assertFalse(PermissionService._is_audit_reader(
            'rei@shizuha.com',
            _item(workflow_name='autonomous-dev', source='qa_cadence_sweep',
                  item_type='qa.cadence', labels=['not-qa-cadence']),
        ))

    @override_settings(PULSE_AUDIT_READ_IDENTITIES=[])
    def test_empty_config_is_noop(self):
        self.assertFalse(PermissionService._is_audit_reader('rei@shizuha.com', _qa_cadence_item()))

    def test_blank_email_and_missing_markers(self):
        with override_settings(PULSE_AUDIT_READ_IDENTITIES=['rei@shizuha.com']):
            self.assertFalse(PermissionService._is_audit_reader('', _qa_cadence_item()))
            self.assertFalse(PermissionService._is_audit_reader(None, _qa_cadence_item()))
            self.assertFalse(PermissionService._is_audit_reader('rei@shizuha.com', _item()))


class CanAccessItemAuditBrowseTests(SimpleTestCase):
    @override_settings(PULSE_AUDIT_READ_IDENTITIES=['rei@shizuha.com'])
    def test_audit_browse_allowed_without_org_check(self):
        with patch.object(PermissionService, 'can_access_organization') as org:
            ok = PermissionService.can_access_item(
                user_id=1234, item=_qa_cadence_item(), permission='browse',
                is_staff=False, user_email='rei@shizuha.com')
        self.assertTrue(ok)
        # The audit exception short-circuits BEFORE the cross-org membership check.
        org.assert_not_called()

    @override_settings(PULSE_AUDIT_READ_IDENTITIES=['rei@shizuha.com'])
    def test_audit_read_does_not_grant_edit(self):
        # Read-only: 'edit' must NOT be granted by the audit scope. With no
        # membership/org access, edit falls through to False.
        with patch.object(PermissionService, 'can_access_organization', return_value=True), \
                patch.object(PermissionService, 'get_user_project_membership', return_value=None):
            ok = PermissionService.can_access_item(
                user_id=1234, item=_qa_cadence_item(), permission='edit',
                is_staff=False, user_email='rei@shizuha.com')
        self.assertFalse(ok)

    @override_settings(PULSE_AUDIT_READ_IDENTITIES=[])
    def test_noop_when_unconfigured_non_member_denied(self):
        # Empty config → auditor gets the normal (denied) path for a private
        # project it isn't a member of.
        with patch.object(PermissionService, 'can_access_organization', return_value=True), \
                patch.object(PermissionService, 'get_user_project_membership', return_value=None):
            ok = PermissionService.can_access_item(
                user_id=1234, item=_qa_cadence_item(), permission='browse',
                is_staff=False, user_email='rei@shizuha.com')
        self.assertFalse(ok)
