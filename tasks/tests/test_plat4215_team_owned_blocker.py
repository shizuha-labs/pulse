"""PLAT-4215 follow-up (migration 0101): a TEAM-OWNED blocker satisfies the
blocked-task constraint.

The 0100 trigger demanded a concrete assignee on the blocker at every instant,
which contradicted Pulse's own team-routing model (the /assign/ path clears the
concrete assignee when assignment_group is set; the availability resolver picks
the member afterwards). Re-routing a blocker task to a team while anything was
blocked on it raised at COMMIT -> recurring 500s on /api/items/<key>/assign/
(INV-32 / INV-27, 2026-07-14) and wedged the blocker on its stale owner.

These tests pin the relaxed contract:
  - blocker with concrete assignee  -> OK (unchanged)
  - blocker with ONLY assignment_group (team-owned) -> OK (the fix)
  - blocker with neither            -> still rejected
"""
from django.db import IntegrityError, connection, transaction
from django.test import TestCase

from tasks.models import Item, ItemLink


def _immediate_constraints():
    with connection.cursor() as cursor:
        cursor.execute('SET CONSTRAINTS ALL IMMEDIATE')


class TeamOwnedBlockerConstraintTests(TestCase):
    """DB-trigger contract tests (postgres only — trigger installed by 0100/0101)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._pg = connection.vendor == 'postgresql'

    def _blocked_pair(self, **blocker_fields):
        blocker = Item.objects.create(
            mode='task', title='blocker', status='open', **blocker_fields)
        parent = Item.objects.create(mode='task', title='parent', status='open')
        link = ItemLink(
            source_item=blocker, target_item=parent, link_type='blocks',
            created_by_id=1)
        link._skip_auto_block = True
        link.save()
        return blocker, parent

    def _mark_blocked(self, parent):
        # Raw UPDATE: exercise the DB trigger itself, not model-layer fences.
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE tasks_task SET status='blocked' WHERE id=%s", [parent.pk])

    def test_concretely_assigned_blocker_still_satisfies(self):
        if not self._pg:
            self.skipTest('postgres-only trigger')
        blocker, parent = self._blocked_pair(
            assignee_id=42, assignee_email='someone@shizuha.com')
        self._mark_blocked(parent)
        _immediate_constraints()  # no raise

    def test_team_owned_blocker_satisfies_constraint(self):
        """The 0101 fix: assignment_group alone counts as owned."""
        if not self._pg:
            self.skipTest('postgres-only trigger')
        blocker, parent = self._blocked_pair(assignment_group='engineering')
        self.assertIsNone(blocker.assignee_id)
        self._mark_blocked(parent)
        _immediate_constraints()  # no raise

    def test_team_routing_an_existing_blocker_is_allowed(self):
        """The INV-32 repro: clearing the concrete assignee while routing the
        blocker to a team must NOT violate the constraint."""
        if not self._pg:
            self.skipTest('postgres-only trigger')
        blocker, parent = self._blocked_pair(
            assignee_id=42, assignee_email='someone@shizuha.com')
        self._mark_blocked(parent)
        _immediate_constraints()
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE tasks_task SET assignee_id=NULL, assignee_email='', "
                "assignment_group='architecture' WHERE id=%s", [blocker.pk])
        _immediate_constraints()  # no raise — team-owned

    def test_unowned_blocker_still_rejected(self):
        if not self._pg:
            self.skipTest('postgres-only trigger')
        blocker, parent = self._blocked_pair()  # no assignee, no group
        # The offending UPDATE runs INSIDE the savepoint so its deferred trigger
        # event is discarded on rollback (postgres re-queues events whose firing
        # was undone by ROLLBACK TO SAVEPOINT — they would re-raise at teardown).
        with self.assertRaisesMessage(
                IntegrityError, 'blocked_item_requires_active_assigned_blocker'):
            with transaction.atomic():
                self._mark_blocked(parent)
                _immediate_constraints()

    def test_terminal_blocker_still_rejected_even_if_team_owned(self):
        if not self._pg:
            self.skipTest('postgres-only trigger')
        blocker, parent = self._blocked_pair(assignment_group='engineering')
        self._mark_blocked(parent)
        _immediate_constraints()
        with self.assertRaisesMessage(
                IntegrityError, 'blocked_item_requires_active_assigned_blocker'):
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(
                        "UPDATE tasks_task SET status='cancelled' WHERE id=%s",
                        [blocker.pk])
                _immediate_constraints()
