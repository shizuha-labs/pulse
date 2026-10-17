"""PLS-728: the internal project-memberships projection must fail closed on
unknown org membership and distinguish a genuine empty from every failure mode.

``/api/internal/users/<id>/project-memberships/`` is the authoritative producer
of a value that becomes a 15-minute JWT authorization claim (``project_ids`` /
``project_roles``). The org-membership filter is authorization-relevant: a row
dropped there disappears from the claim. The old path used the non-strict
``identity_belongs_to_organization``, which reads an unreachable shizuha-admin
as "not a member" (``_make_request`` returns ``None`` on timeout/error) and
serves a short/empty projection with 200 — a confident wrong answer, the same
failure class PLAT-5338 was.

These tests pin the new contract:
- unknown org membership → typed 503 with NO claim keys (fail closed);
- genuine empty → 200 empty, counted ``authoritative_empty``;
- non-empty → 200, counted ``served``;
- the strict check propagates admin unavailability as ProjectMembershipUnavailable;
- the /api/metrics/ family is always rendered (zero-filled) for the absent()
  detector.
"""
from unittest.mock import patch

from django.urls import reverse
from rest_framework.test import APITestCase

from tasks.models import Project, ProjectMembership
from tasks.services.admin_service import AdminServiceUnavailable
from tasks.services.permissions import PermissionService, ProjectMembershipUnavailable
from tasks.services.project_membership_metrics import (
    OUTCOMES,
    count_for,
    record_projection_outcome,
    render_project_membership_metrics,
)


class PLS728ProjectMembershipFailClosedTests(APITestCase):
    def setUp(self):
        self.project = Project.objects.create(
            name='Evolution', key='EVOL', org_id=1, access_level='private', is_active=True,
        )
        self.url = reverse(
            'internal-user-project-memberships',
            kwargs={'user_id': 4242},
        )

    def _seed(self, user_id=4242, email='zen@shizuha.com', role='member', project=None):
        return ProjectMembership.objects.create(
            project=project or self.project,
            user_id=user_id,
            user_email=email,
            role=role,
            is_active=True,
        )

    def test_unknown_org_membership_fails_closed_with_no_claim_keys(self):
        """Admin unreachable → typed 503, no project_memberships/project_ids."""
        self._seed()
        with patch(
            'tasks.services.permissions.PermissionService.identity_belongs_to_organization_strict',
            side_effect=ProjectMembershipUnavailable('admin timeout'),
        ):
            resp = self.client.get(self.url, {'email': 'zen@shizuha.com'})
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.data['error'], 'project_membership_unavailable')
        self.assertNotIn('project_memberships', resp.data)
        self.assertNotIn('project_ids', resp.data)
        self.assertEqual(resp['Retry-After'], '1')

    def test_authoritative_empty_is_served_and_counted(self):
        """No memberships → 200 empty, recorded as authoritative_empty."""
        with patch(
            'tasks.services.permissions.PermissionService.identity_belongs_to_organization_strict',
            return_value=True,
        ):
            resp = self.client.get(self.url, {'email': 'zen@shizuha.com'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['project_ids'], [])
        self.assertEqual(resp.data['project_memberships'], {})

    def test_non_empty_projection_is_served(self):
        """Membership in an org the user belongs to → 200 with the claim shape."""
        self._seed()
        with patch(
            'tasks.services.permissions.PermissionService.identity_belongs_to_organization_strict',
            return_value=True,
        ):
            resp = self.client.get(self.url, {'email': 'zen@shizuha.com'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['project_ids'], [self.project.id])
        self.assertEqual(
            resp.data['project_memberships'][str(self.project.id)],
            'member',
        )

    def test_strict_check_propagates_admin_unavailability(self):
        """AdminServiceUnavailable surfaces as ProjectMembershipUnavailable."""
        with patch(
            'tasks.services.admin_service.admin_service.is_org_member_strict',
            side_effect=AdminServiceUnavailable('admin timeout'),
        ):
            with self.assertRaises(ProjectMembershipUnavailable):
                PermissionService.identity_belongs_to_organization_strict(
                    4242, 1, 'zen@shizuha.com'
                )

    def test_metrics_family_always_renders_all_outcomes(self):
        """The /api/metrics/ family is zero-filled so absent() can detect a rename."""
        before = count_for('served')
        record_projection_outcome('served')
        body = render_project_membership_metrics()
        for outcome in OUTCOMES:
            self.assertIn(
                f'pulse_project_membership_projection_total{{outcome="{outcome}"}}',
                body,
            )
        self.assertEqual(count_for('served'), before + 1)

    def test_unknown_outcome_is_rejected(self):
        """Only fixed, source-controlled outcome values are allowed."""
        with self.assertRaises(ValueError):
            record_projection_outcome('not-a-real-outcome')
