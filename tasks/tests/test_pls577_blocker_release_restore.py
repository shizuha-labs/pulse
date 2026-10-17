"""PLS-577: internal blocker-release restore vs the submit gates.

Live deadlock (PLS-573 × PLAT-4668): a terminal blocker could not release its
blocked ``in_review`` dependent because the restore transition was evaluated as
an ordinary author submit — the PLAT-1354 newer-direction gate and the PLS-441
re-entry head gate both rejected it (stale/closed PR + newer direction), the
dependent stayed blocked, and the deferred PLAT-4215 constraint then refused
the blocker's own terminalization with a naked-blocked 409.

Contract under test (production order, per the task acceptance):
1. An item whose state would fail BOTH submit gates (closed/stale PR head plus
   genuinely newer reviewer direction) is blocked with recorded pre-block
   ``in_review`` state and one active blocker.
2. Terminalizing that blocker auto-restores the dependent to ``in_review``
   exactly once, through the documented Blocker-Resolved transition — no gate
   rejection, no naked-blocked failure, no double restore.
3. Ordinary author submission (``in_progress → in_review``) with the SAME item
   state remains fully gated by PLAT-1354 — the exemption is operation-shaped,
   not item-shaped.
"""
import itertools
from datetime import timedelta
from types import SimpleNamespace
from unittest import mock
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, transaction
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from tasks.models import (
    Comment,
    Item,
    ItemLink,
    Project,
    Status,
    TeamRoster,
    Workflow,
    WorkflowStatus,
    WorkflowTransition,
)
from tasks.services.workflow_service import WorkflowService, WorkflowValidationError

STALE_PR = 'https://origin.shizuha.com/shizuha-labs/hive/pulls/35'
AUTHOR = 'author@agents.shizuha.io'
REVIEWER = 'reviewer@agents.shizuha.io'
_key_counter = itertools.count(1)

GATED_VALIDATORS = [
    {'type': 'review_reentry_requires_progress'},
    {'type': 'submit_requires_current_direction'},
]

User = get_user_model()


def _gated_item(status='blocked'):
    """Unit-test item whose review state would fail BOTH submit gates.

    PLAT-4215 forbids creating an Item directly in ``blocked`` (a durable parent
    must exist before an active blocker links), so we persist it in a working
    status and, for the blocked case, override ``status`` IN MEMORY — which is
    all the classifier and the submit validators read (they key on metadata +
    comments, not the persisted status column).
    """
    bounce_at = timezone.now() - timedelta(hours=2)
    item = Item.objects.create(
        item_key=f'PLS-577-T{next(_key_counter)}',
        title='pls577 blocker release restore',
        status='in_progress',
        workflow_name='autonomous-bug',
        workflow_status='in_progress',
        assignee_id=101,
        assignee_email=AUTHOR,
        created_by_id=101,
        created_by_email=AUTHOR,
        metadata={
            'pull_requests': [{
                'url': STALE_PR, 'repo': 'shizuha-labs/hive', 'number': 35,
                'status': 'closed', 'head_sha': 'stale-head',
            }],
            'review_bounce_at': bounce_at.isoformat(),
            'review_bounce_pr_heads': {STALE_PR: 'stale-head'},
            'pre_block_state': {
                'status': 'in_review',
                'assignee_id': 101,
                'assignee_email': AUTHOR,
                'had_active_blocker': True,
            },
        },
    )
    # Genuinely newer reviewer direction AFTER the PR head/bounce timestamp:
    # this is exactly what makes PLAT-1354 refuse an ordinary submit.
    Comment.objects.create(
        item=item,
        author_id=202,
        author_email=REVIEWER,
        content='Request Changes: rework the disposition path before resubmitting.',
    )
    # These classifier/validator unit tests do not exercise blocker release;
    # the PostgreSQL-backed production-order regression below does.
    item.status = status
    return item


def _recovery_transition(name='Blocker Resolved - Resume In Review'):
    return SimpleNamespace(
        name=name,
        validators=list(GATED_VALIDATORS),
        from_status='blocked',
        to_status='in_review',
    )


def _ordinary_submit_transition():
    return SimpleNamespace(
        name='Submit for Code Review',
        validators=list(GATED_VALIDATORS),
        from_status='in_progress',
        to_status='in_review',
    )


class BlockerReleaseRestoreClassifierTests(TestCase):
    def test_blocked_item_with_recovery_transition_classifies(self):
        item = _gated_item(status='blocked')
        self.assertTrue(
            WorkflowService._is_blocker_release_restore(_recovery_transition(), item)
        )

    def test_non_blocked_item_never_classifies(self):
        item = _gated_item(status='in_progress')
        self.assertFalse(
            WorkflowService._is_blocker_release_restore(_recovery_transition(), item)
        )

    def test_blocked_item_with_ordinary_transition_never_classifies(self):
        item = _gated_item(status='blocked')
        self.assertFalse(
            WorkflowService._is_blocker_release_restore(_ordinary_submit_transition(), item)
        )


class RestoreBypassesSubmitGatesTests(TestCase):
    def test_recovery_transition_passes_both_gates(self):
        # Both gates would fire (closed stale-head PR + newer direction); the
        # documented recovery transition must pass regardless.
        item = _gated_item(status='blocked')
        WorkflowService._run_validators(_recovery_transition(), item)  # must not raise

    def test_ordinary_submit_remains_fully_gated(self):
        # An ordinary submit (not blocked → no recovery classification) stays
        # gated: with a stale/closed PR head the re-entry gate fires first, and
        # with newer direction the direction gate would; assert a submit gate
        # fired regardless of order (both messages say "... gate").
        item = _gated_item(status='in_progress')
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._run_validators(_ordinary_submit_transition(), item)
        self.assertIn('gate', str(ctx.exception).lower())

    def test_misnamed_transition_from_blocked_remains_gated(self):
        # A blocked item cannot smuggle past the gates through an arbitrary
        # transition — only the documented recovery edge is exempt.
        item = _gated_item(status='blocked')
        arbitrary = SimpleNamespace(
            name='Escalation Override',
            validators=list(GATED_VALIDATORS),
            from_status='blocked',
            to_status='in_review',
        )
        with self.assertRaises(WorkflowValidationError):
            WorkflowService._run_validators(arbitrary, item)


class RealWorkflowTransitionClassifierTests(TestCase):
    """The classifier must hold for a REAL WorkflowTransition object (the type
    the auto-restore path passes), not only SimpleNamespace stand-ins."""

    def test_real_blocker_resolved_transition_on_blocked_item_classifies(self):
        wf_status = Status.objects.get_or_create(
            slug='in_review', defaults={'name': 'In Review', 'category': 'in_progress'},
        )[0]
        Status.objects.get_or_create(
            slug='blocked', defaults={'name': 'Blocked', 'category': 'blocked'},
        )
        wf = Workflow.objects.create(
            name='PLS577 Real WF', slug='pls577-real', initial_status=wf_status, is_active=True,
        )
        transition = WorkflowTransition.objects.create(
            workflow=wf,
            name='Blocker Resolved - Resume In Review',
            from_status='blocked',
            to_status='in_review',
            position=0,
            validators=list(GATED_VALIDATORS),
            post_functions=[],
        )
        item = _gated_item(status='blocked')
        self.assertTrue(WorkflowService._is_blocker_release_restore(transition, item))
        # And the same transition on a non-blocked item does NOT classify.
        item.status = 'in_progress'
        self.assertFalse(WorkflowService._is_blocker_release_restore(transition, item))

    def test_ordinary_named_transition_never_classifies(self):
        wf_status = Status.objects.get_or_create(
            slug='in_review', defaults={'name': 'In Review', 'category': 'in_progress'},
        )[0]
        wf = Workflow.objects.create(
            name='PLS577 Real WF2', slug='pls577-real2', initial_status=wf_status, is_active=True,
        )
        transition = WorkflowTransition.objects.create(
            workflow=wf, name='Submit for Code Review', from_status='blocked',
            to_status='in_review', position=0, validators=list(GATED_VALIDATORS), post_functions=[],
        )
        item = _gated_item(status='blocked')
        self.assertFalse(WorkflowService._is_blocker_release_restore(transition, item))


@skipUnless(connection.vendor == 'postgresql', 'PLAT-4215 constraint is PostgreSQL-only')
class TerminalBlockerProductionOrderTests(TransactionTestCase):
    """Exercise the exact top-level caller sequence that deadlocked live.

    The fixture deliberately uses real workflow rows, the ItemLink post-save
    auto-block path, ``WorkflowService.execute_transition`` for terminalization,
    and an immediate evaluation of the deferred PostgreSQL constraint.  Direct
    classifier/validator calls cannot prove this transaction boundary.
    """

    reset_sequences = True

    def setUp(self):
        self.actor = User.objects.create_user(
            username='pls577-actor',
            email='pls577-actor@shizuha.com',
            password='test',
            is_staff=True,
        )
        self.author = User.objects.create_user(
            username='pls577-author',
            email='pls577-author@shizuha.com',
            password='test',
        )
        self.reviewer = User.objects.create_user(
            username='pls577-reviewer',
            email='pls577-reviewer@shizuha.com',
            password='test',
            is_staff=True,
        )
        self.direction_reviewer = User.objects.create_user(
            username='pls577-direction-reviewer',
            email='pls577-direction-reviewer@shizuha.com',
            password='test',
        )
        self.security = User.objects.create_user(
            username='pls577-security',
            email='pls577-security@shizuha.com',
            password='test',
        )
        self.project = Project.objects.create(
            name='PLS577 production-order project',
            key='P57',
            org_id=1,
        )
        for team_name, emails in (
            ('engineering', [self.actor.email]),
            ('review', [self.reviewer.email, self.direction_reviewer.email]),
            ('security', [self.security.email]),
        ):
            TeamRoster.objects.update_or_create(
                organization_id=self.project.org_id,
                team_name=team_name,
                defaults={
                    'organization_slug': 'pls577-test',
                    'tiers': [emails],
                    'is_active': True,
                },
            )

        status_specs = {
            'in_progress': ('In Progress', 'in_progress'),
            'proposed': ('Proposed', 'in_progress'),
            'applying': ('Applying', 'in_progress'),
            'in_review': ('In Review', 'in_progress'),
            'blocked': ('Blocked', 'in_progress'),
            'needs_triage': ('Needs Triage', 'todo'),
            'done': ('Done', 'done'),
        }
        statuses = {}
        for slug, (name, category) in status_specs.items():
            statuses[slug], _ = Status.objects.get_or_create(
                slug=slug,
                defaults={'name': name, 'category': category},
            )

        self.workflow = Workflow.objects.create(
            name='PLS577 Production Order',
            slug='pls577-production-order',
            initial_status=statuses['in_progress'],
            is_active=True,
        )
        for position, slug in enumerate(status_specs):
            WorkflowStatus.objects.create(
                workflow=self.workflow,
                status=statuses[slug],
                position=position,
            )

        self.block_transition = WorkflowTransition.objects.create(
            workflow=self.workflow,
            name='Blocked by Linked Issue',
            from_status='in_review',
            to_status='blocked',
            position=0,
        )
        self.restore_transition = WorkflowTransition.objects.create(
            workflow=self.workflow,
            name='Blocker Resolved - Resume In Review',
            from_status='blocked',
            to_status='in_review',
            position=1,
            validators=list(GATED_VALIDATORS),
        )
        self.block_applying_transition = WorkflowTransition.objects.create(
            workflow=self.workflow,
            name='Blocked by Linked Issue',
            from_status='applying',
            to_status='blocked',
            position=2,
        )
        self.restore_applying_transition = WorkflowTransition.objects.create(
            workflow=self.workflow,
            name='Blocker Resolved - Resume Applying',
            from_status='blocked',
            to_status='applying',
            position=3,
        )
        self.complete_transition = WorkflowTransition.objects.create(
            workflow=self.workflow,
            name='Complete Blocker',
            from_status='in_progress',
            to_status='done',
            position=4,
        )
        self.disposition_transition = WorkflowTransition.objects.create(
            workflow=self.workflow,
            name='Request Security Disposition',
            from_status='in_review',
            to_status='needs_triage',
            position=5,
            allowed_roles=['member', 'admin', 'owner'],
            screen_fields=[{
                'type': 'textarea',
                'required': True,
                'label': 'Disposition basis (canonical task/PR/evidence)',
                'field': 'disposition_basis',
            }],
            post_functions=[
                {'type': 'assign_team', 'value': 'security'},
                {
                    'type': 'add_comment',
                    'body': (
                        'Returned from Review to Security triage for an explicit '
                        'disposition; no Review Passed verdict was recorded.'
                    ),
                },
            ],
        )

        self.request_changes_transition = WorkflowTransition.objects.create(
            workflow=self.workflow,
            name='Request Changes',
            from_status='in_review',
            to_status='proposed',
            position=6,
            allowed_roles=['member', 'admin', 'owner'],
            post_functions=[
                {'type': 'assign_team', 'value': 'engineering'},
                {'type': 'assign_implementer'},
                {'type': 'stamp_review_bounce'},
            ],
        )
        self.unrouted_request_changes_transition = WorkflowTransition.objects.create(
            workflow=self.workflow,
            name='Request Changes Without Destination',
            from_status='in_review',
            to_status='proposed',
            position=7,
            allowed_roles=['member', 'admin', 'owner'],
            post_functions=[{'type': 'assign_implementer'}],
        )

    def _dependent_with_stale_direction(self):
        bounce_at = timezone.now() - timedelta(hours=2)
        dependent = Item.objects.create(
            item_key='PLS-577-DEPENDENT',
            title='stale security finding awaiting disposition',
            project=self.project,
            status='in_review',
            workflow_name=self.workflow.slug,
            workflow_status='in_review',
            assignment_group='review',
            assignee_id=self.reviewer.id,
            assignee_email=self.reviewer.email,
            created_by_id=self.author.id,
            created_by_email=self.author.email,
            metadata={
                'pull_requests': [{
                    'url': STALE_PR,
                    'repo': 'shizuha-labs/hive',
                    'number': 35,
                    'status': 'closed',
                    'head_sha': 'stale-head',
                    'last_synced_at': bounce_at.isoformat(),
                }],
                'review_bounce_at': bounce_at.isoformat(),
                'review_bounce_pr_heads': {STALE_PR: 'stale-head'},
            },
        )
        Comment.objects.create(
            item=dependent,
            author_id=self.direction_reviewer.id,
            author_email=self.direction_reviewer.email,
            content='Request Changes: resolve the disposition path before resubmitting.',
        )
        return dependent

    def _team_resolver(self, team_name, **_kwargs):
        if WorkflowService._canonical_team_name(team_name) == 'security':
            return self.security.id, self.security.email
        return self.reviewer.id, self.reviewer.email

    def test_terminal_blocker_restores_once_then_security_disposition_lands(self):
        dependent = self._dependent_with_stale_direction()
        allowed, newer_direction = WorkflowService._submit_has_ack_for_current_direction(
            dependent,
        )
        self.assertFalse(allowed)
        self.assertEqual([comment.author_email for comment in newer_direction], [
            self.direction_reviewer.email,
        ])
        blocker = Item.objects.create(
            item_key='PLS-577-BLOCKER',
            title='owned active workflow-change blocker',
            project=self.project,
            status='in_progress',
            workflow_name=self.workflow.slug,
            workflow_status='in_progress',
            assignment_group='engineering',
            assignee_id=self.actor.id,
            assignee_email=self.actor.email,
            created_by_id=self.actor.id,
            created_by_email=self.actor.email,
        )

        # Production link order: create the active, owned blocker first; the
        # ItemLink signal drives the dependent through the REAL Block transition.
        ItemLink.objects.create(
            source_item=blocker,
            target_item=dependent,
            link_type='blocks',
            created_by_id=self.actor.id,
            created_by_email=self.actor.email,
        )
        dependent.refresh_from_db()
        self.assertEqual(dependent.status, 'blocked')
        self.assertEqual(dependent.metadata['pre_block_state']['status'], 'in_review')
        self.assertEqual(
            dependent.metadata['pre_block_state']['assignee_email'],
            self.reviewer.email,
        )

        with mock.patch.object(
            WorkflowService,
            '_resolve_unblock_assignee',
            return_value=(self.reviewer.id, self.reviewer.email),
        ), mock.patch.object(
            WorkflowService,
            '_resolve_team_assignee',
            side_effect=self._team_resolver,
        ), mock.patch.object(
            WorkflowService,
            '_is_agent_available_for_assignment',
            return_value=True,
        ):
            # The outer transaction plus SET CONSTRAINTS proves the blocker's
            # terminal write and dependent restoration form one valid commit.
            with transaction.atomic():
                WorkflowService.execute_transition(
                    item=blocker,
                    new_status='done',
                    user=self.actor,
                    screen_data={},
                    transition_id=str(self.complete_transition.id),
                )
                with connection.cursor() as cursor:
                    cursor.execute('SET CONSTRAINTS ALL IMMEDIATE')

            blocker.refresh_from_db()
            dependent.refresh_from_db()
            self.assertEqual(blocker.status, 'done')
            self.assertEqual(dependent.status, 'in_review')
            self.assertEqual(dependent.workflow_status, 'In Review')
            self.assertEqual(dependent.assignment_group, 'review')
            self.assertEqual(dependent.assignee_id, self.reviewer.id)
            self.assertEqual(dependent.assignee_email, self.reviewer.email)
            self.assertNotIn('pre_block_state', dependent.metadata)
            self.assertTrue(dependent.metadata['last_auto_unblock']['used_workflow_transition'])
            self.assertEqual(
                Comment.objects.filter(
                    item=dependent,
                    content__startswith='Auto-unblocked:',
                ).count(),
                1,
            )

            basis = 'PLAT-4740 / Origin hive#64 merged and verified; stale hive#35 closed.'
            WorkflowService.execute_transition(
                item=dependent,
                new_status='needs_triage',
                user=self.actor,
                screen_data={'disposition_basis': basis},
                transition_id=str(self.disposition_transition.id),
            )

        dependent.refresh_from_db()
        self.assertEqual(dependent.status, 'needs_triage')
        self.assertEqual(dependent.workflow_status, 'Needs Triage')
        self.assertEqual(dependent.assignment_group, 'security')
        self.assertEqual(dependent.assignee_id, self.security.id)
        self.assertEqual(dependent.assignee_email, self.security.email)
        self.assertTrue(
            Comment.objects.filter(
                item=dependent,
                content__contains='no Review Passed verdict was recorded',
            ).exists()
        )

    def _review_blocker_with_dependent(self, suffix):
        blocker = Item.objects.create(
            item_key=f'PLS-686-BLOCKER-{suffix}',
            title='change request blocking a dependent',
            project=self.project,
            status='in_review',
            workflow_name=self.workflow.slug,
            workflow_status='in_review',
            assignment_group='review',
            assignee_id=self.reviewer.id,
            assignee_email=self.reviewer.email,
            created_by_id=self.author.id,
            created_by_email=self.author.email,
            metadata={'implementation_return_hold': {
                'code': 'invalid_submit_owner',
                'implementation_team': 'architecture',
                'review_generation': 8,
            }},
        )
        dependent = Item.objects.create(
            item_key=f'PLS-686-DEPENDENT-{suffix}',
            title='dependent held by change request',
            project=self.project,
            status='in_review',
            workflow_name=self.workflow.slug,
            workflow_status='in_review',
            assignment_group='review',
            assignee_id=self.direction_reviewer.id,
            assignee_email=self.direction_reviewer.email,
            created_by_id=self.author.id,
            created_by_email=self.author.email,
        )
        ItemLink.objects.create(
            source_item=blocker,
            target_item=dependent,
            link_type='blocks',
            created_by_id=self.actor.id,
            created_by_email=self.actor.email,
        )
        dependent.refresh_from_db()
        self.assertEqual(dependent.status, 'blocked')
        return blocker, dependent

    def test_request_changes_keeps_configured_route_for_active_blocker(self):
        """PLS-686: evaluate the complete production-order transition.

        The active blocker has the live missing-context/no-PR shape.  The real
        Request Changes chain first declares Engineering, then cannot recover a
        historical proposer.  The configured lane must survive through the
        final save so the deferred PLAT-4215 constraint sees one valid owned
        active blocker rather than a transient/unrouted tuple.
        """
        blocker, dependent = self._review_blocker_with_dependent('ROUTED')

        with mock.patch.object(
            WorkflowService,
            '_resolve_team_assignee',
            return_value=(self.actor.id, self.actor.email),
        ), mock.patch.object(
            WorkflowService,
            '_is_agent_available_for_assignment',
            return_value=True,
        ):
            with transaction.atomic():
                WorkflowService.execute_transition(
                    item=blocker,
                    new_status='proposed',
                    user=self.reviewer,
                    screen_data={},
                    transition_id=str(self.request_changes_transition.id),
                )
                with connection.cursor() as cursor:
                    cursor.execute('SET CONSTRAINTS ALL IMMEDIATE')

        blocker.refresh_from_db()
        dependent.refresh_from_db()
        self.assertEqual(blocker.status, 'proposed')
        self.assertEqual(blocker.assignment_group, 'engineering')
        self.assertEqual(blocker.assignee_id, self.actor.id)
        self.assertEqual(blocker.assignee_email, self.actor.email)
        self.assertEqual(dependent.status, 'blocked')
        self.assertTrue(
            ItemLink.objects.filter(
                source_item=blocker,
                target_item=dependent,
                link_type='blocks',
            ).exists()
        )

    def test_request_changes_keeps_group_route_when_no_seat_is_available(self):
        """PLS-686: no-capacity fallback remains team-owned, not naked."""
        blocker, dependent = self._review_blocker_with_dependent('GROUP')

        with mock.patch.object(
            WorkflowService,
            '_resolve_team_assignee',
            return_value=(None, None),
        ), mock.patch.object(
            WorkflowService,
            '_resolve_team_lead_email',
            return_value=None,
        ):
            with transaction.atomic():
                WorkflowService.execute_transition(
                    item=blocker,
                    new_status='proposed',
                    user=self.reviewer,
                    screen_data={},
                    transition_id=str(self.request_changes_transition.id),
                )
                with connection.cursor() as cursor:
                    cursor.execute('SET CONSTRAINTS ALL IMMEDIATE')

        blocker.refresh_from_db()
        dependent.refresh_from_db()
        self.assertEqual(blocker.status, 'proposed')
        self.assertEqual(blocker.assignment_group, 'engineering')
        self.assertIsNone(blocker.assignee_id)
        self.assertIn(blocker.assignee_email, ('', None))
        self.assertEqual(dependent.status, 'blocked')

    def test_request_changes_without_any_destination_still_fails_closed(self):
        """PLS-796 E: PLAT-4215 still rejects a genuinely naked blocker.

        Production-order proof (no ladder mock): clear every durable destination
        signal and unregister engineering for the item org so step 4c cannot
        invent a route. The real ``_durable_implementer_return_group`` then
        returns None and assign_implementer leaves the blocker naked — deferred
        PLAT-4215 must IntegrityError.
        """
        blocker, _dependent = self._review_blocker_with_dependent('UNROUTED')
        # Drop hold/history so steps 4a/4b cannot route; pre-transition group is
        # review (non-implementation). Unregister engineering so 4c is empty.
        blocker.metadata = {}
        blocker.save(update_fields=['metadata'])
        deleted, _ = TeamRoster.objects.filter(
            organization_id=self.project.org_id,
            team_name='engineering',
        ).delete()
        self.assertGreaterEqual(deleted, 1)
        self.assertFalse(
            WorkflowService._team_registered_for_item('engineering', blocker),
        )
        self.assertIsNone(
            WorkflowService._durable_implementer_return_group(blocker),
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            WorkflowService.execute_transition(
                item=blocker,
                new_status='proposed',
                user=self.reviewer,
                screen_data={},
                transition_id=str(self.unrouted_request_changes_transition.id),
            )
            with connection.cursor() as cursor:
                cursor.execute('SET CONSTRAINTS ALL IMMEDIATE')

    def test_request_changes_uses_hold_team_when_context_and_assign_team_missing(self):
        """PLS-796 B/4a: hold.implementation_team owns the bounce without assign_team."""
        blocker, dependent = self._review_blocker_with_dependent('HOLDTEAM')

        with mock.patch.object(
            WorkflowService,
            '_resolve_team_assignee',
            side_effect=self._team_resolver,
        ), mock.patch.object(
            WorkflowService,
            '_is_agent_available_for_assignment',
            return_value=True,
        ), mock.patch.object(
            WorkflowService,
            '_is_team_member',
            return_value=True,
        ):
            # architecture is not on the fixture roster; force resolver via security
            # by rewriting hold team to security for a concrete seat proof.
            md = dict(blocker.metadata or {})
            md['implementation_return_hold'] = {
                'code': 'invalid_submit_owner',
                'implementation_team': 'security',
                'review_generation': 8,
            }
            blocker.metadata = md
            blocker.save(update_fields=['metadata'])

            with transaction.atomic():
                WorkflowService.execute_transition(
                    item=blocker,
                    new_status='proposed',
                    user=self.reviewer,
                    screen_data={},
                    transition_id=str(self.unrouted_request_changes_transition.id),
                )
                with connection.cursor() as cursor:
                    cursor.execute('SET CONSTRAINTS ALL IMMEDIATE')

        blocker.refresh_from_db()
        dependent.refresh_from_db()
        self.assertEqual(blocker.status, 'proposed')
        self.assertEqual(blocker.assignment_group, 'security')
        self.assertEqual(blocker.assignee_id, self.security.id)
        self.assertEqual(blocker.assignee_email, self.security.email)
        self.assertEqual(dependent.status, 'blocked')

    def test_request_changes_defaults_to_engineering_without_destination_signals(self):
        """PLS-796 B/4c: review lane + empty metadata still group-routes to engineering.

        4c uses the project/workflow default implementer lane (not invent-Engineering).
        This fixture workflow is custom, so set the project default explicitly.
        """
        self.project.settings = {'default_implementer_group': 'engineering'}
        self.project.save(update_fields=['settings'])
        blocker, dependent = self._review_blocker_with_dependent('DEFAULTENG')
        blocker.metadata = {}
        blocker.save(update_fields=['metadata'])

        with mock.patch.object(
            WorkflowService,
            '_resolve_team_assignee',
            return_value=(self.actor.id, self.actor.email),
        ), mock.patch.object(
            WorkflowService,
            '_is_agent_available_for_assignment',
            return_value=True,
        ):
            with transaction.atomic():
                WorkflowService.execute_transition(
                    item=blocker,
                    new_status='proposed',
                    user=self.reviewer,
                    screen_data={},
                    transition_id=str(self.unrouted_request_changes_transition.id),
                )
                with connection.cursor() as cursor:
                    cursor.execute('SET CONSTRAINTS ALL IMMEDIATE')

        blocker.refresh_from_db()
        dependent.refresh_from_db()
        self.assertEqual(blocker.status, 'proposed')
        self.assertEqual(blocker.assignment_group, 'engineering')
        self.assertEqual(blocker.assignee_id, self.actor.id)
        self.assertEqual(dependent.status, 'blocked')

    def test_request_changes_preserves_pre_transition_non_review_owner(self):
        """PLS-796 C (CTX-528): pre-transition non-review owner+team is not wiped."""
        blocker, dependent = self._review_blocker_with_dependent('CTX528')
        # Live shape: concrete non-review seat already on the item; no stash,
        # no same-transition assign_team, empty durable metadata.
        blocker.assignment_group = 'security'
        blocker.assignee_id = self.security.id
        blocker.assignee_email = self.security.email
        blocker.metadata = {}
        blocker.save(update_fields=[
            'assignment_group', 'assignee_id', 'assignee_email', 'metadata',
        ])

        with mock.patch.object(
            WorkflowService,
            '_is_agent_available_for_assignment',
            return_value=True,
        ), mock.patch.object(
            WorkflowService,
            '_is_team_member',
            return_value=True,
        ):
            with transaction.atomic():
                WorkflowService.execute_transition(
                    item=blocker,
                    new_status='proposed',
                    user=self.reviewer,
                    screen_data={},
                    transition_id=str(self.unrouted_request_changes_transition.id),
                )
                with connection.cursor() as cursor:
                    cursor.execute('SET CONSTRAINTS ALL IMMEDIATE')

        blocker.refresh_from_db()
        dependent.refresh_from_db()
        self.assertEqual(blocker.status, 'proposed')
        self.assertEqual(blocker.assignment_group, 'security')
        self.assertEqual(blocker.assignee_id, self.security.id)
        self.assertEqual(blocker.assignee_email, self.security.email)
        self.assertEqual(dependent.status, 'blocked')

    def test_request_changes_unrouted_no_capacity_stays_group_owned(self):
        """PLS-796 D: no seat still leaves a durable group route (not naked)."""
        blocker, dependent = self._review_blocker_with_dependent('NOSEAT')
        # hold team security (rostered) → step 4a; resolver returns no seat.
        md = dict(blocker.metadata or {})
        md['implementation_return_hold'] = {
            'code': 'invalid_submit_owner',
            'implementation_team': 'security',
            'review_generation': 8,
        }
        blocker.metadata = md
        blocker.save(update_fields=['metadata'])

        with mock.patch.object(
            WorkflowService,
            '_resolve_team_assignee',
            return_value=(None, None),
        ), mock.patch.object(
            WorkflowService,
            '_resolve_team_lead_email',
            return_value=None,
        ):
            with transaction.atomic():
                WorkflowService.execute_transition(
                    item=blocker,
                    new_status='proposed',
                    user=self.reviewer,
                    screen_data={},
                    transition_id=str(self.unrouted_request_changes_transition.id),
                )
                with connection.cursor() as cursor:
                    cursor.execute('SET CONSTRAINTS ALL IMMEDIATE')

        blocker.refresh_from_db()
        dependent.refresh_from_db()
        self.assertEqual(blocker.status, 'proposed')
        self.assertEqual(blocker.assignment_group, 'security')
        self.assertIn(blocker.assignee_id, (None, 0))
        self.assertFalse((blocker.assignee_email or '').strip())
        self.assertEqual(dependent.status, 'blocked')

    def test_unregistered_hold_and_history_fall_through_to_engineering(self):
        """PLS-796: phantom metadata groups are not routes; 4c still works."""
        self.project.settings = {'default_implementer_group': 'engineering'}
        self.project.save(update_fields=['settings'])
        blocker, dependent = self._review_blocker_with_dependent('PHANTOM')
        md = {
            'implementation_return_hold': {
                'code': 'invalid_submit_owner',
                'implementation_team': 'not-a-real-team',
                'review_generation': 1,
            },
            'focused_qa_assignment_group': 'ghost-pod',
            'block_history': [{
                'status': 'in_progress',
                'assignment_group': 'foreign-org-team',
            }],
        }
        blocker.metadata = md
        blocker.save(update_fields=['metadata'])
        self.assertIsNone(
            WorkflowService._accepted_implementer_group('not-a-real-team', blocker),
        )
        self.assertEqual(
            WorkflowService._durable_implementer_return_group(blocker),
            'engineering',
        )

        with mock.patch.object(
            WorkflowService,
            '_resolve_team_assignee',
            return_value=(self.actor.id, self.actor.email),
        ), mock.patch.object(
            WorkflowService,
            '_is_agent_available_for_assignment',
            return_value=True,
        ):
            with transaction.atomic():
                WorkflowService.execute_transition(
                    item=blocker,
                    new_status='proposed',
                    user=self.reviewer,
                    screen_data={},
                    transition_id=str(self.unrouted_request_changes_transition.id),
                )
                with connection.cursor() as cursor:
                    cursor.execute('SET CONSTRAINTS ALL IMMEDIATE')

        blocker.refresh_from_db()
        dependent.refresh_from_db()
        self.assertEqual(blocker.assignment_group, 'engineering')
        self.assertEqual(blocker.assignee_id, self.actor.id)
        self.assertEqual(dependent.status, 'blocked')

    def test_unregistered_versioned_context_falls_through_not_fail_open(self):
        """PLS-796: versioned context with unregistered team is not a route."""
        self.project.settings = {'default_implementer_group': 'engineering'}
        self.project.save(update_fields=['settings'])
        blocker, dependent = self._review_blocker_with_dependent('BADCTX')
        blocker.metadata = {
            WorkflowService._IMPLEMENTATION_RETURN_CONTEXT_KEY: {
                'version': WorkflowService._IMPLEMENTATION_RETURN_CONTEXT_VERSION,
                'implementation_team': 'phantom-lane',
                'implementer': {
                    'id': self.actor.id,
                    'email': self.actor.email,
                },
                'review_generation': 3,
            },
        }
        blocker.save(update_fields=['metadata'])
        ctx, err = WorkflowService._implementation_return_context(blocker)
        self.assertIsNone(ctx)
        self.assertEqual(err, 'unregistered_return_context_team')

        with mock.patch.object(
            WorkflowService,
            '_resolve_team_assignee',
            return_value=(self.actor.id, self.actor.email),
        ), mock.patch.object(
            WorkflowService,
            '_is_agent_available_for_assignment',
            return_value=True,
        ):
            with transaction.atomic():
                WorkflowService.execute_transition(
                    item=blocker,
                    new_status='proposed',
                    user=self.reviewer,
                    screen_data={},
                    transition_id=str(self.unrouted_request_changes_transition.id),
                )
                with connection.cursor() as cursor:
                    cursor.execute('SET CONSTRAINTS ALL IMMEDIATE')

        blocker.refresh_from_db()
        dependent.refresh_from_db()
        # Falls through to 4c project/workflow default engineering, not phantom-lane.
        self.assertEqual(blocker.assignment_group, 'engineering')
        self.assertEqual(dependent.status, 'blocked')

    def test_chained_release_restores_owned_route_when_status_lane_is_unknown(self):
        """Mirror PLS-577 -> PLS-573 -> PLAT-4668 in production order.

        ``applying`` intentionally has no status-lane mapping and the captured
        pre-block state has no assignment group.  Before the regression fix,
        the resume found the recorded Engineering owner but applied it while
        the item was still groupless; the PLS-454 write guard returned
        ``team_unknown``, unassigned the active middle blocker, and deferred
        PLAT-4215 rejected the outer blocker's terminalization.
        """
        downstream = self._dependent_with_stale_direction()
        middle = Item.objects.create(
            item_key='PLS-577-MIDDLE',
            title='workflow change applying and blocking security finding',
            project=self.project,
            status='applying',
            workflow_name=self.workflow.slug,
            workflow_status='applying',
            assignment_group='engineering',
            assignee_id=self.actor.id,
            assignee_email=self.actor.email,
            created_by_id=self.actor.id,
            created_by_email=self.actor.email,
        )
        outer = Item.objects.create(
            item_key='PLS-577-OUTER',
            title='terminal blocker releasing workflow change',
            project=self.project,
            status='in_progress',
            workflow_name=self.workflow.slug,
            workflow_status='in_progress',
            assignment_group='engineering',
            assignee_id=self.actor.id,
            assignee_email=self.actor.email,
            created_by_id=self.actor.id,
            created_by_email=self.actor.email,
        )

        ItemLink.objects.create(
            source_item=middle,
            target_item=downstream,
            link_type='blocks',
            created_by_id=self.actor.id,
            created_by_email=self.actor.email,
        )
        ItemLink.objects.create(
            source_item=outer,
            target_item=middle,
            link_type='blocks',
            created_by_id=self.actor.id,
            created_by_email=self.actor.email,
        )
        middle.refresh_from_db()
        downstream.refresh_from_db()
        self.assertEqual(middle.status, 'blocked')
        self.assertEqual(middle.metadata['pre_block_state']['status'], 'applying')
        self.assertEqual(downstream.status, 'blocked')

        # Reproduce the live blocked legacy row after the real ItemLink path:
        # the stored pre-block owner is valid, but both the current route and
        # captured route are absent.  A blocked dependent may be unowned because
        # its active outer blocker is the durable owner.  QuerySet.update is
        # intentional fixture setup; the production operation under test is the
        # subsequent terminal transition and its transactional auto-restore.
        pre_block = dict(middle.metadata['pre_block_state'])
        pre_block['assignment_group'] = None
        metadata = dict(middle.metadata)
        metadata['pre_block_state'] = pre_block
        with mock.patch.object(
            WorkflowService,
            '_is_agent_available_for_assignment',
            return_value=True,
        ):
            with transaction.atomic():
                Item.objects.filter(pk=middle.pk).update(
                    assignment_group=None,
                    assignee_id=None,
                    assignee_email='',
                    metadata=metadata,
                )
                middle.refresh_from_db()
                self.assertIsNone(
                    middle.metadata['pre_block_state']['assignment_group']
                )
                WorkflowService.execute_transition(
                    item=outer,
                    new_status='done',
                    user=self.actor,
                    screen_data={},
                    transition_id=str(self.complete_transition.id),
                )
                with connection.cursor() as cursor:
                    cursor.execute('SET CONSTRAINTS ALL IMMEDIATE')

        outer.refresh_from_db()
        middle.refresh_from_db()
        downstream.refresh_from_db()
        self.assertEqual(outer.status, 'done')
        self.assertEqual(middle.status, 'applying')
        self.assertEqual(middle.workflow_status, 'Applying')
        self.assertEqual(middle.assignment_group, 'engineering')
        self.assertEqual(middle.assignee_id, self.actor.id)
        self.assertEqual(middle.assignee_email, self.actor.email)
        self.assertNotIn('pre_block_state', middle.metadata)
        self.assertEqual(downstream.status, 'blocked')


@skipUnless(connection.vendor == 'postgresql', 'PLAT-4215 constraint is PostgreSQL-only')
class PLS953LastBlockerReleaseTests(TransactionTestCase):
    """Real caller sequence for the live todo/no-resume-transition deadlock."""

    reset_sequences = True

    def setUp(self):
        self.actor = User.objects.create_user(
            username='pls953-actor',
            email='pls953-actor@shizuha.com',
            password='test',
            is_staff=True,
        )
        self.project = Project.objects.create(
            name='PLS953 release project', key='P953', org_id=1,
        )
        TeamRoster.objects.update_or_create(
            organization_id=self.project.org_id,
            team_name='engineering',
            defaults={
                'organization_slug': 'pls953-test',
                'tiers': [[self.actor.email]],
                'is_active': True,
            },
        )
        statuses = {}
        for slug, name, category in (
            ('todo', 'To Do', 'todo'),
            ('in_progress', 'In Progress', 'in_progress'),
            ('blocked', 'Blocked', 'in_progress'),
            ('done', 'Done', 'done'),
        ):
            statuses[slug], _ = Status.objects.get_or_create(
                slug=slug,
                defaults={'name': name, 'category': category},
            )
        self.workflow = Workflow.objects.create(
            name='PLS953 Direct Restore',
            slug='pls953-direct-restore',
            initial_status=statuses['todo'],
            is_active=True,
        )
        for position, slug in enumerate(statuses):
            WorkflowStatus.objects.create(
                workflow=self.workflow,
                status=statuses[slug],
                position=position,
            )
        WorkflowTransition.objects.create(
            workflow=self.workflow,
            name='Blocked by Linked Issue',
            from_status='todo',
            to_status='blocked',
            position=0,
        )
        # Deliberately no blocked -> todo transition: SCS-142 used the direct
        # fallback and exposed the swallowed/False restore bug.
        self.complete_transition = WorkflowTransition.objects.create(
            workflow=self.workflow,
            name='Complete Blocker',
            from_status='in_progress',
            to_status='done',
            position=1,
        )

    def _active_pair(self, suffix):
        blocker = Item.objects.create(
            item_key=f'P953-B-{suffix}', title='owned active blocker',
            project=self.project, status='in_progress',
            workflow_name=self.workflow.slug, workflow_status='in_progress',
            assignment_group='engineering', assignee_id=self.actor.id,
            assignee_email=self.actor.email, created_by_id=self.actor.id,
            created_by_email=self.actor.email,
        )
        dependent = Item.objects.create(
            item_key=f'P953-D-{suffix}',
            title='todo dependent without explicit resume edge',
            project=self.project, status='todo',
            workflow_name=self.workflow.slug, workflow_status='todo',
            assignment_group='engineering', assignee_id=self.actor.id,
            assignee_email=self.actor.email, created_by_id=self.actor.id,
            created_by_email=self.actor.email,
        )
        link = ItemLink.objects.create(
            source_item=blocker, target_item=dependent, link_type='blocks',
            created_by_id=self.actor.id, created_by_email=self.actor.email,
        )
        dependent.refresh_from_db()
        self.assertEqual(dependent.status, 'blocked')
        self.assertEqual(dependent.metadata['pre_block_state']['status'], 'todo')
        self.assertTrue(dependent.metadata['pre_block_state']['had_active_blocker'])
        self.assertFalse(WorkflowTransition.objects.filter(
            workflow=self.workflow,
            from_status='blocked',
            to_status='todo',
        ).exists())
        return blocker, dependent, link

    def _complete(self, blocker):
        with transaction.atomic():
            WorkflowService.execute_transition(
                item=blocker, new_status='done', user=self.actor,
                screen_data={}, transition_id=str(self.complete_transition.id),
            )
            with connection.cursor() as cursor:
                cursor.execute('SET CONSTRAINTS ALL IMMEDIATE')

    def test_terminal_release_direct_fallback_restores_todo(self):
        blocker, dependent, _link = self._active_pair('TERMINAL')
        self._complete(blocker)
        blocker.refresh_from_db()
        dependent.refresh_from_db()
        self.assertEqual(blocker.status, 'done')
        self.assertEqual(dependent.status, 'todo')
        self.assertEqual(dependent.workflow_status, 'To Do')
        self.assertFalse(
            dependent.metadata['last_auto_unblock']['used_workflow_transition']
        )

    def test_terminal_release_false_restore_rolls_back_graph(self):
        blocker, dependent, link = self._active_pair('FALSE')
        with mock.patch.object(
            WorkflowService, '_restore_blocked_item', return_value=False,
        ), self.assertRaises(WorkflowValidationError) as raised:
            self._complete(blocker)
        self.assertEqual(raised.exception.code, 'dependency_release_rejected')
        blocker.refresh_from_db()
        dependent.refresh_from_db()
        self.assertEqual(blocker.status, 'in_progress')
        self.assertEqual(dependent.status, 'blocked')
        self.assertTrue(ItemLink.objects.filter(pk=link.pk).exists())

    def test_terminal_release_exception_rolls_back_graph(self):
        blocker, dependent, link = self._active_pair('RAISE')
        with mock.patch.object(
            WorkflowService,
            '_restore_blocked_item',
            side_effect=RuntimeError('forced restore exception'),
        ), self.assertRaises(WorkflowValidationError) as raised:
            self._complete(blocker)
        self.assertEqual(raised.exception.code, 'dependency_release_rejected')
        blocker.refresh_from_db()
        dependent.refresh_from_db()
        self.assertEqual(blocker.status, 'in_progress')
        self.assertEqual(dependent.status, 'blocked')
        self.assertTrue(ItemLink.objects.filter(pk=link.pk).exists())


@skipUnless(connection.vendor == 'postgresql', 'PLAT-4215 constraint is PostgreSQL-only')
class PLS796CrossWorkflowRequestChangesProductionOrderTests(TransactionTestCase):
    """PLS-796 §4.1 A–E matrix on autonomous-dev / autonomous-bug / reviewed-task.

    Uses **source-owned production** Request Changes rows only (migration seeds).
    Never creates substitute workflows/transitions — missing or wrong production
    chain fails the test (rei P1). PostgreSQL production-order: real
    execute_transition + SET CONSTRAINTS ALL IMMEDIATE.

    TransactionTestCase flushes migration rows between tests. We re-apply the
    source seed functions in setUp (same code paths as migrations 0071/0141)
    rather than ``serialized_rollback``, which collides on contenttypes under
    parallel CI clones.
    """

    reset_sequences = True

    # Locked live carriers (wiki §4.1 / migration 0119 + 0141).
    WORKFLOW_SPECS = (
        {
            'slug': 'reviewed-task',
            'return_status': 'in_progress',
            # Seed 0071: assign_implementer then notify.
            'required_pf_types': ('assign_implementer', 'send_notification'),
            'forbidden_pf_types': ('assign_team',),
        },
        {
            'slug': 'autonomous-dev',
            'return_status': 'in_progress',
            # Seed 0141: stamp → assign_implementer → notify.
            'required_pf_types': (
                'stamp_review_bounce', 'assign_implementer', 'send_notification',
            ),
            'forbidden_pf_types': ('assign_team',),
        },
        {
            'slug': 'autonomous-bug',
            'return_status': 'in_progress',
            # Seed 0141: stamp → assign_implementer → notify.
            'required_pf_types': (
                'stamp_review_bounce', 'assign_implementer', 'send_notification',
            ),
            'forbidden_pf_types': ('assign_team',),
        },
    )

    def setUp(self):
        import importlib
        import uuid as _uuid
        from django.apps import apps as django_apps
        # Re-apply source seeds after TransactionTestCase flush. These are the
        # same RunPython callables migrations own — not substitute fixtures.
        # (Migration modules start with digits → importlib, not bare import.)
        seed_reviewed_task = importlib.import_module(
            'tasks.migrations.0071_seed_reviewed_task_workflow',
        ).seed_reviewed_task
        seed_autonomous_bounce_workflows = importlib.import_module(
            'tasks.migrations.0141_pls796_seed_autonomous_bounce_workflows',
        ).seed_autonomous_bounce_workflows
        seed_reviewed_task(django_apps, None)
        seed_autonomous_bounce_workflows(django_apps, None)

        tag = _uuid.uuid4().hex[:8]
        self.actor = User.objects.create_user(
            username=f'pls796-xwf-actor-{tag}',
            email=f'pls796-xwf-actor-{tag}@shizuha.com',
            password='test',
            is_staff=True,
        )
        self.reviewer = User.objects.create_user(
            username=f'pls796-xwf-reviewer-{tag}',
            email=f'pls796-xwf-reviewer-{tag}@shizuha.com',
            password='test',
            is_staff=True,
        )
        # A reviewer seated on a NON-`review` gating lane. The live PLS-468 shape
        # is an autonomous-dev item held at in_review on the exact-org `security`
        # queue, which the `review`-named lanes above cannot reproduce.
        self.gate_reviewer = User.objects.create_user(
            username=f'pls796-xwf-gate-{tag}',
            email=f'pls796-xwf-gate-{tag}@shizuha.com',
            password='test',
            is_staff=True,
        )
        self.project = Project.objects.create(
            name=f'PLS796 cross-workflow project {tag}',
            key=f'P{tag[:4].upper()}',
            org_id=1,
            # Authoritative 4c default (not invent-from-registration).
            settings={'default_implementer_group': 'engineering'},
        )
        for team_name, emails in (
            ('engineering', [self.actor.email]),
            ('review', [self.reviewer.email]),
            ('architecture', [self.actor.email]),
            # Registered exact-org on purpose. If these lanes were absent the
            # resolver would fall through to the engineering default anyway and
            # cases F–H would pass without the fix — a green proving nothing.
            ('security', [self.gate_reviewer.email]),
            ('qa', [self.gate_reviewer.email]),
        ):
            TeamRoster.objects.update_or_create(
                organization_id=self.project.org_id,
                team_name=team_name,
                defaults={
                    # Must match org_id=1's canonical slug for assignee write-guard.
                    'organization_slug': 'shizuha',
                    'tiers': [emails],
                    'is_active': True,
                },
            )
        WorkflowService._invalidate_team_roster_caches()

    def _require_production_request_changes(self, spec):
        """Load production Request Changes; fail if absent or chain differs.

        Never creates substitute workflows/transitions (rei P1).
        Requires exact from/to edge and full ordered PF sequence.
        """
        workflow = Workflow.objects.filter(slug=spec['slug']).first()
        self.assertIsNotNone(
            workflow,
            f"production workflow {spec['slug']!r} missing — migration seed required",
        )
        transition = WorkflowTransition.objects.filter(
            workflow=workflow,
            name='Request Changes',
            from_status='in_review',
            to_status=spec['return_status'],
        ).first()
        self.assertIsNotNone(
            transition,
            f"{spec['slug']} production Request Changes "
            f"in_review→{spec['return_status']} row missing",
        )
        pf_types = [
            pf.get('type')
            for pf in (transition.post_functions or [])
            if isinstance(pf, dict) and pf.get('type')
        ]
        required = list(spec['required_pf_types'])
        # Full ordered sequence: required types must appear in order as a
        # contiguous subsequence of the production chain (not merely present).
        try:
            start = pf_types.index(required[0])
        except ValueError:
            self.fail(
                f"{spec['slug']} production Request Changes missing {required[0]}; "
                f"got ordered types {pf_types}"
            )
        window = pf_types[start:start + len(required)]
        self.assertEqual(
            window,
            required,
            f"{spec['slug']} production pf order wrong: {pf_types} "
            f"(need contiguous {required})",
        )
        for forbidden in spec.get('forbidden_pf_types') or ():
            self.assertNotIn(
                forbidden,
                pf_types,
                f"{spec['slug']} Request Changes must not include {forbidden}",
            )
        return workflow, transition

    def _force_assign(self, item_id, *, assignee_id, assignee_email, group):
        """Set ownership under the PG assignment-org proof fence."""
        proof = '|'.join([
            '1',  # org_id
            str(self.project.id),
            str(assignee_id or 0),
            (assignee_email or '').strip().lower(),
            group,
        ])
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT set_config('pulse.assignment_org_proof', %s, true)",
                    [proof],
                )
            Item.objects.filter(pk=item_id).update(
                assignee_id=assignee_id,
                assignee_email=assignee_email,
                assignment_group=group,
            )

    def _pair(self, workflow, suffix, *, group='review', assignee=None, metadata=None):
        owner = assignee or self.reviewer
        # Create without assignee first, then force-assign under the org proof
        # fence (PG trigger + Item.save guard).
        blocker = Item.objects.create(
            item_key=f"PLS796-XWF-{workflow.slug}-{suffix}-B",
            title=f"PLS-796 {workflow.slug} {suffix} blocker",
            project=self.project,
            status='in_review',
            workflow_name=workflow.slug,
            workflow_status='in_review',
            assignment_group=group,
            created_by_id=self.actor.id,
            created_by_email=self.actor.email,
            metadata=metadata if metadata is not None else {},
        )
        self._force_assign(
            blocker.pk,
            assignee_id=owner.id,
            assignee_email=owner.email,
            group=group,
        )
        blocker.refresh_from_db()
        dependent = Item.objects.create(
            item_key=f"PLS796-XWF-{workflow.slug}-{suffix}-D",
            title=f"PLS-796 {workflow.slug} {suffix} dependent",
            project=self.project,
            status='in_review',
            workflow_name=workflow.slug,
            workflow_status='in_review',
            assignment_group='review',
            created_by_id=self.actor.id,
            created_by_email=self.actor.email,
        )
        self._force_assign(
            dependent.pk,
            assignee_id=self.reviewer.id,
            assignee_email=self.reviewer.email,
            group='review',
        )
        dependent.refresh_from_db()
        ItemLink.objects.create(
            source_item=blocker,
            target_item=dependent,
            link_type='blocks',
            created_by_id=self.actor.id,
            created_by_email=self.actor.email,
        )
        dependent.refresh_from_db()
        if dependent.status != 'blocked':
            # Production may lack "Blocked by Linked Issue" from in_review on
            # some workflows; A–E still needs a blocked dependent for PLAT-4215.
            # Raw SQL avoids PLS-467 state-mutation marker (same pattern as
            # test_plat4215_team_owned_blocker).
            import json as _json
            pre = {
                'status': 'in_review',
                'workflow_status': 'in_review',
                'assignment_group': 'review',
                'assignee_id': self.reviewer.id,
                'assignee_email': self.reviewer.email,
            }
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE tasks_task SET status='blocked', "
                    "workflow_status='blocked', "
                    "metadata = COALESCE(metadata, '{}'::jsonb) || %s::jsonb "
                    "WHERE id=%s",
                    [_json.dumps({'pre_block_state': pre}), dependent.pk],
                )
            dependent.refresh_from_db()
        self.assertEqual(dependent.status, 'blocked')
        return blocker, dependent

    def _fire(
        self, blocker, request_changes, *, resolve_to=None, available=True,
        fired_by=None,
    ):
        """Execute production Request Changes under forced deferred constraints.

        ``fired_by`` is the party recording the verdict. It defaults to the
        ``review`` lane, but the PLS-468 shape requires firing as the gating lane
        that HOLDS the item — there, Security both authored the verdict and fired
        the transition, which is precisely what distinguishes it from CTX-528.
        """
        resolve = resolve_to if resolve_to is not None else (
            self.actor.id, self.actor.email
        )
        executor = fired_by or self.reviewer
        with mock.patch.object(
            WorkflowService,
            '_resolve_team_assignee',
            return_value=resolve,
        ), mock.patch.object(
            WorkflowService,
            '_is_agent_available_for_assignment',
            return_value=available,
        ), mock.patch.object(
            WorkflowService,
            '_is_team_member',
            return_value=True,
        ):
            with transaction.atomic():
                WorkflowService.execute_transition(
                    item=blocker,
                    new_status=request_changes.to_status,
                    user=executor,
                    screen_data={'comment': 'PLS-796 cross-workflow A-E'},
                    transition_id=str(request_changes.id),
                )
                with connection.cursor() as cursor:
                    cursor.execute('SET CONSTRAINTS ALL IMMEDIATE')

    def _assert_bounce_ok(self, blocker, dependent, request_changes, *,
                          group='engineering', assignee_id=None,
                          allow_group_queue=False, link_count=1):
        blocker.refresh_from_db()
        dependent.refresh_from_db()
        self.assertEqual(blocker.status, request_changes.to_status)
        self.assertEqual(blocker.assignment_group, group)
        if assignee_id is None:
            self.assertIn(blocker.assignee_id, (None, 0))
        elif allow_group_queue and blocker.assignee_id in (None, 0):
            # Ladder destination is the implementer lane; concrete seat may be
            # group-queued by the write-guard while group is correct (contract
            # §2.2 step 4: group-queue OK).
            pass
        else:
            self.assertEqual(blocker.assignee_id, assignee_id)
        # Bounce stamp present when stamp_review_bounce is on the chain.
        meta = blocker.metadata or {}
        if any(
            isinstance(pf, dict) and pf.get('type') == 'stamp_review_bounce'
            for pf in (request_changes.post_functions or [])
        ):
            self.assertTrue(
                meta.get('review_bounce_at'),
                'stamp_review_bounce must record review_bounce_at',
            )
        # Dependent stays blocked; blocker link unchanged.
        self.assertEqual(dependent.status, 'blocked')
        self.assertEqual(
            ItemLink.objects.filter(
                source_item=blocker, target_item=dependent, link_type='blocks',
            ).count(),
            link_count,
        )

    def _run_matrix_case(self, spec, case):
        workflow, request_changes = self._require_production_request_changes(spec)
        if case == 'A':
            # Valid versioned stash → owned implementer lane.
            md = {
                WorkflowService._IMPLEMENTATION_RETURN_CONTEXT_KEY: {
                    'version': WorkflowService._IMPLEMENTATION_RETURN_CONTEXT_VERSION,
                    'implementation_team': 'engineering',
                    'implementer': {
                        'id': self.actor.id,
                        'email': self.actor.email,
                    },
                    'review_generation': 2,
                },
            }
            blocker, dependent = self._pair(workflow, 'A', metadata=md)
            self._fire(blocker, request_changes)
            self._assert_bounce_ok(
                blocker, dependent, request_changes,
                assignee_id=self.actor.id, allow_group_queue=True,
            )
        elif case == 'B':
            # Missing/invalid context → 4c project/workflow default engineering.
            blocker, dependent = self._pair(workflow, 'B', metadata={})
            self._fire(blocker, request_changes)
            self._assert_bounce_ok(
                blocker, dependent, request_changes,
                assignee_id=self.actor.id, allow_group_queue=True,
            )
        elif case == 'C':
            # Pre-transition non-review owner preserved.
            blocker, dependent = self._pair(
                workflow, 'C', group='engineering', assignee=self.actor, metadata={},
            )
            self._fire(blocker, request_changes)
            self._assert_bounce_ok(
                blocker, dependent, request_changes,
                assignee_id=self.actor.id, allow_group_queue=True,
            )
        elif case == 'D':
            # No capacity → group-queued engineering.
            blocker, dependent = self._pair(workflow, 'D', metadata={})
            self._fire(
                blocker, request_changes, resolve_to=(None, None), available=False,
            )
            self._assert_bounce_ok(
                blocker, dependent, request_changes, assignee_id=None,
            )
        elif case in ('F', 'G', 'H'):
            # The live PLS-468 shape, and its Architecture/QA equivalents.
            #
            # An item held at `in_review` on a NON-`review` gating lane, with no
            # valid return context and no same-transition assign_team. Before the
            # fix, ladder step 3 accepted the item's current assignment_group —
            # exact-org registered and not on the `review`/`merge`/admin name
            # list — so Request Changes bounced the work straight back onto the
            # queue of the very team that rejected it, and never reached the
            # workflow default. PLAT-4215 stopped 409ing while the handoff stayed
            # broken (@rui, PR #179 P1).
            #
            # Cases B/D/E all seat their blocker on `review`, so none of them can
            # observe this: the name list masked it. The distinguishing signal is
            # the STAGE the transition departed, not the lane's name.
            #
            # The rejecter must BE the holding lane. On the live carrier
            # Security both authored the verdict and fired the transition; that
            # self-bounce is the whole signal. Firing as the `review` lane
            # instead would reproduce CTX-528's shape with the team renamed —
            # a distinct party rejecting an item someone else owns — which the
            # contract requires to PRESERVE, so it would assert the opposite of
            # this case and no resolver could satisfy both.
            lane = {'F': 'security', 'G': 'architecture', 'H': 'qa'}[case]
            blocker, dependent = self._pair(
                workflow, case,
                group=lane, assignee=self.gate_reviewer, metadata={},
            )
            self.assertEqual(
                blocker.assignment_group, lane,
                f'fixture precondition: blocker must start held by {lane}',
            )
            self.assertEqual(
                blocker.assignee_id, self.gate_reviewer.id,
                'fixture precondition: the rejecter must hold the seat it vacates',
            )
            self._fire(blocker, request_changes, fired_by=self.gate_reviewer)
            # Must land on the authoritative implementer lane/default...
            self._assert_bounce_ok(
                blocker, dependent, request_changes,
                group='engineering', assignee_id=self.actor.id,
                allow_group_queue=True,
            )
            blocker.refresh_from_db()
            # ...and must not pin the reviewer who rejected it.
            self.assertNotEqual(
                blocker.assignee_id, self.gate_reviewer.id,
                f'Request Changes must not return the item to its {lane} reviewer',
            )
        elif case == 'I':
            # The OTHER polarity of F, and the reason this cannot be "any gating
            # lane holding at in_review is a reviewer".
            #
            # Same workflow, same departed status, same empty metadata, same
            # `security` group as F — but a DISTINCT party fires the bounce. That
            # is CTX-528's shape in production order: Security owned the work and
            # the review lane rejected it, so the owner must survive.
            #
            # F and I differ in exactly one variable (who fired), so a fix that
            # passes both cannot be keyed on the lane's name or on the departed
            # stage alone. Without this case, F would also pass under the strictly
            # wrong rule "never preserve a gating lane departing a review stage",
            # which silently wipes implementer ownership.
            blocker, dependent = self._pair(
                workflow, case,
                group='security', assignee=self.gate_reviewer, metadata={},
            )
            self._fire(blocker, request_changes, fired_by=self.reviewer)
            blocker.refresh_from_db()
            dependent.refresh_from_db()
            self.assertEqual(
                blocker.assignment_group, 'security',
                'a non-review owner rejected by a DISTINCT party is implementer '
                'provenance and must be preserved (CTX-528 shape)',
            )
            # Deliberately NOT asserting the concrete seat here.
            #
            # Measured on this fixture: F/G/H land `assignee=None` on
            # reviewed-task but a concrete seat on autonomous-dev/bug, so seat
            # resolution through `_pair` varies by workflow independently of this
            # fix. Asserting it would measure the harness rather than the
            # contract, and §2.2(1) is explicit that a valid `assignment_group`
            # is owned — group-queued is a pass.
            #
            # Concrete-seat preservation IS covered, by the locked CTX-528 test
            # `test_request_changes_preserves_pre_transition_non_review_owner`,
            # which asserts assignee_id/email survive and passes under this fix.
            self.assertEqual(dependent.status, 'blocked')
        elif case == 'E':
            # Unregister engineering → no durable destination → fail closed
            # (WorkflowValidationError / IntegrityError / PLAT-4215).
            blocker, dependent = self._pair(workflow, 'E', metadata={})
            pre_status = blocker.status
            pre_group = blocker.assignment_group
            TeamRoster.objects.filter(
                organization_id=self.project.org_id,
                team_name='engineering',
            ).delete()
            WorkflowService._invalidate_team_roster_caches()
            with self.assertRaises((WorkflowValidationError, IntegrityError)):
                self._fire(blocker, request_changes)
            blocker.refresh_from_db()
            dependent.refresh_from_db()
            # Rollback: status/ownership unchanged; dependent still blocked.
            self.assertEqual(blocker.status, pre_status)
            self.assertEqual(blocker.assignment_group, pre_group)
            self.assertEqual(dependent.status, 'blocked')
            self.assertEqual(
                ItemLink.objects.filter(
                    source_item=blocker, target_item=dependent, link_type='blocks',
                ).count(),
                1,
            )
            # Restore roster for subsequent cases in same class if any.
            TeamRoster.objects.update_or_create(
                organization_id=self.project.org_id,
                team_name='engineering',
                defaults={
                    'organization_slug': 'shizuha',
                    'tiers': [[self.actor.email]],
                    'is_active': True,
                },
            )
            WorkflowService._invalidate_team_roster_caches()
        else:
            self.fail(f'unknown case {case}')

    def test_production_request_changes_chains_match_source_seeds(self):
        """rei P1: assert production pf chains; never create substitutes."""
        for spec in self.WORKFLOW_SPECS:
            with self.subTest(workflow=spec['slug']):
                self._require_production_request_changes(spec)

    def test_matrix_a_through_e_per_production_workflow(self):
        """PLS-796 §4.1: A–E via execute_transition on each locked carrier.

        Discovered test — invokes the previously-dead _run_matrix_case helper
        for autonomous-dev, autonomous-bug, and reviewed-task with forced
        deferred constraints (rei P1).

        F–H extend the matrix to review-held blockers on NON-`review` gating
        lanes (security / architecture / QA). A–E all seat their review-held
        blocker on `review` itself, so the whole matrix was blind to a lane that
        gates work under a different name — the live PLS-468 shape (@rui P1).
        """
        for spec in self.WORKFLOW_SPECS:
            for case in ('A', 'B', 'C', 'D', 'F', 'G', 'H', 'I', 'E'):
                # E unregisters the engineering roster mid-case (it restores it
                # afterwards, so this ordering is belt-and-braces, not required).
                with self.subTest(workflow=spec['slug'], case=case):
                    self._run_matrix_case(spec, case)

    def test_ladder_a_through_e_via_durable_group_resolver(self):
        """Helper-level ladder coverage (companion to full execute matrix)."""
        # A: versioned context team wins when exact-org registered.
        item = Item(
            project=self.project,
            workflow_name='autonomous-dev',
            assignment_group='review',
            metadata={
                WorkflowService._IMPLEMENTATION_RETURN_CONTEXT_KEY: {
                    'version': WorkflowService._IMPLEMENTATION_RETURN_CONTEXT_VERSION,
                    'implementation_team': 'engineering',
                    'implementer': {
                        'id': self.actor.id,
                        'email': self.actor.email,
                    },
                    'review_generation': 2,
                },
            },
        )
        ctx, err = WorkflowService._implementation_return_context(item)
        self.assertEqual(err, '')
        self.assertEqual(ctx['implementation_team'], 'engineering')

        # B: empty metadata → 4c workflow default engineering (registered).
        item_b = Item(
            project=self.project,
            workflow_name='autonomous-dev',
            assignment_group='review',
            metadata={},
        )
        self.assertEqual(
            WorkflowService._durable_implementer_return_group(item_b),
            'engineering',
        )

        # C: pre-transition non-review owner group preserved.
        item_c = Item(
            project=self.project,
            workflow_name='autonomous-bug',
            assignment_group='engineering',
            assignee_id=self.actor.id,
            assignee_email=self.actor.email,
            metadata={},
        )
        self.assertEqual(
            WorkflowService._durable_implementer_return_group(item_c),
            'engineering',
        )

        # D: still engineering group when registered (capacity is resolver-side).
        self.assertEqual(
            WorkflowService._durable_implementer_return_group(item_b),
            'engineering',
        )

        # E: unregister engineering → None (fail closed, no invent).
        TeamRoster.objects.filter(
            organization_id=self.project.org_id,
            team_name='engineering',
        ).delete()
        WorkflowService._invalidate_team_roster_caches()
        self.assertIsNone(
            WorkflowService._durable_implementer_return_group(item_b),
        )

    def test_4c_does_not_invent_engineering_when_not_workflow_default(self):
        """Engineering registered ≠ 4c default when project overrides the lane."""
        self.project.settings = {'default_implementer_group': 'architecture'}
        self.project.save(update_fields=['settings'])
        item = Item.objects.create(
            item_key='PLS796-4C-OVERRIDE',
            title='4c override',
            project=self.project,
            status='in_review',
            workflow_name='autonomous-dev',
            workflow_status='in_review',
            assignment_group='review',
            assignee_id=self.reviewer.id,
            assignee_email=self.reviewer.email,
            metadata={},
        )
        # Engineering is registered (setUp) but project default is architecture.
        self.assertEqual(
            WorkflowService._workflow_default_implementer_group(item),
            'architecture',
        )
        self.assertEqual(
            WorkflowService._durable_implementer_return_group(item),
            'architecture',
        )

    def test_4c_returns_none_when_default_lane_unregistered(self):
        """Fail closed when the workflow default lane is not exact-org registered."""
        TeamRoster.objects.filter(
            organization_id=self.project.org_id,
            team_name='engineering',
        ).delete()
        WorkflowService._invalidate_team_roster_caches()
        item = Item.objects.create(
            item_key='PLS796-4C-UNREG',
            title='4c unregistered default',
            project=self.project,
            status='in_review',
            workflow_name='autonomous-dev',
            workflow_status='in_review',
            assignment_group='review',
            assignee_id=self.reviewer.id,
            assignee_email=self.reviewer.email,
            metadata={},
        )
        self.assertEqual(
            WorkflowService._workflow_default_implementer_group(item),
            'engineering',
        )
        self.assertIsNone(WorkflowService._durable_implementer_return_group(item))
