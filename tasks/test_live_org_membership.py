"""A just-created organization is usable before the access token is re-minted."""
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase
from rest_framework.exceptions import PermissionDenied

from tasks.authentication import merge_live_membership
from tasks.views import _ensure_explicit_org_membership


class LiveOrgMembershipTests(SimpleTestCase):
    def test_missing_org_is_filled_from_admin_once(self):
        user = SimpleNamespace(
            id=1001,
            email='csraunakbansal@gmail.com',
            is_staff=False,
            is_superuser=False,
            organization_memberships={909: 'owner'},
        )
        with patch(
            'tasks.authentication._fetch_memberships_live',
            return_value={909: 'owner', 50: 'owner'},
        ) as fetch:
            role = merge_live_membership(user, 50)
            again = merge_live_membership(user, 77)
        self.assertEqual(role, 'owner')
        self.assertIsNone(again)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(user.organization_memberships[50], 'owner')

    def test_create_accepts_an_org_confirmed_live(self):
        user = SimpleNamespace(
            id=1001,
            email='csraunakbansal@gmail.com',
            is_staff=False,
            is_superuser=False,
            organization_memberships={909: 'owner'},
        )
        request = SimpleNamespace(
            headers={'X-Organization-ID': '50'},
            query_params={},
            user=user,
        )
        with patch(
            'tasks.authentication._fetch_memberships_live',
            return_value={50: 'owner'},
        ):
            self.assertEqual(_ensure_explicit_org_membership(request), 50)

    def test_create_still_rejects_an_org_admin_does_not_confirm(self):
        user = SimpleNamespace(
            id=1001,
            email='csraunakbansal@gmail.com',
            is_staff=False,
            is_superuser=False,
            organization_memberships={909: 'owner'},
        )
        request = SimpleNamespace(
            headers={'X-Organization-ID': '3'},
            query_params={},
            user=user,
        )
        with patch(
            'tasks.authentication._fetch_memberships_live',
            return_value={909: 'owner'},
        ):
            with self.assertRaises(PermissionDenied):
                _ensure_explicit_org_membership(request)
