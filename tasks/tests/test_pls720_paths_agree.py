"""PLS-720 regression: direct assignment must AGREE with the group path for the
same (item, seat) on a WIP-cap-enforced roster — not 409
assignment_validation_failed (assignee_id: wip_reservation_target_mismatch)
while the group path seats the identical person.

Root cause confirmed via live repro (PLAT-6193/6194): the PLS-509 direct
reservation in the assign view is minted against the item's PERSISTED
assignment_group, then the PLS-539 direct-override clearing (revi #313) sets
assignment_group=None before save, so _verify_wip_reservation compares
team='' to reservation.assignment_group=<team> and raises
wip_reservation_target_mismatch. The group path keeps the group, so its
reservation verifies.
"""
import uuid
from contextlib import ExitStack
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from tasks.models import Item, Project, TeamRoster

User = get_user_model()


@override_settings(ROOT_URLCONF='taskmanager.urls')
class PLS720PathsAgreeTest(TestCase):
    """Direct-with-reason and group-to-same-seat must agree on a wip_enforced
    roster (the live 'engineering' configuration that reproduced PLS-720)."""

    def setUp(self):
        self.client = APIClient()
        suffix = uuid.uuid4().hex[:8]
        self.actor = User.objects.create_user(
            username=f'pls720a{suffix}', email=f'pls720a{suffix}@shizuha.com',
            password='x', is_staff=True, is_superuser=True,
        )
        self.client.force_authenticate(self.actor)
        key = f'Q{suffix[:4].upper()}'
        self.project = Project.objects.create(
            name=f'PLS720-{suffix}', key=key, org_id=1,
            access_level='open', is_active=True,
        )
        self.target_email = f'seat{suffix}@shizuha.com'
        roster, _ = TeamRoster.objects.get_or_create(
            organization_slug='shizuha',
            team_name='engineering',
            defaults={
                'tiers': [[self.target_email]],
                'is_active': True,
                'wip_enforced': True,
            },
        )
        if (self.target_email not in str(roster.tiers)
                or not roster.is_active or not roster.wip_enforced):
            roster.tiers = [[self.target_email]]
            roster.is_active = True
            roster.wip_enforced = True
            roster.save(update_fields=['tiers', 'is_active', 'wip_enforced'])
        self.item = Item.objects.create(
            mode='task', item_type='task.bug', title='pls720',
            project=self.project, item_key=f'{key}-1',
            assignment_group='engineering', status='open',
            workflow_name='autonomous-bug',
        )

    def _assign_direct(self, item):
        return self.client.post(
            f'/api/items/{item.pk}/assign/',
            {'assignee_email': self.target_email,
             'direct_assignment_reason': 'PLS-720 containment: seat the named seat'},
            format='json',
        )

    def _assign_group(self, item):
        return self.client.post(
            f'/api/items/{item.pk}/assign/',
            {'assignment_group': 'engineering'},
            format='json',
        )

    def _patch_user(self):
        """Patch the FULL user-service surface the assign endpoint touches:
        get_user_by_email (PLS-509 reservation block), resolve_user_info
        (item.assign), get_user_by_id (assignee_id path)."""
        info = {
            'id': 4242,
            'email': self.target_email,
            'username': 'seat',
            'full_name': 'Seat',
            'is_agent': True,
        }
        return [
            patch('tasks.services.admin_service.admin_service.get_organization',
                  side_effect=lambda org_id: {'id': int(org_id), 'slug': 'shizuha'}),
            patch('tasks.services.admin_service.admin_service.is_org_member',
                  return_value=True),
            patch('tasks.user_service.user_service.get_user_by_email',
                  return_value=info),
            patch('tasks.user_service.user_service.get_user_by_id',
                  return_value=info),
            patch('tasks.user_service.user_service.resolve_user_info',
                  return_value=info),
        ]

    def test_direct_and_group_paths_agree_for_same_wip_enforced_seat(
        self,
    ):
        """Two identical wip_enforced items, same seat: direct-with-reason and
        group routing must reach the SAME verdict (both admit, or both refuse
        with the same reason) — never direct-409 while group seats it."""
        item_a = self.item
        item_b = Item.objects.create(
            mode='task', item_type='task.bug', title='pls720b',
            project=self.project, item_key=f'{self.item.item_key}b',
            assignment_group='engineering', status='open',
            workflow_name='autonomous-bug',
        )
        with ExitStack() as stack:
            for p in self._patch_user():
                stack.enter_context(p)

            direct = self._assign_direct(item_a)
            group = self._assign_group(item_b)

        direct_ok = direct.status_code in (200, 202)
        group_ok = group.status_code in (200, 202)
        if group_ok:
            self.assertTrue(
                direct_ok,
                f'PLS-720 defect: direct {direct.status_code} '
                f'{dict(getattr(direct, "data", {}) or {})} while group '
                f'seated the SAME seat ({group.status_code}).',
            )
            item_a.refresh_from_db()
            self.assertEqual(item_a.assignee_email, self.target_email)
        else:
            self.assertEqual(direct.status_code, group.status_code,
                             'both must refuse with the same verdict for one seat')
            # If both refused, both must refuse with the same reason class and
            # that class must be actionable, not an internal mismatch.
            self.assertNotEqual(
                dict(getattr(direct, 'data', {}) or {}).get('error'),
                'assignment_validation_failed',
                'internal wip_reservation_target_mismatch is not an actionable '
                'capacity/eligibility verdict',
            )

    def test_direct_assign_of_wip_enforced_seat_succeeds_with_reason(
        self,
    ):
        """Regression for the fix: direct override to an in-cap wip_enforced
        seat with a reason must seat the owner (no spurious reservation
        mismatch), preserving the direct_assignment audit breadcrumb."""
        with ExitStack() as stack:
            for p in self._patch_user():
                stack.enter_context(p)
            resp = self._assign_direct(self.item)
        self.assertIn(resp.status_code, (200, 202), resp.content)
        self.item.refresh_from_db()
        self.assertEqual(self.item.assignee_email, self.target_email)

    def test_direct_assign_with_co_supplied_group_keeps_reservation_path(
        self,
    ):
        """Regression guard: a direct override WITH a co-supplied group keeps
        the group at save (PLS-539 retained-team rule), so the PLS-509 roster
        reservation must still mint + verify against that group (unchanged
        behavior; the fix only targeted the no-team direct-override branch)."""
        with ExitStack() as stack:
            for p in self._patch_user():
                stack.enter_context(p)
            resp = self.client.post(
                f'/api/items/{self.item.pk}/assign/',
                {'assignee_email': self.target_email,
                 'assignment_group': 'engineering',
                 'direct_assignment_reason': 'PLS-720: named seat in retained team'},
                format='json',
            )
        self.assertIn(resp.status_code, (200, 202), resp.content)
        self.item.refresh_from_db()
        self.assertEqual(self.item.assignee_email, self.target_email)
        self.assertEqual(self.item.assignment_group, 'engineering')
