"""PLS-711 §7.2/§7.3: a save into a non-capacity status RELEASES the
reservation instead of mismatching it.

@hiro's ruling §3: `_slot_kind_for_status` is three-valued and `None` means
"not applicable". The mint path honours that (`_reserve_wip_slot` returns
without reserving); the verify path used to COMPARE it, so `None != 'ready'`
raised `wip_reservation_target_mismatch`.

This is deterministic and NOT escalation-specific: `blocked` is a hardcoded
literal in `_assignment_excluded_statuses()`, so no DB fault is required, and
the same holds for every other excluded status. The fixture is therefore
parametrised over SET MEMBERSHIP rather than pinned to `blocked` -- a
blocked-only fixture would go green while the identical defect stayed live for
`deferred`, `cancelled`, and PLS-707's incoming `waiting_external`.
"""

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import transaction
from django.test import TransactionTestCase

from tasks.models import (
    AgentWipCapacity,

    Item,
    ItemLink,
    Status,
    TeamRoster,
    WipSlotReservation,
    Workflow,
    WorkflowStatus,
)
from tasks.services.workflow_service import WorkflowService

User = get_user_model()


class Pls711SentinelReleaseTests(TransactionTestCase):

    def setUp(self):
        self.actor = User.objects.create_user(
            username='pls711s', email='pls711s@example.com', is_staff=True)
        TeamRoster.objects.update_or_create(
            organization_id=1, team_name='engineering',
            defaults={'organization_slug': 'shizuha',
                      'tiers': [[self.actor.email]], 'is_active': True,
                      'wip_enforced': True, 'ready_wip_cap': 50,
                      'active_wip_cap': 50},
        )
        WorkflowService._invalidate_team_roster_caches()
        open_status, _ = Status.objects.get_or_create(
            slug='open', defaults={'name': 'Open', 'category': 'todo'}
        )
        self.workflow, _ = Workflow.objects.get_or_create(
            slug='autonomous-dev',
            defaults={
                'name': 'Autonomous Dev',
                'initial_status': open_status,
                'is_active': True,
            },
        )
        WorkflowStatus.objects.get_or_create(
            workflow=self.workflow,
            status=open_status,
            defaults={'position': 0},
        )
        self.item = Item.objects.create(
            item_key='P711S-1', mode='task', title='sentinel',
            description='x', workflow_name='autonomous-dev', status='open',
            assignment_group='engineering',
            assignee_id=self.actor.id, assignee_email=self.actor.email,
            created_by_id=self.actor.id, created_by_email=self.actor.email,
        )

    def _reserve_then_move_to(self, status, *, item=None):
        """Mint at a capacity-consuming status, then mutate to `status`."""
        item = item or self.item
        # Each call starts from a clean, capacity-consuming state: a subTest
        # that left the item excluded would make the NEXT mint return None and
        # the precondition assertion below would (correctly) reject it.
        item.status = 'open'
        if hasattr(item, '_wip_reservation_token'):
            delattr(item, '_wip_reservation_token')
        with transaction.atomic():
            reserved = WorkflowService._reserve_wip_slot(
                item, self.actor.id, self.actor.email)
        self.assertIsNot(
            reserved, None,
            'fixture minted no reservation -- without one _verify_wip_reservation '
            'early-returns on `if not token` and this test asserts nothing '
            '(the wip_enforced=True roster above is what makes it mint)',
        )
        token = getattr(item, '_wip_reservation_token', None)
        self.assertIsNotNone(token, 'no token attached after a successful mint')
        item.status = status
        return token

    def test_saving_into_each_excluded_status_releases_rather_than_raises(self):
        # Parametrised over MEMBERSHIP, not over a `blocked` literal.
        for index, status in enumerate(sorted(WorkflowService._assignment_excluded_statuses())):
            with self.subTest(status=status):
                self.assertIn(
                    status, WorkflowService._assignment_excluded_statuses(),
                    f'{status} is not excluded -- the fixture premise is wrong',
                )
                # Use a fresh parent row per target so this drives the real
                # admission save sequence rather than repeatedly calling the
                # patched helper. The reservation token is transient, exactly
                # as it is on the escalation parent in production.
                item = Item.objects.create(
                    item_key=f'P711S-{index + 10}', mode='task',
                    title=f'sentinel {status}', description='x',
                    workflow_name='autonomous-dev', status='open',
                    assignment_group='engineering',
                    assignee_id=self.actor.id, assignee_email=self.actor.email,
                    created_by_id=self.actor.id, created_by_email=self.actor.email,
                )
                status_row, _ = Status.objects.get_or_create(
                    slug=status,
                    defaults={
                        'name': status.replace('_', ' ').title(),
                        'category': (
                            'done'
                            if WorkflowService._status_is_terminal(status)
                            else 'todo'
                        ),
                    },
                )
                WorkflowStatus.objects.get_or_create(
                    workflow=self.workflow,
                    status=status_row,
                    defaults={'position': 999},
                )
                if status == 'blocked':
                    blocker = Item.objects.create(
                        mode='task', title='sentinel blocker', status='open',
                        workflow_name='autonomous-dev',
                        assignment_group='engineering',
                        assignee_id=self.actor.id,
                        assignee_email=self.actor.email,
                    )
                    link = ItemLink(
                        source_item=blocker,
                        target_item=item,
                        link_type='blocks',
                        created_by_id=self.actor.id,
                        created_by_email=self.actor.email,
                    )
                    link._skip_auto_block = True
                    link.save()
                token = self._reserve_then_move_to(status, item=item)
                update_fields = ['status']
                if status == 'waiting_external':
                    # The status is retired. Entry raises even with an armed
                    # authority, and the row stays on its workable status.
                    with self.assertRaises(ValidationError):
                        with transaction.atomic():
                            item.save(update_fields=update_fields)
                    item.refresh_from_db()
                    self.assertNotEqual(item.status, 'waiting_external')
                    continue
                # Must NOT raise: the parent status write consumes the
                # reservation through Item.save's production caller path.
                with transaction.atomic():
                    item.save(update_fields=update_fields)
                self.assertFalse(
                    WipSlotReservation.objects.filter(token=token).exists(),
                    f'reservation survived a save into {status}; it must be '
                    'RELEASED, not merely skipped -- otherwise it counts against '
                    'the seat until it expires',
                )

    def test_CONTROL_a_genuine_mismatch_still_raises(self):
        """The guard must still guard.

        Releasing on a non-capacity status could have been implemented as
        "stop verifying", which would satisfy the test above while disabling
        the check entirely. Here the item STAYS capacity-consuming and the team
        is changed underneath the reservation -- a real mismatch, which must
        still raise.
        """
        self._reserve_then_move_to('open')          # still capacity-consuming
        self.item.assignment_group = 'devops'       # reservation was for engineering
        with self.assertRaises(ValidationError) as caught, transaction.atomic():
            WorkflowService._verify_wip_reservation(self.item)
        self.assertIn('wip_reservation_target_mismatch', str(caught.exception))

    def test_CONTROL_the_predicate_agrees_with_the_mint_path(self):
        """§7.3: the two sides must read the sentinel the same way.

        This is the invariant the whole ruling turns on -- if they ever diverge
        again, the defect returns in a new caller.
        """
        for status in ('open', 'in_progress', 'blocked', 'deferred', 'done'):
            with self.subTest(status=status):
                consumes = WorkflowService._consumes_capacity(status)
                kind = WorkflowService._slot_kind_for_status(status)
                self.assertEqual(
                    consumes, kind is not None,
                    f'_consumes_capacity({status!r}) disagrees with '
                    f'_slot_kind_for_status -> {kind!r}',
                )
