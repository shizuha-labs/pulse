"""PLS-933: needs_triage owner-derivation must stamp the TRUE owning team.

Live regression (2026-08-10/12, san+nagi): ~98% of the needs_triage barrel was
mis-stamped `assignment_group=engineering` regardless of true owner (finance
FIN-35/36, drive DRV-81/84, wiki WIKI-49/51, security-finding DOJO-40, ...),
so every real team's actionable queue looked empty and no seat became
lease-worthy. Root cause: create-time default (`tasks/views.py`) and the
`_workflow_default_implementer_group` static map + reporter-assign fallback
(
`workflow_service.py`) defaulted to engineering with NO per-item signal
consulted first.

Production caller order under test (stateful recurrence shape — the unit of
regression is the SEQUENCE, not one helper):
  create (no team)
    -> derive, else engineering only with an org-local engineering roster [views.py]
  needs_triage -> next-hop assign
    -> _workflow_default_implementer_group(item)                   [return/route]
  reporter-assign fallback
    -> _derive_owner_team(item) or item.assignment_group or 'engineering'
                                                                    [:8971 analog]

Each resolved owner must pass exact-org registration
(`_accepted_implementer_group`) — the SAME authority gate routing already
uses, with the PLS-731 cell C platform fallback as the ONE existence
definition (nagi Option 2 ruling). Finance existing NOWHERE (neither the
item's org nor the platform roster) must NOT route to finance.
"""
import itertools

from django.contrib.auth import get_user_model
from django.test import TestCase

from tasks.models import Item, Project, Status, TeamRoster, Workflow, WorkflowStatus
from tasks.services.workflow_service import WorkflowService

User = get_user_model()
_key_counter = itertools.count(1)


class PLS933OwnerDerivationTests(TestCase):
    """The three fixtures from the task: foreign-owner items land on their real
    team at all three call sites; unknown/unregistered cases stay fail-closed."""

    def setUp(self):
        self.project = Project.objects.create(
            name='Finance test project', key='FIN', org_id=1,
        )
        self.drive_project = Project.objects.create(
            name='Drive test project', key='DRV', org_id=1,
        )
        self.wiki_project = Project.objects.create(
            name='Wiki test project', key='WIKI', org_id=1,
        )
        self.org2_project = Project.objects.create(
            name='Foreign-org FIN (no finance roster)', key='FIN', org_id=2,
        )
        # Teams: engineering (registered), finance/drive/wiki (registered org 1
        # only), security (registered), and deliberately NOT finance in org 2.
        for team_name, org, emails in (
            ('engineering', 1, ['eng@shizuha.com']),
            ('finance', 1, ['fin@shizuha.com']),
            ('drive', 1, ['drv@shizuha.com']),
            ('wiki', 1, ['wiki@shizuha.com']),
            ('security', 1, ['sec@shizuha.com']),
        ):
            TeamRoster.objects.update_or_create(
                organization_id=org,
                team_name=team_name,
                defaults={
                    'organization_slug': 'shizuha' if org == 1 else 'org2',
                    'tiers': [emails],
                    'is_active': True,
                },
            )
        WorkflowService._invalidate_team_roster_caches()

    def _item(self, project=None, workflow='autonomous-bug', tags=None, key=None):
        return Item(
            item_key=key or f'FIN-T{next(_key_counter)}',
            title='pls933 test',
            project=project or self.project,
            status='needs_triage',
            workflow_name=workflow,
            workflow_status='needs_triage',
            tags=tags or '',
            metadata={},
        )

    def test_finance_project_derives_finance(self):
        """A FIN project with a registered finance team routes to finance."""
        self.assertEqual(
            WorkflowService._derive_owner_team(self._item()),
            'finance',
        )

    def test_drive_and_wiki_projects_derive_true_team(self):
        self.assertEqual(
            WorkflowService._derive_owner_team(self._item(project=self.drive_project)),
            'drive',
        )
        self.assertEqual(
            WorkflowService._derive_owner_team(self._item(project=self.wiki_project)),
            'wiki',
        )

    def test_workflow_default_implementer_group_uses_true_owner(self):
        """The route default must pick finance (not the static engineering)."""
        self.assertEqual(
            WorkflowService._workflow_default_implementer_group(self._item()),
            'finance',
        )

    def test_fin_nowhere_roster_fails_closed(self):
        """FIN with a finance roster in NEITHER the item's org NOR the platform
        roster must NOT route to finance — true nowhere-exists fail-closed.

        Re-scoped per nagi's Option 2 ruling (PLS-933): cell C platform
        fallback is the ONE existence definition. A platform-rostered finance
        receiving a foreign org's FIN item is domain-correct routing (cell C
        makes it routable) — the cross-org leak this gate prevents is finance
        existing NOWHERE.
        """
        item = self._item(project=self.org2_project)
        # Sanity (domain-correct per the ruling): with the platform roster
        # present, cell C routes the foreign org's FIN to finance.
        self.assertEqual(
            WorkflowService._derive_owner_team(item),
            'finance',
        )
        # Remove finance from BOTH layers -> nowhere-exists -> None.
        TeamRoster.objects.filter(team_name='finance').delete()
        WorkflowService._invalidate_team_roster_caches()
        self.assertIsNone(WorkflowService._derive_owner_team(item))
        # And the workflow default still falls back to the registered
        # engineering lane (platform-rostered via cell C) — never a
        # nowhere-exists finance.
        self.assertEqual(
            WorkflowService._workflow_default_implementer_group(item),
            'engineering',
        )

    def test_derivation_agrees_with_registration_layer(self):
        """Drift guard (nagi, PLS-933 Option 2 ruling): `_derive_owner_team`
        must never return a team `_team_registered_for_item` would reject for
        the item's org — both layers pinned to the same existence definition
        (the PLAT-8169 silent-drift class)."""
        items = (
            self._item(),                               # org 1 project-key hint
            self._item(project=self.drive_project),     # org 1 drive
            self._item(project=self.wiki_project),      # org 1 wiki
            self._item(project=self.org2_project),      # org 2, cell C fallback
            self._item(workflow='security-finding'),    # workflow hint
            self._item(tags='finance'),                 # tag hint
        )
        for item in items:
            derived = WorkflowService._derive_owner_team(item)
            if derived is None:
                continue
            # Assert the two layers agree DIRECTLY (not via the derivation's
            # internal gate): if the ladder ever skips or swaps the gate,
            # this fails.
            self.assertTrue(
                WorkflowService._team_registered_for_item(derived, item),
                msg=(
                    f"{item.item_key}: derivation returned {derived!r} but "
                    f"the registration layer rejects it for this org"
                ),
            )

    def test_security_finding_workflow_derives_security(self):
        item = self._item(workflow='security-finding')
        self.assertEqual(WorkflowService._derive_owner_team(item), 'security')

    def test_unknown_project_key_without_signals_stays_default(self):
        """No hint, no registration match -> engineering default preserved."""
        project = Project.objects.create(
            name='Mystery', key='MYS', org_id=1,
        )
        item = self._item(project=project)
        self.assertIsNone(WorkflowService._derive_owner_team(item))
        self.assertEqual(
            WorkflowService._workflow_default_implementer_group(item),
            'engineering',
        )

    def test_tags_naming_known_team_derive(self):
        """A generic project carrying an owner tag routes by that tag (exact-org)."""
        project = Project.objects.create(
            name='Generic', key='GEN', org_id=1,
        )
        item = self._item(project=project, tags='security-sensitive,ctf')
        # 'ctf' is not a known team; 'security-sensitive' not either -> None.
        self.assertIsNone(WorkflowService._derive_owner_team(item))
        # A direct team tag routes.
        item2 = self._item(project=project, tags='drive')
        self.assertEqual(WorkflowService._derive_owner_team(item2), 'drive')

    def test_derive_owner_team_never_invents_unregistered(self):
        """Unknown team in tags -> None (fail closed, no phantom route).

        Uses a generic (GEN) project so no project-key hint can pre-empt the
        tag path: an unregistered tag must never fabricate a route.
        """
        project = Project.objects.create(
            name='Generic', key='GEN', org_id=1,
        )
        item = self._item(project=project, tags='phantom-team-name')
        self.assertIsNone(WorkflowService._derive_owner_team(item))
