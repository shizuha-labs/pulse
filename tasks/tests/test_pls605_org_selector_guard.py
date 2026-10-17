"""PLS-605 regression: an unvalidated org selector reaching the ORM.

Two routes read the `organization` query parameter and handed the raw string to
`.filter(org_id=...)`, so a non-numeric value reached an integer column and
raised. Both were reported/measured live on 2026-07-21:

1. `/api/projects/?organization=abc` -> HTTP 500, wrapped by PLS-358's fail-loud
   handler as `{"code": "projects_unexpected_error"}`.
2. `/api/objectives/?organization=abc` -> HTTP 500 with a GENERIC `server_error`
   body and no code, because `ObjectiveViewSet` has no equivalent wrapper. This
   twin is strictly harder to find than the original: it produces an anonymous
   500 indistinguishable from any other internal error, so no log search by
   error code would ever surface it.

The same line carried a second defect on `projects`: it honoured ONLY
`?organization=`, silently ignoring the documented `X-Organization-ID` header
and `?organization_id=`. A caller using the documented header believed it had
scoped the request and received another org's projects with a 200.

Both are closed by adopting `_explicit_org_id`, which resolves all three
documented selectors and fails closed on an unparseable value. Per PLS-670 /
ADR-0054 the malformed-selector status is 400 (client error), distinct from
403 for a well-formed org the caller may not access.

Deliberately asserted here and worth stating so nobody "tightens" it later: a
well-formed selector naming an org the caller is NOT a member of still returns
200-with-zero-rows rather than 403. `PermissionService` has already scoped the
queryset, so nothing leaks; 403 parity with items/boards belongs to the uniform
PLS-439 selector port rather than to these two routes ahead of the rest.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from tasks.models import Objective, Project


MALFORMED = 'abc'


class PLS605OrgSelectorGuardTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username='pls605', email='pls605@shizuha.com',
            password='testpass123', is_staff=False,
        )
        # Org membership claims as the JWT auth layer would attach them, so the
        # org-scoped reads below are meaningful rather than trivially empty.
        self.user.organization_memberships = {7: 'member'}
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

        self.project = Project.objects.create(
            name='Pulse', key='PLS605', org_id=7, access_level='open',
        )
        Objective.objects.create(
            title='PLS-605 objective', org_id=7, project=self.project,
        )

    # --- the reported crash, both routes -------------------------------------

    def test_malformed_selector_does_not_reach_the_orm_on_projects(self):
        response = self.client.get(f'/api/projects/?organization={MALFORMED}')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertNotEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)

    def test_malformed_selector_does_not_reach_the_orm_on_objectives(self):
        # The twin. Before the fix this raised ValueError out of get_queryset and
        # surfaced as an UNLABELLED 500 — the reason it stayed invisible.
        response = self.client.get(f'/api/objectives/?organization={MALFORMED}')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertNotEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)

    def test_malformed_value_is_rejected_through_every_documented_selector(self):
        """A guard on one selector spelling is not a guard."""
        cases = (
            ('query organization', {}, f'?organization={MALFORMED}'),
            ('query organization_id', {}, f'?organization_id={MALFORMED}'),
            ('header', {'HTTP_X_ORGANIZATION_ID': MALFORMED}, ''),
        )
        for route in ('/api/projects/', '/api/objectives/'):
            for label, headers, query in cases:
                with self.subTest(route=route, selector=label):
                    response = self.client.get(f'{route}{query}', **headers)
                    self.assertEqual(
                        response.status_code, status.HTTP_400_BAD_REQUEST,
                        f'{route} via {label} should reject a malformed selector '
                        f'with 400, not {response.status_code}',
                    )

    # --- the silent-ignore half ----------------------------------------------

    def test_all_three_documented_selectors_are_honoured(self):
        """`X-Organization-ID` and `?organization_id=` were silently ignored.

        A non-member org must narrow to zero rows through EVERY documented
        selector. Before the fix the header and `organization_id` spellings were
        not read at all on `projects`, so this returned the caller's full
        unnarrowed list with a 200 — scoped in the caller's belief only.
        """
        cases = (
            ('query organization', {}, '?organization=999'),
            ('query organization_id', {}, '?organization_id=999'),
            ('header', {'HTTP_X_ORGANIZATION_ID': '999'}, ''),
        )
        for route in ('/api/projects/', '/api/objectives/'):
            for label, headers, query in cases:
                with self.subTest(route=route, selector=label):
                    response = self.client.get(f'{route}{query}', **headers)
                    self.assertEqual(response.status_code, status.HTTP_200_OK)
                    self.assertEqual(
                        self._rows(response), 0,
                        f'{route} via {label} returned rows for a non-member org',
                    )

    def test_well_formed_member_org_still_narrows_and_returns_rows(self):
        """The positive control.

        Every assertion above is satisfied by a route that returns nothing at
        all, so without this one the suite would pass against a totally broken
        endpoint. It also pins the deliberate 200-empty (not 403) posture for a
        well-formed non-member org, which PLS-605's acceptance requires.
        """
        for route in ('/api/projects/', '/api/objectives/'):
            with self.subTest(route=route):
                response = self.client.get(f'{route}?organization=7')
                self.assertEqual(response.status_code, status.HTTP_200_OK)
                self.assertEqual(self._rows(response), 1)

    @staticmethod
    def _rows(response):
        data = response.data
        if isinstance(data, dict) and 'results' in data:
            return len(data['results'])
        return len(data)


class PLS605ZeroSelectorBoundaryTests(TestCase):
    """`?organization=0` must stay an explicit selector, not "no selector".

    revi's P1 on shizuha-tasks#92 @ 3afaad19: `_explicit_org_id` documents None
    as "no explicit scope, NOT org zero" and returns `int(raw)`, but both new
    consumers tested it with `if org_id:`. Raw "0" normalizes to integer 0, the
    condition is false, and narrowing is skipped entirely.

    That is a WIDENING regression, not preserved behaviour: before helper
    adoption the raw string "0" was truthy, so both routes ran
    `.filter(org_id="0")` and returned zero rows. At the flagged head
    `/api/projects/` returned the caller's full accessible set and
    `/api/objectives/` every membership-scoped row — the exact wrong-target
    class this PR exists to close.

    Covered here for all three documented selector spellings on both routes,
    plus the contradictory-`organization_slug` precedence case on projects.
    """

    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username='pls605zero', email='pls605zero@shizuha.com',
            password='testpass123', is_staff=False,
        )
        self.user.organization_memberships = {7: 'member'}
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

        self.project = Project.objects.create(
            name='Pulse Zero', key='PLS605Z', org_id=7, access_level='open',
        )
        Objective.objects.create(
            title='PLS-605 zero objective', org_id=7, project=self.project,
        )

    def _rows(self, response):
        body = response.json()
        return body['results'] if isinstance(body, dict) and 'results' in body else body

    def test_zero_selector_narrows_and_never_returns_the_full_set(self):
        """Every documented spelling of org 0 must narrow, on both routes.

        No project/objective belongs to org 0, so a correctly-honoured selector
        yields zero rows. Returning rows means the selector was dropped.
        """
        cases = (
            ('query organization', '/api/projects/?organization=0', {}),
            ('query organization_id', '/api/projects/?organization_id=0', {}),
            ('header', '/api/projects/', {'HTTP_X_ORGANIZATION_ID': '0'}),
            ('query organization', '/api/objectives/?organization=0', {}),
            ('query organization_id', '/api/objectives/?organization_id=0', {}),
            ('header', '/api/objectives/', {'HTTP_X_ORGANIZATION_ID': '0'}),
        )
        for label, url, extra in cases:
            with self.subTest(selector=label, url=url):
                response = self.client.get(url, **extra)
                self.assertEqual(response.status_code, status.HTTP_200_OK)
                self.assertEqual(
                    self._rows(response), [],
                    'org 0 was dropped as "no selector" and the route widened '
                    'to the caller full scope',
                )

    def test_zero_selector_is_not_overridden_by_a_contradictory_slug(self):
        """Explicit numeric precedence must not depend on the value being truthy.

        With truthiness, `?organization=0&organization_slug=<valid>` fell through
        to `elif org_slug` and answered with the slug's org — a lower-precedence
        selector overriding an explicit numeric one.

        The slug is resolved to org 7 (which HAS rows) rather than left to the
        real admin lookup. That is what makes this assertion load-bearing: an
        unresolvable slug yields `queryset.none()`, so the test would return []
        and pass under the bug too, proving nothing.
        """
        with patch(
            'tasks.services.admin_service.admin_service.get_organization_by_slug',
            return_value={'id': 7},
        ):
            response = self.client.get(
                '/api/projects/?organization=0&organization_slug=shizuha')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            self._rows(response), [],
            'organization_slug answered a request that named an explicit '
            'numeric org selector',
        )

    def test_positive_control_member_org_still_returns_rows(self):
        """The load-bearing control: these assertions must not pass vacuously.

        Every zero-case above is satisfied by a route that returns nothing at
        all, so without this the suite would stay green if narrowing broke
        entirely.
        """
        response = self.client.get('/api/projects/?organization=7')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(self._rows(response), 'well-formed member org returned no rows')

    def test_no_selector_still_means_no_explicit_scope(self):
        """`None` must keep meaning "unscoped", distinct from 0."""
        response = self.client.get('/api/projects/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(self._rows(response), 'no-selector request was wrongly narrowed')


class PLS670SelectorStatusContractTests(TestCase):
    """PLS-670 / ADR-0054: malformed selector -> 400, valid-but-forbidden -> 403.

    Before PLS-670, `_explicit_org_id` raised PermissionDenied for BOTH a
    syntactically invalid selector and a well-formed org the caller may not
    access, so a client could not tell a typo from an authorization failure.
    The contract now splits the families: unparseable non-empty selectors are a
    client error (400), while a valid numeric org outside the caller's
    memberships stays 403. These invariants must hold across every selector
    spelling on a route that adopts the shared helper.
    """

    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username='pls670', email='pls670@shizuha.com',
            password='testpass123', is_staff=False,
        )
        self.user.organization_memberships = {7: 'member'}
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_malformed_selector_returns_400_not_403(self):
        """Unparseable non-empty selector is a client error (400)."""
        cases = (
            ('query organization', {}, '/api/items/?organization=abc'),
            ('query organization_id', {}, '/api/items/?organization_id=abc'),
            ('header', {'HTTP_X_ORGANIZATION_ID': 'abc'}, '/api/items/'),
        )
        for label, headers, url in cases:
            with self.subTest(selector=label):
                response = self.client.get(url, **headers)
                self.assertEqual(
                    response.status_code, status.HTTP_400_BAD_REQUEST,
                    f'{label} malformed selector should be 400, got '
                    f'{response.status_code}',
                )

    def test_valid_but_forbidden_org_returns_403(self):
        """A well-formed numeric org the caller may not access stays 403."""
        cases = (
            ('query organization', {}, '/api/items/?organization=999'),
            ('query organization_id', {}, '/api/items/?organization_id=999'),
            ('header', {'HTTP_X_ORGANIZATION_ID': '999'}, '/api/items/'),
        )
        for label, headers, url in cases:
            with self.subTest(selector=label):
                response = self.client.get(url, **headers)
                self.assertEqual(
                    response.status_code, status.HTTP_403_FORBIDDEN,
                    f'{label} forbidden org should be 403, got '
                    f'{response.status_code}',
                )

    def test_member_org_still_returns_200(self):
        """Positive control: a member org must not regress to 400/403."""
        response = self.client.get('/api/items/?organization=7')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_no_selector_still_returns_200(self):
        """Empty selector keeps meaning 'no explicit scope' (None)."""
        response = self.client.get('/api/items/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
