"""PLS-1283 regression: the junior-set union is ORG-SCOPED.

Defect (PLS-1282 follow-up): ``_team_junior_map`` unioned
``TeamRoster.junior_emails`` across review/documentation rows into a GLOBAL
(cross-org) junior set, and ``_junior_guarded_tiers`` stripped that set from
every non-junior-only team. A staffed org-B team whose members appeared in an
org-A junior listing was therefore unroutable via the team-resolver —
HIVE-1710's routing-correctness half (the truthful ``junior_scope_excluded``
label landed in pulse#547; this fixes the outcome).

Semantic decision (recorded on PLS-1283): "junior" is an ORG-SCOPED
classification — a property of the (organization, team) roster row that
records it, not a global seat property. Routing is org-scoped end-to-end
(org-scoped roster lookup, org-scoped candidate snapshot, PLAT-4213 org
guard); a cross-org read in the guard was an isolation leak, inconsistent
with every other roster input. PLS-4 is preserved within each org: an org
that classifies a seat as junior on its OWN review row still strips that seat
from its own non-junior teams. Fail-closed fallbacks: NULL-slug legacy rows
stay in every org's set, and org-less callers keep the legacy global union —
PLS-4 never opens up.
"""
from unittest.mock import patch

from django.test import TestCase

from tasks.models import Item, Project, TeamRoster
from tasks.services.workflow_service import WorkflowService


def _organization(org_id):
    return {
        1: {'id': 1, 'slug': 'shizuha'},
        2: {'id': 2, 'slug': 'shizuha-digital'},
    }.get(org_id)


class Pls1283OrgScopedJuniorUnionTests(TestCase):
    def setUp(self):
        # The junior map carries a 30s class-level cache keyed by roster
        # version; clear it so each test reads its own rows.
        WorkflowService._junior_cache = None
        self.project = Project.objects.create(
            name='Hive', key='HIVE', org_id=1,
            access_level='private', is_active=True,
        )
        self.item = Item.objects.create(
            project=self.project, item_key='HIVE-1710', mode='task',
            title='Auto-rollback receipt', assignment_group='qa',
        )

    def _qa_roster(self, tiers, org_slug='shizuha', org_id=1):
        TeamRoster.objects.update_or_create(
            team_name='qa', organization_id=org_id, organization_slug=org_slug,
            defaults={'tiers': tiers, 'is_active': True, 'junior_emails': []},
        )

    def _review_juniors(self, emails, org_slug='shizuha', org_id=1):
        TeamRoster.objects.update_or_create(
            team_name='review', organization_id=org_id,
            organization_slug=org_slug,
            defaults={'tiers': [list(emails)], 'is_active': True,
                      'junior_emails': list(emails)},
        )

    def test_cross_org_junior_listing_no_longer_strips_staffed_roster(self):
        """The HIVE-1710 routing-correctness contract: org-2's review row lists
        zen as junior, but org-1's qa roster is staffed — the org-1 guard must
        keep every member and the resolver must route (no hold, no strip)."""
        self._qa_roster([['zen@shizuha.com', 'nagi@shizuha.com']])
        self._review_juniors(['zen@shizuha.com'], org_slug='shizuha-digital',
                             org_id=2)
        # Unit level: the org-1 junior set does not contain zen; the guard
        # keeps the full roster.
        self.assertNotIn('zen@shizuha.com', WorkflowService._junior_emails('shizuha'))
        guarded = WorkflowService._junior_guarded_tiers(
            'qa', [['zen@shizuha.com', 'nagi@shizuha.com']], 'shizuha',
        )
        self.assertEqual(guarded, [['zen@shizuha.com', 'nagi@shizuha.com']])
        # Resolver level: with every seat live the roster is STAFFED — the
        # resolver routes normally to the full pool's pick. Loads are equal
        # (0/0) so the route's FIFO route key takes the roster head (zen);
        # under the org-scoped fix zen is a member — the bug face (global
        # junior union) stripped zen and routed to nagi instead. No hold is
        # recorded in the fixed face.
        captured = {}
        # `_record_routability_hold` is a @staticmethod — on this codebase the
        # class attribute IS the plain function (no __func__). getattr keeps
        # the spy compatible if the shape ever changes to classmethod.
        original = getattr(WorkflowService._record_routability_hold, '__func__',
                           WorkflowService._record_routability_hold)

        def _spy(item, team_name, reasons, source='team-resolver', **kw):
            captured['reasons'] = list(reasons or [])
            return original(item, team_name, reasons, source=source, **kw)

        def _get_user(email):
            return {
                'zen@shizuha.com': {'id': '42', 'email': 'zen@shizuha.com'},
                'nagi@shizuha.com': {'id': '43', 'email': 'nagi@shizuha.com'},
            }.get((email or '').strip().lower())

        with patch.object(WorkflowService,
                          '_record_routability_hold',
                          staticmethod(_spy)), \
             patch.object(WorkflowService,
                          '_is_agent_available_for_new_assignment',
                          return_value=True), \
             patch.object(WorkflowService,
                          '_is_agent_available_for_assignment',
                          return_value=True), \
             patch.object(WorkflowService,
                          '_member_can_execute_from_status',
                          return_value=True), \
             patch.object(WorkflowService, '_agent_active_load', return_value=0), \
             patch.object(WorkflowService, '_agent_workable_load', return_value=0), \
             patch('tasks.user_service.user_service',
                   **{'get_user_by_email.side_effect': _get_user}), \
             patch('tasks.services.admin_service.admin_service.get_organization',
                   side_effect=_organization):
            result = WorkflowService._resolve_team_assignee(
                'qa', item=self.item, enforce_cap=False,
            )
        self.assertEqual(result, (42, 'zen@shizuha.com'))
        self.assertEqual(captured, {})

    def test_same_org_junior_listing_still_strips_pls4(self):
        """PLS-4 is preserved within an org: org-1's OWN review row listing zen
        still strips zen from org-1's qa guard."""
        self._qa_roster([['zen@shizuha.com', 'nagi@shizuha.com']])
        self._review_juniors(['zen@shizuha.com'], org_slug='shizuha', org_id=1)
        self.assertIn('zen@shizuha.com', WorkflowService._junior_emails('shizuha'))
        guarded = WorkflowService._junior_guarded_tiers(
            'qa', [['zen@shizuha.com', 'nagi@shizuha.com']], 'shizuha',
        )
        self.assertEqual(guarded, [['nagi@shizuha.com']])

    def test_null_org_review_row_strips_everywhere_failclosed(self):
        """Legacy/ambiguous rows (NULL organization_slug) stay in EVERY org's
        junior set — fail-closed, PLS-4 never opens up.

        Seeding note: ``TeamRoster.save()`` fail-closed-normalizes a NULL slug
        to the default org (PLAT-4213), so a NULL-slug legacy row cannot be
        minted through the ORM — ORM-seeding this face minted a duplicate
        ``(shizuha, review)`` identity and violated
        ``uniq_team_roster_org_name`` (runs 7279/7283). The scoping contract
        is therefore exercised at the map boundary: the injected legacy row
        (org key ``''``) must land in EVERY org's junior set and strip from
        that org's non-junior teams."""
        self._qa_roster([['zen@shizuha.com', 'nagi@shizuha.com']])
        legacy_row_map = {('', 'review'): ['zen@shizuha.com']}
        with patch.object(WorkflowService, '_team_junior_map',
                          return_value=legacy_row_map):
            self.assertIn('zen@shizuha.com',
                          WorkflowService._junior_emails('shizuha'))
            self.assertIn('zen@shizuha.com',
                          WorkflowService._junior_emails('shizuha-digital'))
            guarded = WorkflowService._junior_guarded_tiers(
                'qa', [['zen@shizuha.com', 'nagi@shizuha.com']], 'shizuha',
            )
            self.assertEqual(guarded, [['nagi@shizuha.com']])

    def test_unknown_org_keeps_legacy_global_union(self):
        """An org-less caller (e.g. the pod-roster pull path) keeps the legacy
        GLOBAL union — fail-closed, unchanged behavior."""
        self._review_juniors(['zen@shizuha.com'], org_slug='shizuha-digital',
                             org_id=2)
        self.assertIn('zen@shizuha.com', WorkflowService._junior_emails(''))

    def test_other_orgs_own_junior_classification_intact(self):
        """Org-2's review team still sees its own junior classification: the
        deepest-tier junior failover survives the org-2 guard."""
        self._review_juniors(['zen@shizuha.com'], org_slug='shizuha-digital',
                             org_id=2)
        guarded = WorkflowService._junior_guarded_tiers(
            'review',
            [['senior@shizuha.com'], ['zen@shizuha.com']],
            'shizuha-digital',
        )
        self.assertEqual(guarded, [['senior@shizuha.com'], ['zen@shizuha.com']])

    def test_non_junior_only_team_rows_never_feed_the_union(self):
        """Only JUNIOR_ONLY teams (review/documentation) feed the junior set —
        a qa-row junior_emails column must not strip org-mates either."""
        TeamRoster.objects.update_or_create(
            team_name='qa', organization_id=1, organization_slug='shizuha',
            defaults={'tiers': [['zen@shizuha.com']], 'is_active': True,
                      'junior_emails': ['zen@shizuha.com']},
        )
        self.assertNotIn('zen@shizuha.com',
                         WorkflowService._junior_emails('shizuha'))
