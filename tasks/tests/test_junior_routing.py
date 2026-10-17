"""PLS-4 — junior-tier routing over DB-backed tiered rosters.

Juniors are normal roster members in the deepest failover tier, not a parallel
hardcoded roster. They receive NEW work only when every senior tier for an
allowed team is unavailable. In-flight junior work is not force-reassigned when a
senior returns; the next resolver call simply prefers the active senior tier.
"""
from unittest import mock

from django.test import TestCase

from tasks.services.workflow_service import WorkflowService


REVIEW_SENIORS = [
    'revi@shizuha.com',
    'kei@shizuha.com',
    'rei@shizuha.com',
    'ren@shizuha.com',
]
REVIEW_JUNIOR = 'revi-jr@shizuha.com'
DOC_SENIORS = ['haru@shizuha.com', 'yuki@shizuha.com', 'scout@shizuha.com']
DOC_JUNIOR = 'yuki-jr@shizuha.com'


TIERED_ROSTER = {
    'review': [
        ['revi@shizuha.com', 'kei@shizuha.com'],
        ['rei@shizuha.com', 'ren@shizuha.com'],
        [REVIEW_JUNIOR],
    ],
    'documentation': [
        ['haru@shizuha.com', 'yuki@shizuha.com'],
        ['scout@shizuha.com'],
        [DOC_JUNIOR],
    ],
    # Deliberately includes a junior in a non-junior team to verify the scope
    # guard drops it instead of routing engineering work to a junior.
    'engineering': [['kai@shizuha.com'], [REVIEW_JUNIOR]],
    'devops': [['ichi@shizuha.com']],
}


def _flat(roster):
    return {email for tiers in roster.values() for email in WorkflowService._flat_roster(tiers)}


def _patch_roster(roster=None):
    return mock.patch.object(WorkflowService, '_pod_roster', return_value=roster or TIERED_ROSTER)


def _patch_availability(*unavailable_emails):
    """Patch availability so the given emails are unavailable."""
    unavailable = {e.strip().lower() for e in unavailable_emails}

    def _avail(email):
        return email.strip().lower() not in unavailable

    return mock.patch.object(WorkflowService, '_is_agent_available_for_assignment', side_effect=_avail)


def _patch_user_service(emails):
    """Resolve each email to a unique uid and make every load zero."""
    uid_map = {e.strip().lower(): i + 1 for i, e in enumerate(sorted(emails))}

    def _get_user(email):
        e = email.strip().lower()
        return {'id': str(uid_map[e]), 'email': e} if e in uid_map else None

    user_svc_patch = mock.patch(
        'tasks.user_service.user_service',
        **{'get_user_by_email.side_effect': _get_user},
    )
    load_patch = mock.patch.object(WorkflowService, '_agent_workable_load', return_value=0)

    class _Combined:
        def __enter__(self):
            self._u = user_svc_patch.__enter__()
            self._l = load_patch.__enter__()
            return self

        def __exit__(self, *a):
            load_patch.__exit__(*a)
            user_svc_patch.__exit__(*a)

    return _Combined()


class JuniorRoutingTests(TestCase):
    """Verify senior-first, junior-deepest-failover resolution (PLS-4)."""

    def test_senior_active_junior_never_assigned(self):
        """With senior reviewers available, revi-jr must never receive a task."""
        with _patch_roster(), _patch_availability(), _patch_user_service(_flat(TIERED_ROSTER)):
            uid, email = WorkflowService._resolve_team_assignee('review')

        self.assertEqual(email, 'revi@shizuha.com')
        self.assertNotEqual(email, REVIEW_JUNIOR)
        self.assertIsNotNone(uid)

    def test_all_senior_tiers_unavailable_junior_takes_over(self):
        """All review seniors unavailable → the deepest junior tier receives it."""
        with _patch_roster(), _patch_availability(*REVIEW_SENIORS), _patch_user_service(_flat(TIERED_ROSTER)):
            uid, email = WorkflowService._resolve_team_assignee('review')

        self.assertEqual(email, REVIEW_JUNIOR)
        self.assertIsNotNone(uid)

    def test_excluded_junior_does_not_bounce_back_when_seniors_unavailable(self):
        """Reassigning away from a junior must not resolve back to it."""
        roster = {
            'review': [['review-senior@example.test'], [REVIEW_JUNIOR]],
        }
        with _patch_roster(roster), _patch_availability('review-senior@example.test'), _patch_user_service(_flat(roster)):
            uid, email = WorkflowService._resolve_team_assignee(
                'review',
                exclude_email=REVIEW_JUNIOR,
            )

        self.assertIsNone(uid)
        self.assertIsNone(email)

    def test_senior_returns_routes_back_to_senior(self):
        """Senior restored → the next new-task resolution routes to senior again."""
        all_emails = _flat(TIERED_ROSTER)
        with _patch_roster(), _patch_availability(*REVIEW_SENIORS), _patch_user_service(all_emails):
            _, first = WorkflowService._resolve_team_assignee('review')
        self.assertEqual(first, REVIEW_JUNIOR)

        with _patch_roster(), _patch_availability(), _patch_user_service(all_emails):
            _, second = WorkflowService._resolve_team_assignee('review')
        self.assertEqual(second, 'revi@shizuha.com')

    def test_documentation_team_senior_absent_junior_routes(self):
        """Documentation uses the same deepest-tier junior fallback."""
        with _patch_roster(), _patch_availability(*DOC_SENIORS), _patch_user_service(_flat(TIERED_ROSTER)):
            _, email = WorkflowService._resolve_team_assignee('documentation')

        self.assertEqual(email, DOC_JUNIOR)

    def test_junior_scope_guard_engineering_not_affected(self):
        """A mis-rostered junior on engineering is ignored, not assigned."""
        with _patch_roster(), _patch_availability('kai@shizuha.com'), _patch_user_service(_flat(TIERED_ROSTER)):
            uid, email = WorkflowService._resolve_team_assignee('engineering')

        self.assertIsNone(uid)
        self.assertIsNone(email)

    def test_junior_scope_guard_devops_not_affected(self):
        """DevOps likewise never falls through to juniors."""
        roster = {
            'devops': [['ichi@shizuha.com'], [DOC_JUNIOR]],
        }
        with _patch_roster(roster), _patch_availability('ichi@shizuha.com'), _patch_user_service(_flat(roster)):
            uid, email = WorkflowService._resolve_team_assignee('devops')

        self.assertIsNone(uid)
        self.assertIsNone(email)

    def test_both_senior_and_junior_unavailable_returns_none(self):
        """When every review tier is unavailable, no assignee is returned."""
        with _patch_roster(), _patch_availability(*REVIEW_SENIORS, REVIEW_JUNIOR), _patch_user_service(_flat(TIERED_ROSTER)):
            uid, email = WorkflowService._resolve_team_assignee('review')

        self.assertIsNone(uid)
        self.assertIsNone(email)
