"""PLAT-8222: the rebalance sweep must respect capability-gated demand.

Demand side (``item_required_capabilities``): an item's high-risk labels map to
capability tags via ``HIGH_RISK_LABEL_CAPABILITY_MAP``; unmapped vocabulary is
deliberately un-gated so a new label can never freeze routing.

Supply side (``seat_team_satisfies``): PLS-198 fail-open on profile-less teams
(the registry is opt-in); a profiled team must hold every required capability
against the union of its profile row and the hardcoded baseline, with slugs
normalized on both sides and team lookup resolved iexact (matching the sweep's
``assignment_group__iexact`` convention).
"""
from django.test import TestCase

from tasks.models import TeamCapabilityProfile
from tasks.services import focused_qa
from tasks.services.focused_qa import (
    HIGH_RISK_LABEL_CAPABILITY_MAP,
    item_required_capabilities,
    seat_team_satisfies,
)
from tasks.services.workflow_service import WorkflowService


class _FakeItem:
    """Minimal item stand-in: risk_labels_for_item only reads labels/tags."""

    def __init__(self, labels=None, tags=None):
        self.labels = labels
        self.tags = tags


class ItemRequiredCapabilitiesTests(TestCase):
    def test_mapped_labels_union_their_capabilities(self):
        item = _FakeItem(labels='requires-live-qa,auth')
        self.assertEqual(
            item_required_capabilities(item), {'live-qa', 'security-review'},
        )

    def test_tags_field_is_scanned_too(self):
        item = _FakeItem(tags='infra-control-plane')
        self.assertEqual(item_required_capabilities(item), {'cluster-exec'})

    def test_unmapped_high_risk_label_is_ungated(self):
        # A high-risk label with no mapping entry contributes nothing — the
        # gate must never freeze routing on unmapped vocabulary.
        item = _FakeItem(labels='requires-live-qa,brand-new-risk-label')
        self.assertEqual(item_required_capabilities(item), {'live-qa'})

    def test_no_risk_labels_means_no_demand(self):
        self.assertEqual(item_required_capabilities(_FakeItem(labels='docs,bug')), set())
        self.assertEqual(item_required_capabilities(_FakeItem()), set())

    def test_map_covers_the_documented_defect_vocabulary(self):
        # The carriers named in the defect (ORIG-290, SCS-166, HIVE-2364…)
        # must all be gated.
        for label in (
            'requires-live-qa', 'infra-control-plane',
            'security-sensitive', 'auth',
        ):
            self.assertIn(label, HIGH_RISK_LABEL_CAPABILITY_MAP, label)


class SeatTeamSatisfiesTests(TestCase):
    def setUp(self):
        TeamCapabilityProfile.objects.all().delete()
        self.addCleanup(TeamCapabilityProfile.objects.all().delete)

    def test_fail_open_on_profileless_team(self):
        # PLS-198: the registry is opt-in; a team with no profile row is
        # un-gated (gating profile-less teams would freeze routing fleet-wide).
        self.assertTrue(seat_team_satisfies('no-such-team', {'live-qa'}))

    def test_profiled_team_without_capability_refuses(self):
        TeamCapabilityProfile.objects.create(
            team_name='docs-pod', capabilities=['docs-review'],
        )
        self.assertFalse(seat_team_satisfies('docs-pod', {'live-qa'}))

    def test_profiled_team_with_capability_admits(self):
        TeamCapabilityProfile.objects.create(
            team_name='qa-pod', capabilities=['live-qa', 'replay-inspection'],
        )
        self.assertTrue(seat_team_satisfies('qa-pod', {'live-qa'}))
        self.assertTrue(
            seat_team_satisfies('qa-pod', {'live-qa', 'replay-inspection'}),
        )

    def test_all_required_must_be_held(self):
        TeamCapabilityProfile.objects.create(
            team_name='half-pod', capabilities=['live-qa'],
        )
        self.assertFalse(
            seat_team_satisfies('half-pod', {'live-qa', 'cluster-exec'}),
        )

    def test_baseline_conferred_capability_is_not_dropped_by_profile_row(self):
        # 'security' confers security-review via the hardcoded baseline; a
        # profile row that omits it must not drop the baseline grant.
        TeamCapabilityProfile.objects.create(
            team_name='security', capabilities=['something-else'],
        )
        self.assertTrue(seat_team_satisfies('security', {'security-review'}))

    def test_capability_slugs_normalize_on_both_sides(self):
        TeamCapabilityProfile.objects.create(
            team_name='ops-pod', capabilities=['host_exec'],
        )
        self.assertTrue(seat_team_satisfies('ops-pod', {'host-exec'}))

    def test_empty_required_admits_everything(self):
        self.assertTrue(seat_team_satisfies('any-team', set()))
        self.assertTrue(seat_team_satisfies('any-team', None))

    def test_profileless_lookup_is_case_insensitive_on_team_name(self):
        # The sweep matches assignment_group iexact (PLAT-916 group_q), so the
        # profile check must resolve the same way.
        TeamCapabilityProfile.objects.create(
            team_name='Merge', capabilities=['repo-write'],
        )
        self.assertTrue(seat_team_satisfies('merge', {'repo-write'}))
        self.assertTrue(seat_team_satisfies('MERGE', {'repo-write'}))


class ReturnToLaneEndpointTests(TestCase):
    """PLAT-9119: the sanctioned cannot-execute return must clear the owner
    through the REAL guarded group-queue command — end to end.

    Live defect (haru ANDON 2026-09-18): every POST /return-to-lane/ 500'd
    because the view called ``item.unassign(reason='cannot_execute_return')``
    while ``CLEAR_ASSIGNMENT_REASONS`` did not contain that reason — the
    guarded clear command raised ``assignment_clear_reason_invalid`` before
    any team lookup, unhandled in the view. The capability-gate tests above
    never exercised the endpoint, so the drift survived CI. This test drives
    the real endpoint (no unassign mocking) so reason-set drift fails here.
    """

    def _fixture(self):
        from django.contrib.auth import get_user_model
        from rest_framework.test import APIClient
        from tasks.models import Item, Project, TeamRoster

        User = get_user_model()
        user = User.objects.create(
            username='haru-return-lane',
            email='haru-return-lane@shizuha.com',
        )
        # The Item.save roster-authorization guard requires the assignee to be
        # in the task organization's target team roster — register the lane
        # (with the assignee in tiers) BEFORE the item exists.
        TeamRoster.objects.update_or_create(
            organization_id=1,
            team_name='engineering',
            defaults={
                'organization_slug': 'shizuha',
                'tiers': [['haru-return-lane@shizuha.com']],
                'is_active': True,
            },
        )
        project = Project.objects.create(org_id=1, name='Return Lane', key='RLN')
        item = Item.objects.create(
            # item_key is minted by the serializer path (Project.get_next_item_key),
            # not by raw create — set it explicitly or the URL resolves to /items/None/.
            item_key='RLN-1',
            title='wedged awaiting_deploy carrier',
            mode='task',
            status='awaiting_deploy',
            priority='high',
            assignment_group='engineering',
            workflow_name='autonomous-dev',
            project=project,
            assignee_id=user.id,
            assignee_email=user.email,
        )
        client = APIClient()
        client.force_authenticate(user=user)
        return client, item, user

    def test_return_to_lane_clears_owner_through_guarded_command(self):
        client, item, user = self._fixture()
        response = client.post(
            f'/api/items/{item.item_key}/return-to-lane/',
            {
                'reason': 'awaiting_deploy carrier is not self-closable from this seat',
                'reason_class': 'access-blocked',
            },
            format='json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        item.refresh_from_db()
        self.assertIsNone(item.assignee_id)
        self.assertIsNone(item.assignee_email)
        # The lane keeps the item: same-lane group-queue HOLD, not a drop.
        self.assertEqual(item.assignment_group, 'engineering')
        returns = (item.metadata or {}).get('cannot_execute_returns') or []
        self.assertEqual(len(returns), 1)
        self.assertEqual(returns[0]['reason_class'], 'access-blocked')

    def test_return_to_lane_unresolvable_key_is_typed_404_not_500(self):
        # PLS-1361 (live defect, shion ANDON evidence): item 34519/HIVE-2523
        # returned HTTP 500 three times across three days on the same call.
        # _resolve_item RAISES Item.DoesNotExist for unresolvable ids — it
        # never returns None — so the view's old `is None` branch was dead
        # and DRF turned the raise into the bare structured-500 envelope.
        # The seat retried after the suggested 30s backoff and read the same
        # 500 each day. A wrong id must be a typed 404 the seat can act on.
        client, _item, _user = self._fixture()
        response = client.post(
            '/api/items/PHANTOM-999999/return-to-lane/',
            {
                'reason': 'seat cannot execute: probe the route with a bad key',
                'reason_class': 'capability-mismatch',
            },
            format='json',
        )
        self.assertEqual(response.status_code, 404, response.content)
        self.assertIn('not found', response.json()['error'])

    def test_return_to_lane_unresolvable_numeric_id_is_typed_404_not_500(self):
        # Same PLS-1361 face through the numeric-id branch of _resolve_item.
        client, _item, _user = self._fixture()
        response = client.post(
            '/api/items/999999999/return-to-lane/',
            {
                'reason': 'seat cannot execute: probe the route with a bad id',
                'reason_class': 'access-blocked',
            },
            format='json',
        )
        self.assertEqual(response.status_code, 404, response.content)

    def test_return_to_lane_honors_destination_group(self):
        client, item, user = self._fixture()
        response = client.post(
            f'/api/items/{item.item_key}/return-to-lane/',
            {
                'reason': 'this work belongs to the devops lane, not engineering',
                'reason_class': 'wrong-lane',
                'destination_group': 'engineering',
            },
            format='json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        item.refresh_from_db()
        self.assertIsNone(item.assignee_id)
        self.assertIsNone(item.assignee_email)
