"""PLS-634 / PLS-481 S1: accepted-capacity-held schema and PublicationBinding-gated acceptance.

Exact-head evidence matrix (rui P1 @ 1953e34 bounce):
  1. Role proofs from canonical comment authority (NOT binding-tuple verdicts /
     publication success)
  2. PublicationBinding identity/round/result mismatch variants
  3. Real execute_transition row-lock concurrent accepts
  4. Plan/HeldPlanTarget/audit/outbox materialization via seeded transition
  5. Actor / feature-gate / WIP exclusion / zero-state failure paths
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.db import connection, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from tasks.models import (
    Comment,
    HeldPlanTarget,
    Item,
    ItemActivity,
    Notification,
    Project,
    ProjectMembership,
    PublicationAttempt,
    PublicationBinding,
    Status,
    Workflow,
    WorkflowStatus,
    WorkflowTransition,
)
from tasks.services.publication_handoff import _digest
from tasks.services.workflow_service import WorkflowService, WorkflowValidationError


ARCH_EMAIL = 'aoi@shizuha.com'
SEC_EMAIL = 'akira@shizuha.com'
ENG_EMAIL = 'nagi@shizuha.com'


@override_settings(PLS482_PUBLICATION_BINDING_ENABLED=True)
class FeatureRfcPublicationPathTests(TestCase):
    """PLS-960 production path: fresh RFC -> same-carrier binding."""

    def setUp(self):
        self.architect = get_user_model().objects.create_user(
            username='pls960-architect', email=ARCH_EMAIL, password='x',
        )
        self.architect.organization_memberships = {1: 'member'}
        self.project = Project.objects.create(
            name='PLS 960 RFC', key='PBR', org_id=1, access_level='open',
        )
        ProjectMembership.objects.create(
            project=self.project, user_id=self.architect.id,
            user_email=self.architect.email, role='member', is_active=True,
            grant_source='independent',
        )
        self.transition = WorkflowTransition.objects.get(
            workflow__slug='feature-request-rfc',
            from_status='rfc_design',
            name='Prepare Publication Binding',
        )
        with patch.object(
            WorkflowService, 'assignment_org_mismatch',
            return_value=(False, 'shizuha', 'shizuha'),
        ):
            self.item = Item.objects.create(
                project=self.project, item_key='PBR-1', mode='task',
                item_type='task.rfc', title='Fresh no-PR RFC',
                status='rfc_design', workflow_status='rfc_design',
                workflow_name='feature-request-rfc',
                assignee_id=self.architect.id,
                assignee_email=self.architect.email,
                assignment_group='architecture',
                created_by_id=self.architect.id,
                created_by_email=self.architect.email,
                tags='no-pr', metadata={},
            )
        self.pages = [{
            'page_id': '55555555-5555-4555-8555-555555555555',
            'version': 3,
            'anchors': [{'anchor': 'contract', 'section_hash': '5' * 64}],
        }]
        self.screen = {'publication_binding': {
            'operation_key': 'pls960-op-1',
            'review_round_id': 'pls960-round-1',
            'publication_result': 'accepted',
            'pages': self.pages,
        }}

    def _receipt(self):
        return {
            'publication_snapshot_id': '66666666-6666-4666-8666-666666666666',
            'organization_id': 1,
            'publication_result': 'accepted',
            'pages': self.pages,
            'snapshot_digest': _digest(
                'io.shizuha.wiki-publication-snapshot/v1',
                {'organization_id': 1, 'publication_result': 'accepted',
                 'pages': self.pages},
            ),
            'replay': False,
        }

    @patch.object(WorkflowService, '_is_team_member', return_value=True)
    @patch('tasks.services.publication_handoff._wiki_snapshot')
    def test_fresh_rfc_mints_one_binding_and_round_without_leaving_design(
        self, wiki_snapshot, _team_member,
    ):
        wiki_snapshot.return_value = self._receipt()
        with self.captureOnCommitCallbacks(execute=False):
            result = WorkflowService.execute_transition(
                self.item, 'rfc_design', user=self.architect,
                screen_data=self.screen, transition_id=str(self.transition.id),
            )
        result.refresh_from_db()
        self.assertEqual(result.status, 'rfc_design')
        self.assertEqual(
            result.metadata['publication_review_round_id'], 'pls960-round-1',
        )
        binding = PublicationBinding.objects.get(item=result)
        self.assertEqual(binding.attempt.review_round_id, 'pls960-round-1')
        self.assertEqual(binding.binding_tuple['pages'], self.pages)

        replay = WorkflowService.execute_transition(
            Item.objects.get(pk=result.pk), 'rfc_design', user=self.architect,
            screen_data=self.screen, transition_id=str(self.transition.id),
        )
        self.assertEqual(replay.status, 'rfc_design')
        self.assertEqual(PublicationBinding.objects.filter(item=result).count(), 1)
        wiki_snapshot.assert_called_once()

    @patch.object(WorkflowService, '_is_team_member', return_value=True)
    @patch('tasks.services.publication_handoff._wiki_snapshot')
    def test_stale_page_receipt_holds_and_leaves_carrier_unmutated(
        self, wiki_snapshot, _team_member,
    ):
        from tasks.services.publication_handoff import PublicationWikiError

        before_metadata = dict(self.item.metadata)
        wiki_snapshot.side_effect = PublicationWikiError(
            'stale_publication_hold', 'Wiki page version changed.',
            {'current_pages': [{'page_id': self.pages[0]['page_id'], 'version': 4}]},
        )
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService.execute_transition(
                self.item, 'rfc_design', user=self.architect,
                screen_data=self.screen, transition_id=str(self.transition.id),
            )
        self.assertEqual(ctx.exception.code, 'STALE_PUBLICATION_HOLD')
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, 'rfc_design')
        self.assertEqual(self.item.metadata, before_metadata)
        self.assertFalse(PublicationBinding.objects.filter(item=self.item).exists())
        self.assertEqual(
            PublicationAttempt.objects.get(operation_key='pls960-op-1').state,
            'stale_publication_hold',
        )


def _snapshot_accept_state(item):
    """Capture Accept-related fields + side-effect counts for zero-state assertions."""
    item.refresh_from_db()
    return {
        'publication_binding_id': item.publication_binding_id,
        'publication_round': item.publication_round,
        'publication_result': item.publication_result,
        'plan_hash': item.plan_hash,
        'plan_generation': item.plan_generation or 0,
        'capacity_reason': item.capacity_reason or '',
        'status': item.status,
        'held_targets': HeldPlanTarget.objects.filter(item=item).count(),
        'accept_activities': ItemActivity.objects.filter(
            item=item, field_name='accept_capacity_held',
        ).count(),
        'accept_notifications': Notification.objects.filter(
            item=item, title='Accept — Capacity-Held',
        ).count(),
        'metadata_accept': (item.metadata or {}).get('accept_capacity_held'),
    }


class AcceptedCapacityHeldModelTests(TestCase):
    """Verify the new model fields exist and have correct defaults."""

    def setUp(self):
        self.project = Project.objects.create(
            name="Pulse", key="PLS", org_id=1, access_level="open",
        )

    def test_item_has_plan_hash_field(self):
        item = Item.objects.create(
            project=self.project, title="Test", item_type="task.generic",
        )
        self.assertIsNone(item.plan_hash)
        self.assertEqual(item.plan_generation or 0, 0)
        self.assertEqual(item.capacity_reason, "")

    def test_item_has_publication_binding_field(self):
        item = Item.objects.create(
            project=self.project, title="Test", item_type="task.generic",
        )
        self.assertIsNone(item.publication_binding)
        self.assertIsNone(item.publication_round)
        self.assertIsNone(item.publication_result)

    def test_held_plan_target_creation(self):
        item = Item.objects.create(
            project=self.project, title="Test", item_type="task.generic",
        )
        target = HeldPlanTarget.objects.create(
            item=item,
            target_team_id="engineering",
            next_eligible_slice=0,
            plan_generation=1,
        )
        self.assertEqual(target.target_team_id, "engineering")
        self.assertEqual(target.next_eligible_slice, 0)
        self.assertEqual(target.plan_generation, 1)
        self.assertIsNotNone(target.created_at)

    def test_held_plan_target_unique_constraint(self):
        item = Item.objects.create(
            project=self.project, title="Test", item_type="task.generic",
        )
        HeldPlanTarget.objects.create(
            item=item, target_team_id="engineering", plan_generation=1,
        )
        with self.assertRaises(Exception):
            HeldPlanTarget.objects.create(
                item=item, target_team_id="engineering", plan_generation=1,
            )

    def test_accepted_capacity_held_is_not_wip(self):
        """HLD §4: accepted_capacity_held must not consume a WIP seat."""
        self.assertIsNone(
            WorkflowService._slot_kind_for_status('accepted_capacity_held')
        )
        self.assertIn(
            'accepted_capacity_held',
            WorkflowService._assignment_excluded_statuses(),
        )


class _AcceptFixtureMixin:
    """Shared helpers for Accept — Capacity-Held gate tests."""

    org_id = 1

    def setUp(self):
        from tasks.models import TeamRoster
        self.project = Project.objects.create(
            name="Pulse", key=f"P{uuid4().hex[:4].upper()}", org_id=self.org_id,
            access_level="open",
        )
        # Exact-org team authority for plan slice teams (rei P1).
        for team_name, email in (
            ('engineering', ENG_EMAIL),
            ('qa', ENG_EMAIL),
            ('architecture', ARCH_EMAIL),
            ('security', SEC_EMAIL),
        ):
            TeamRoster.objects.update_or_create(
                organization_id=self.org_id,
                team_name=team_name,
                defaults={
                    'organization_slug': 'shizuha',
                    'tiers': [[email]],
                    'is_active': True,
                },
            )
        # Membership stubs: Architecture + Security emails resolve as members.
        self._member_patch = patch.object(
            WorkflowService,
            '_is_team_member',
            side_effect=self._fake_is_team_member,
        )
        self._sec_patch = patch.object(
            WorkflowService,
            '_is_security_team_email',
            side_effect=lambda email: (
                (email or '').strip().lower() == SEC_EMAIL
            ),
        )
        self._member_patch.start()
        self._sec_patch.start()
        self.addCleanup(self._member_patch.stop)
        self.addCleanup(self._sec_patch.stop)

    @staticmethod
    def _fake_is_team_member(email, team_name, item=None):
        email = (email or '').strip().lower()
        team = (team_name or '').strip().lower().replace('_', '-')
        if team == 'architecture':
            return email == ARCH_EMAIL
        if team == 'security':
            return email == SEC_EMAIL
        if team == 'engineering':
            return email == ENG_EMAIL
        return False

    def _make_item(self, **kwargs):
        defaults = dict(
            project=self.project,
            title="Design RFC",
            item_type="task.generic",
            workflow_name="feature-request-rfc",
            assignment_group="architecture",
            labels={},
            tags="",
            status="rfc_design",
            state_generation=1,
        )
        defaults.update(kwargs)
        return Item.objects.create(**defaults)

    def _make_binding(
        self,
        item,
        *,
        result="accepted",
        state="published",
        review_round_id="round-1",
        binding_tuple=None,
        expected_item_updated_at=None,
        organization_id=None,
        carrier_state_generation=None,
        include_tuple_identity=True,
        canonical_only=True,
        mint_org_cas=True,
    ):
        """Create a producer-shaped PLS-482 binding (handoff mint fields).

        By default mints organization_id + carrier_state_generation the same
        way publication_handoff finalize does (rei P1). Pass mint_org_cas=False
        to omit them and prove fail-closed.
        """
        now = timezone.now()
        attempt = PublicationAttempt.objects.create(
            operation_key=f"op-{item.pk}-{uuid4().hex[:10]}",
            request_digest=uuid4().hex[:16],
            item=item,
            transition_id=uuid4(),
            expected_item_updated_at=expected_item_updated_at or (
                now - timezone.timedelta(hours=1)
            ),
            expected_status="in_review",
            review_round_id=review_round_id,
            publication_result=result,
            artifact_policy="not_required",
            state=state,
        )
        snapshot_id = uuid4()
        # Canonical PLS-482 binding_tuple shape from publication_handoff.py —
        # deliberately NO architecture/security verdict keys.
        bt = dict(binding_tuple or {})
        if include_tuple_identity:
            bt.setdefault('publication_snapshot_id', str(snapshot_id))
            bt.setdefault('pages', [])
            bt.setdefault('review_round_id', review_round_id)
            bt.setdefault('publication_result', result)
            bt.setdefault('snapshot_digest', f'digest-{item.pk}')
            bt.setdefault('artifact_authority_ref', None)
        if canonical_only:
            # Strip any accidental verdict keys so tests cannot smuggle proofs
            # through the binding tuple.
            for k in list(bt.keys()):
                if 'verdict' in k or k in ('architecture', 'security', 'verdicts'):
                    bt.pop(k, None)
        if mint_org_cas:
            org = organization_id
            if org is None and item.project_id:
                org = item.project.org_id
            if org is not None and 'organization_id' not in bt:
                bt['organization_id'] = org
            if carrier_state_generation is not None:
                bt['carrier_state_generation'] = carrier_state_generation
            elif 'carrier_state_generation' not in bt:
                bt['carrier_state_generation'] = int(item.state_generation or 0)
        else:
            bt.pop('organization_id', None)
            bt.pop('org_id', None)
            bt.pop('carrier_state_generation', None)
            if organization_id is not None:
                bt['organization_id'] = organization_id
            if carrier_state_generation is not None:
                bt['carrier_state_generation'] = carrier_state_generation
        binding = PublicationBinding.objects.create(
            attempt=attempt,
            item=item,
            publication_snapshot_id=snapshot_id,
            binding_digest=f"digest-{item.pk}-{uuid4().hex[:8]}",
            binding_tuple=bt,
        )
        Item.objects.filter(pk=item.pk).update(
            updated_at=now - timezone.timedelta(hours=2),
        )
        item.refresh_from_db()
        binding.refresh_from_db()
        return binding

    def _post_role_proofs(self, item, review_round_id='round-1'):
        """Post Architecture + Security sign-offs bound to the review round."""
        Comment.objects.create(
            item=item,
            content=(
                f"Architecture sign-off: approved — design is sound "
                f"(review_round_id={review_round_id})."
            ),
            author_email=ARCH_EMAIL,
            author_id=1,
        )
        Comment.objects.create(
            item=item,
            content=(
                f"Security sign-off: approved — threat model reviewed "
                f"(review_round_id={review_round_id})."
            ),
            author_email=SEC_EMAIL,
            author_id=2,
        )

    def _plan_document(self, teams=None, slice_states=None):
        teams = teams if teams is not None else ['engineering']
        slice_states = slice_states or {}
        slices = []
        for idx, team in enumerate(teams, start=1):
            sid = f'slice-{idx}'
            slices.append({
                'id': sid,
                'team': team,
                'depends_on': [],
                'state': slice_states.get(sid, 'held'),
            })
        return {'slices': slices}

    def _plan_screen(self, capacity_reason='held for capacity', teams=None,
                     plan_document=None, plan_hash=None, **_ignored):
        doc = plan_document if plan_document is not None else self._plan_document(teams=teams)
        normalized = WorkflowService._normalize_accept_plan_document(
            doc, organization_id=self.org_id,
        )
        digest = (
            plan_hash
            if plan_hash is not None
            else WorkflowService._digest_accept_plan_document(normalized)
        )
        return {
            'plan_hash': digest,
            'capacity_reason': capacity_reason,
            'plan_document': normalized,
        }

    def _accept_pf(self, **screen):
        data = self._plan_screen(**screen) if screen else self._plan_screen()
        return {
            'type': 'accept_capacity_held',
            **data,
        }

    def _valid_payload(self, item=None, user_email=ARCH_EMAIL, **screen):
        payload = dict(self._accept_pf(**screen) if screen else self._accept_pf())
        payload['_user'] = SimpleNamespace(id=1, email=user_email)
        payload['_screen_data'] = self._plan_screen(**screen) if screen else self._plan_screen()
        return payload

    def _run_post_function(self, item, pf=None, user=None, screen_data=None):
        transition = SimpleNamespace()
        transition.post_functions = [pf or self._accept_pf()]
        user = user or SimpleNamespace(id=1, email=ARCH_EMAIL)
        screen_data = screen_data if screen_data is not None else self._plan_screen()
        WorkflowService.execute_post_functions(
            transition, item, user=user, screen_data=screen_data,
        )
        item.refresh_from_db()
        return item

    def _seed_accept_transition(self, workflow_slug='feature-request-rfc',
                                from_status='rfc_design'):
        """Seed a minimal Accept transition for execute_transition tests."""
        statuses = {}
        for slug, cat in (
            (from_status, 'in_progress'),
            ('accepted_capacity_held', 'backlog'),
            ('implementation_active', 'in_progress'),
            ('open', 'todo'),
        ):
            st, _ = Status.objects.get_or_create(
                slug=slug,
                defaults={'name': slug, 'category': cat, 'is_system': True},
            )
            statuses[slug] = st

        wf = Workflow.objects.filter(slug=workflow_slug).first()
        if wf is None:
            wf = Workflow.objects.create(
                slug=workflow_slug,
                name=workflow_slug,
                is_active=True,
                initial_status=statuses.get('open') or statuses[from_status],
            )
        elif not wf.initial_status_id:
            wf.initial_status = statuses.get('open') or statuses[from_status]
            wf.save(update_fields=['initial_status'])

        for pos, slug in enumerate(statuses, start=1):
            WorkflowStatus.objects.get_or_create(
                workflow=wf, status=statuses[slug], defaults={'position': pos},
            )
        tr, _ = WorkflowTransition.objects.get_or_create(
            workflow=wf,
            name='Accept — Capacity-Held',
            from_status=from_status,
            to_status='accepted_capacity_held',
            defaults={
                'allowed_roles': ['architecture'],
                'validators': [{'type': 'accept_capacity_held'}],
                'screen_fields': [
                    {'field': 'plan_hash', 'required': True},
                    {'field': 'plan_document', 'required': True},
                    {'field': 'capacity_reason', 'required': True},
                ],
                'post_functions': [{'type': 'accept_capacity_held'}],
                'position': 25,
                'button_style': 'primary',
            },
        )
        return wf, tr


class ValidateAcceptCapacityHeldTests(_AcceptFixtureMixin, TestCase):
    """P1 matrix: conjunction + role proofs + binding identity."""

    def test_canonical_binding_with_role_proofs_passes(self):
        item = self._make_item()
        self._make_binding(item)
        self._post_role_proofs(item)
        self.assertTrue(
            WorkflowService._validate_accept_capacity_held(
                item, self._valid_payload(),
            )
        )

    def test_publication_success_alone_fails_without_role_proofs(self):
        """rui P1: accepted binding is NOT dual-role proof."""
        item = self._make_item()
        self._make_binding(item, result='accepted', state='published')
        # No Architecture/Security comments.
        before = _snapshot_accept_state(item)
        self.assertFalse(
            WorkflowService._validate_accept_capacity_held(
                item, self._valid_payload(),
            )
        )
        self.assertEqual(_snapshot_accept_state(item), before)

    def test_binding_tuple_verdict_keys_are_ignored(self):
        """Non-canonical verdict keys on the tuple must not authorize Accept."""
        item = self._make_item()
        self._make_binding(
            item,
            binding_tuple={
                'architecture_verdict': 'approved',
                'security_verdict': 'approved',
                'verdicts': {'architecture': 'approved', 'security': 'approved'},
            },
            canonical_only=False,
        )
        # Still no authenticated role comments → fail.
        self.assertFalse(
            WorkflowService._validate_accept_capacity_held(
                item, self._valid_payload(),
            )
        )

    def test_role_proof_missing_round_token_fails(self):
        item = self._make_item()
        self._make_binding(item, review_round_id='round-1')
        Comment.objects.create(
            item=item,
            content='Architecture sign-off: approved',  # no round token
            author_email=ARCH_EMAIL, author_id=1,
        )
        Comment.objects.create(
            item=item,
            content='Security sign-off: approved',
            author_email=SEC_EMAIL, author_id=2,
        )
        self.assertFalse(
            WorkflowService._validate_accept_capacity_held(
                item, self._valid_payload(),
            )
        )

    def test_role_proof_wrong_author_fails(self):
        item = self._make_item()
        self._make_binding(item)
        Comment.objects.create(
            item=item,
            content='Architecture sign-off: approved (review_round_id=round-1)',
            author_email=ENG_EMAIL, author_id=9,  # not Architecture
        )
        Comment.objects.create(
            item=item,
            content='Security sign-off: approved (review_round_id=round-1)',
            author_email=SEC_EMAIL, author_id=2,
        )
        self.assertFalse(
            WorkflowService._validate_accept_capacity_held(
                item, self._valid_payload(),
            )
        )

    def test_reviewed_task_carrier_passes(self):
        item = self._make_item(workflow_name="reviewed-task", status="in_review")
        self._make_binding(item)
        self._post_role_proofs(item)
        self.assertTrue(
            WorkflowService._validate_accept_capacity_held(
                item, self._valid_payload(),
            )
        )

    def test_no_pr_autonomous_dev_architecture_passes(self):
        item = self._make_item(
            workflow_name="autonomous-dev",
            labels={"no-pr": "true"},
            assignment_group="architecture",
        )
        self._make_binding(item)
        self._post_role_proofs(item)
        self.assertTrue(
            WorkflowService._validate_accept_capacity_held(
                item, self._valid_payload(),
            )
        )

    def test_no_pr_autonomous_bug_architecture_marker_passes(self):
        item = self._make_item(
            workflow_name="autonomous-bug",
            labels={"no-pr": "true", "architecture": "true"},
            assignment_group="engineering",
        )
        self._make_binding(item)
        self._post_role_proofs(item)
        self.assertTrue(
            WorkflowService._validate_accept_capacity_held(
                item, self._valid_payload(),
            )
        )

    def test_non_no_pr_carrier_fails(self):
        item = self._make_item(
            workflow_name="autonomous-dev",
            labels={},
            tags="",
            assignment_group="architecture",
        )
        self._make_binding(item)
        self._post_role_proofs(item)
        before = _snapshot_accept_state(item)
        self.assertFalse(
            WorkflowService._validate_accept_capacity_held(
                item, self._valid_payload(),
            )
        )
        self.assertEqual(_snapshot_accept_state(item), before)

    def test_no_pr_without_architecture_fails(self):
        item = self._make_item(
            workflow_name="autonomous-dev",
            labels={"no-pr": "true"},
            assignment_group="engineering",
            tags="",
        )
        self._make_binding(item)
        self._post_role_proofs(item)
        self.assertFalse(
            WorkflowService._validate_accept_capacity_held(
                item, self._valid_payload(),
            )
        )

    def test_code_pr_workflow_fails(self):
        item = self._make_item(
            workflow_name="autonomous-bug",
            labels={},
            assignment_group="engineering",
        )
        self._make_binding(item)
        self._post_role_proofs(item)
        self.assertFalse(
            WorkflowService._validate_accept_capacity_held(
                item, self._valid_payload(),
            )
        )

    def test_no_binding_fails(self):
        item = self._make_item()
        self._post_role_proofs(item)
        before = _snapshot_accept_state(item)
        self.assertFalse(
            WorkflowService._validate_accept_capacity_held(
                item, self._valid_payload(),
            )
        )
        self.assertEqual(_snapshot_accept_state(item), before)

    def test_wrong_round_in_binding_tuple_fails(self):
        item = self._make_item()
        self._make_binding(
            item,
            review_round_id="round-live",
            binding_tuple={'review_round_id': 'round-STALE'},
        )
        self._post_role_proofs(item, review_round_id='round-live')
        self.assertFalse(
            WorkflowService._validate_accept_capacity_held(
                item, self._valid_payload(),
            )
        )

    def test_wrong_result_in_binding_tuple_fails(self):
        item = self._make_item()
        self._make_binding(
            item,
            result="accepted",
            binding_tuple={'publication_result': 'rejected'},
        )
        self._post_role_proofs(item)
        self.assertFalse(
            WorkflowService._validate_accept_capacity_held(
                item, self._valid_payload(),
            )
        )

    def test_matched_rejected_publication_result_fails(self):
        """rei P1: matched rejected/negative result is not an accepted enum."""
        item = self._make_item()
        self._make_binding(item, result='rejected', state='failed')
        self._post_role_proofs(item)
        before = _snapshot_accept_state(item)
        self.assertFalse(
            WorkflowService._validate_accept_capacity_held(
                item, self._valid_payload(),
            )
        )
        self.assertEqual(_snapshot_accept_state(item), before)

    def test_missing_producer_org_cas_fields_fail_closed(self):
        """rei P1: real PLS-482 shape without org/CAS must not pass via fallback."""
        item = self._make_item()
        self._make_binding(item, mint_org_cas=False)
        self._post_role_proofs(item)
        before = _snapshot_accept_state(item)
        self.assertFalse(
            WorkflowService._validate_accept_capacity_held(
                item, self._valid_payload(),
            )
        )
        self.assertEqual(_snapshot_accept_state(item), before)


    def test_wrong_snapshot_in_binding_tuple_fails(self):
        item = self._make_item()
        self._make_binding(
            item,
            binding_tuple={
                'publication_snapshot_id': '00000000-0000-0000-0000-00000000dead',
            },
        )
        self._post_role_proofs(item)
        self.assertFalse(
            WorkflowService._validate_accept_capacity_held(
                item, self._valid_payload(),
            )
        )

    def test_cross_org_binding_tuple_fails(self):
        item = self._make_item()
        self._make_binding(item, organization_id=999)
        self._post_role_proofs(item)
        before = _snapshot_accept_state(item)
        self.assertFalse(
            WorkflowService._validate_accept_capacity_held(
                item, self._valid_payload(),
            )
        )
        self.assertEqual(_snapshot_accept_state(item), before)

    def test_missing_plan_hash_fails(self):
        item = self._make_item()
        self._make_binding(item)
        self._post_role_proofs(item)
        payload = self._valid_payload()
        screen = self._plan_screen()
        screen['plan_hash'] = ''
        payload['plan_hash'] = ''
        payload['_screen_data'] = screen
        self.assertFalse(
            WorkflowService._validate_accept_capacity_held(item, payload)
        )

    def test_plan_hash_digest_mismatch_fails(self):
        item = self._make_item()
        self._make_binding(item)
        self._post_role_proofs(item)
        payload = self._valid_payload()
        screen = self._plan_screen()
        screen['plan_hash'] = '0' * 64
        payload['_screen_data'] = screen
        self.assertFalse(
            WorkflowService._validate_accept_capacity_held(item, payload)
        )

    def test_missing_plan_document_fails(self):
        item = self._make_item()
        self._make_binding(item)
        self._post_role_proofs(item)
        payload = self._valid_payload()
        payload['_screen_data'] = {
            'plan_hash': '0' * 64,
            'capacity_reason': 'reason',
            'plan_document': {'slices': []},
        }
        self.assertFalse(
            WorkflowService._validate_accept_capacity_held(item, payload)
        )

    def test_non_architecture_actor_fails(self):
        item = self._make_item()
        self._make_binding(item)
        self._post_role_proofs(item)
        payload = self._valid_payload(user_email=ENG_EMAIL)
        self.assertFalse(
            WorkflowService._validate_accept_capacity_held(item, payload)
        )

    def test_canonical_accepted_publication_result_passes(self):
        """rei P1: PLS-482 publication_result='accepted' is the success enum."""
        item = self._make_item()
        self._make_binding(item, result='accepted', state='published')
        self._post_role_proofs(item)
        self.assertTrue(
            WorkflowService._validate_accept_capacity_held(
                item, self._valid_payload(),
            )
        )

    def test_lifecycle_published_result_is_not_accepted_enum(self):
        """rei P1: attempt.state published/delivered is not publication_result."""
        item = self._make_item()
        self._make_binding(item, result='published', state='published')
        self._post_role_proofs(item)
        self.assertFalse(
            WorkflowService._validate_accept_capacity_held(
                item, self._valid_payload(),
            )
        )

    def test_caller_terminal_slice_state_rejected_at_accept(self):
        """rei P1: cannot submit every slice as done at acceptance."""
        item = self._make_item()
        self._make_binding(item)
        self._post_role_proofs(item)
        doc = self._plan_document(
            teams=['engineering'],
            slice_states={'slice-1': 'done'},
        )
        # Normalize without org so we can craft a digest, then force terminal state
        # into the screen payload the gate sees.
        payload = self._valid_payload()
        payload['_screen_data'] = {
            'plan_hash': 'x' * 64,
            'capacity_reason': 'reason',
            'plan_document': doc,
        }
        self.assertFalse(
            WorkflowService._validate_accept_capacity_held(item, payload)
        )

    def test_cyclic_plan_dependencies_rejected(self):
        item = self._make_item()
        self._make_binding(item)
        self._post_role_proofs(item)
        doc = {
            'slices': [
                {'id': 'a', 'team': 'engineering', 'depends_on': ['b']},
                {'id': 'b', 'team': 'engineering', 'depends_on': ['a']},
            ],
        }
        payload = self._valid_payload()
        payload['_screen_data'] = {
            'plan_hash': 'y' * 64,
            'capacity_reason': 'reason',
            'plan_document': doc,
        }
        self.assertFalse(
            WorkflowService._validate_accept_capacity_held(item, payload)
        )

    def test_unregistered_plan_team_rejected(self):
        item = self._make_item()
        self._make_binding(item)
        self._post_role_proofs(item)
        doc = self._plan_document(teams=['nonexistent-team'])
        payload = self._valid_payload()
        payload['_screen_data'] = {
            'plan_hash': 'z' * 64,
            'capacity_reason': 'reason',
            'plan_document': doc,
        }
        self.assertFalse(
            WorkflowService._validate_accept_capacity_held(item, payload)
        )

    @override_settings(PULSE_ACCEPT_CAPACITY_HELD_ENABLED=False)
    def test_feature_gate_off_fails_closed(self):
        item = self._make_item()
        self._make_binding(item)
        self._post_role_proofs(item)
        before = _snapshot_accept_state(item)
        self.assertFalse(
            WorkflowService._validate_accept_capacity_held(
                item, self._valid_payload(),
            )
        )
        self.assertEqual(_snapshot_accept_state(item), before)


class AcceptCapacityHeldCasTests(_AcceptFixtureMixin, TestCase):
    """CAS drift variants."""

    def test_cas_missing_state_generation_fails_closed(self):
        """rei P1: no timestamp fallback when producer CAS field is absent."""
        item = self._make_item()
        binding = self._make_binding(item)
        self._post_role_proofs(item)
        bt = dict(binding.binding_tuple)
        bt.pop('carrier_state_generation', None)
        PublicationBinding.objects.filter(pk=binding.pk).update(binding_tuple=bt)
        item.refresh_from_db()
        before = _snapshot_accept_state(item)
        self.assertFalse(
            WorkflowService._validate_accept_capacity_held(
                item, self._valid_payload(),
            )
        )
        self.assertEqual(_snapshot_accept_state(item), before)

    def test_cas_drift_state_generation_fails(self):
        item = self._make_item(state_generation=3)
        self._make_binding(item, carrier_state_generation=3)
        self._post_role_proofs(item)
        Item.objects.filter(pk=item.pk).update(priority='high')
        item.refresh_from_db()
        self.assertEqual(item.state_generation, 4)
        before = _snapshot_accept_state(item)
        self.assertFalse(
            WorkflowService._validate_accept_capacity_held(
                item, self._valid_payload(),
            )
        )
        self.assertEqual(_snapshot_accept_state(item), before)

    def test_cas_state_generation_match_passes_even_if_touched(self):
        item = self._make_item(state_generation=5)
        self._make_binding(item, carrier_state_generation=5)
        self._post_role_proofs(item)
        Item.objects.filter(pk=item.pk).update(
            updated_at=timezone.now() + timezone.timedelta(hours=2),
        )
        item.refresh_from_db()
        self.assertTrue(
            WorkflowService._validate_accept_capacity_held(
                item, self._valid_payload(),
            )
        )


class AcceptCapacityHeldPostFunctionTests(_AcceptFixtureMixin, TestCase):
    """Post-function persistence + HeldPlanTarget + audit + outbox."""

    def test_post_function_materializes_plan_targets_audit_outbox(self):
        item = self._make_item()
        binding = self._make_binding(item)
        self._post_role_proofs(item)
        screen = self._plan_screen(
            capacity_reason='Team at WIP cap',
            teams=['engineering', 'qa'],
        )
        self._run_post_function(item, screen_data=screen)
        self.assertEqual(item.publication_binding_id, binding.pk)
        self.assertEqual(item.publication_round, "round-1")
        self.assertEqual(item.publication_result, "accepted")
        self.assertEqual(item.plan_hash, screen['plan_hash'])
        self.assertEqual(item.plan_generation, 1)
        self.assertEqual(item.capacity_reason, "Team at WIP cap")
        targets = set(
            HeldPlanTarget.objects.filter(item=item).values_list(
                'target_team_id', flat=True,
            )
        )
        self.assertEqual(targets, {'engineering', 'qa'})
        self.assertEqual(
            HeldPlanTarget.objects.filter(item=item, plan_generation=1).count(), 2,
        )
        accepted = (item.metadata or {}).get('accepted_plan') or {}
        self.assertEqual(accepted.get('plan_hash'), screen['plan_hash'])
        self.assertEqual(len(accepted.get('slices') or []), 2)
        self.assertTrue(all(
            s.get('state') == 'held' for s in accepted.get('slices') or []
        ))
        audit = (item.metadata or {}).get('accept_capacity_held') or {}
        self.assertEqual(audit.get('plan_hash'), screen['plan_hash'])
        self.assertEqual(audit.get('publication_binding_id'), str(binding.pk))
        self.assertIn('architecture', audit.get('role_proofs') or {})
        self.assertIn('security', audit.get('role_proofs') or {})
        self.assertEqual(
            ItemActivity.objects.filter(
                item=item, field_name='accept_capacity_held',
            ).count(),
            1,
        )
        self.assertEqual(
            Notification.objects.filter(
                item=item, title='Accept — Capacity-Held',
            ).count(),
            1,
        )

    def test_post_function_idempotent_same_binding_replay(self):
        item = self._make_item()
        binding = self._make_binding(item)
        self._post_role_proofs(item)
        self._run_post_function(item)
        after_first = _snapshot_accept_state(item)
        self.assertEqual(after_first['publication_binding_id'], binding.pk)
        self.assertEqual(after_first['plan_generation'], 1)
        self._run_post_function(item)
        self.assertEqual(_snapshot_accept_state(item), after_first)

    def test_post_function_rejects_when_gate_fails(self):
        item = self._make_item(workflow_name="autonomous-dev", labels={}, tags="")
        self._make_binding(item)
        self._post_role_proofs(item)
        before = _snapshot_accept_state(item)
        with self.assertRaises(WorkflowValidationError):
            self._run_post_function(item)
        self.assertEqual(_snapshot_accept_state(item), before)


class AcceptCapacityHeldZeroStateTests(_AcceptFixtureMixin, TestCase):
    """Every failure path leaves zero Accept side effects."""

    def test_validator_failures_never_mutate(self):
        cases = [
            ("no_binding", {"workflow_name": "feature-request-rfc"}, None, True),
            (
                "code_pr",
                {
                    "workflow_name": "autonomous-dev",
                    "labels": {},
                    "assignment_group": "architecture",
                },
                {"result": "approved"},
                True,
            ),
            (
                "cross_org",
                {},
                {"organization_id": 4242},
                True,
            ),
            (
                "no_proofs",
                {},
                {"result": "accepted"},
                False,  # skip role proofs
            ),
        ]
        for label, item_kwargs, binding_kwargs, with_proofs in cases:
            with self.subTest(case=label):
                item = self._make_item(**item_kwargs)
                if binding_kwargs is not None:
                    self._make_binding(item, **binding_kwargs)
                if with_proofs:
                    self._post_role_proofs(item)
                before = _snapshot_accept_state(item)
                self.assertFalse(
                    WorkflowService._validate_accept_capacity_held(
                        item, self._valid_payload(),
                    ),
                    msg=f'{label} should fail gate',
                )
                after = _snapshot_accept_state(item)
                self.assertEqual(after, before, msg=f'{label} mutated state')
                self.assertIsNone(after['publication_binding_id'])
                self.assertEqual(after['plan_generation'], 0)
                self.assertEqual(after['held_targets'], 0)
                self.assertEqual(after['accept_notifications'], 0)


class AcceptCapacityHeldConcurrentTests(_AcceptFixtureMixin, TransactionTestCase):
    """Concurrent accepts through the real execute_transition lock path."""

    def _execute_accept(self, item_id, plan_label, transition_id):
        try:
            item = Item.objects.get(pk=item_id)
            user = SimpleNamespace(id=1, email=ARCH_EMAIL)
            # Distinct digest-verified plans so CAS race is on plan_hash identity.
            screen = self._plan_screen(
                capacity_reason=f'concurrent-{plan_label}',
                plan_document={
                    'slices': [
                        {
                            'id': f'slice-{plan_label}',
                            'team': 'engineering',
                            'depends_on': [],
                        },
                    ],
                },
            )
            WorkflowService.execute_transition(
                item,
                'accepted_capacity_held',
                user=user,
                screen_data=screen,
                transition_id=str(transition_id),
            )
            return ('ok', screen['plan_hash'])
        except WorkflowValidationError as exc:
            return ('reject', f'{plan_label}:{exc}')
        except Exception as exc:  # pragma: no cover
            return ('error', f'{plan_label}:{type(exc).__name__}:{exc}')
        finally:
            connection.close()

    def test_concurrent_execute_transition_one_wins(self):
        _wf, tr = self._seed_accept_transition()
        item = self._make_item(status='rfc_design', workflow_name='feature-request-rfc')
        binding = self._make_binding(item)
        self._post_role_proofs(item)
        before = _snapshot_accept_state(item)
        self.assertIsNone(before['publication_binding_id'])

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(self._execute_accept, item.pk, 'A', tr.id),
                pool.submit(self._execute_accept, item.pk, 'B', tr.id),
            ]
            results = [f.result(timeout=45) for f in as_completed(futures)]

        statuses = sorted(r[0] for r in results)
        self.assertEqual(statuses.count('ok'), 1, msg=results)
        self.assertEqual(statuses.count('reject'), 1, msg=results)
        self.assertNotIn('error', statuses, msg=results)

        item.refresh_from_db()
        self.assertEqual(item.status, 'accepted_capacity_held')
        self.assertEqual(item.publication_binding_id, binding.pk)
        self.assertEqual(item.plan_generation, 1)
        self.assertTrue(item.plan_hash)
        self.assertEqual(
            HeldPlanTarget.objects.filter(item=item, plan_generation=1).count(), 1,
        )
        self.assertEqual(
            Notification.objects.filter(
                item=item, title='Accept — Capacity-Held',
            ).count(),
            1,
        )


class AcceptCapacityHeldRunValidatorsTests(_AcceptFixtureMixin, TestCase):
    """Validator wiring path honors accept_capacity_held + screen_data."""

    def test_run_validators_raises_on_gate_failure(self):
        item = self._make_item(workflow_name="autonomous-dev", labels={})
        self._make_binding(item)
        self._post_role_proofs(item)
        transition = SimpleNamespace()
        transition.validators = [{'type': 'accept_capacity_held'}]
        transition.id = uuid4()
        before = _snapshot_accept_state(item)
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._run_validators(
                transition, item,
                user=SimpleNamespace(id=1, email=ARCH_EMAIL),
                screen_data=self._plan_screen(),
            )
        self.assertIn('Capacity-Held', str(ctx.exception))
        self.assertEqual(_snapshot_accept_state(item), before)

    def test_run_validators_passes_on_exact_conjunction(self):
        item = self._make_item()
        self._make_binding(item)
        self._post_role_proofs(item)
        transition = SimpleNamespace()
        transition.validators = [{'type': 'accept_capacity_held'}]
        transition.id = uuid4()
        WorkflowService._run_validators(
            transition, item,
            user=SimpleNamespace(id=1, email=ARCH_EMAIL),
            screen_data=self._plan_screen(),
        )


class AcceptCapacityHeldExecuteTransitionTests(_AcceptFixtureMixin, TestCase):
    """Seeded transition end-to-end via execute_transition."""

    def test_seeded_transition_accepts_and_holds(self):
        _wf, tr = self._seed_accept_transition()
        item = self._make_item(status='rfc_design')
        binding = self._make_binding(item)
        self._post_role_proofs(item)
        user = SimpleNamespace(id=1, email=ARCH_EMAIL)
        screen = self._plan_screen(
            capacity_reason='capacity held for e2e',
            teams=['engineering'],
        )
        WorkflowService.execute_transition(
            item,
            'accepted_capacity_held',
            user=user,
            screen_data=screen,
            transition_id=str(tr.id),
        )
        item.refresh_from_db()
        self.assertEqual(item.status, 'accepted_capacity_held')
        self.assertEqual(item.publication_binding_id, binding.pk)
        self.assertEqual(item.plan_hash, screen['plan_hash'])
        self.assertEqual(
            HeldPlanTarget.objects.filter(item=item).count(), 1,
        )
        self.assertTrue(
            ItemActivity.objects.filter(
                item=item, field_name='accept_capacity_held',
            ).exists()
        )
        self.assertTrue(
            Notification.objects.filter(
                item=item, title='Accept — Capacity-Held',
            ).exists()
        )

    def test_seeded_transition_rejects_without_proofs_zero_state(self):
        _wf, tr = self._seed_accept_transition()
        item = self._make_item(status='rfc_design')
        self._make_binding(item)
        # No role proofs.
        before = _snapshot_accept_state(item)
        user = SimpleNamespace(id=1, email=ARCH_EMAIL)
        with self.assertRaises(WorkflowValidationError):
            WorkflowService.execute_transition(
                item,
                'accepted_capacity_held',
                user=user,
                screen_data=self._plan_screen(),
                transition_id=str(tr.id),
            )
        after = _snapshot_accept_state(item)
        self.assertEqual(after['status'], before['status'])
        self.assertIsNone(after['publication_binding_id'])
        self.assertEqual(after['held_targets'], 0)
        self.assertEqual(after['accept_notifications'], 0)


class ParentDebtRegressionTests(TestCase):
    """Parent-debt invariant: terminal statuses blocked while slices open."""

    def setUp(self):
        self.project = Project.objects.create(
            name="Pulse", key="PLS", org_id=1, access_level="open",
        )

    def _item_with_open_plan(self, **kwargs):
        defaults = dict(
            project=self.project,
            title="Held",
            item_type="task.generic",
            plan_hash="plan-existing",
            plan_generation=1,
            status="accepted_capacity_held",
            metadata={
                'accepted_plan': {
                    'schema': 'shizuha.pulse.held-plan.v1',
                    'plan_hash': 'plan-existing',
                    'slices': [
                        {
                            'id': 'slice-1',
                            'team': 'engineering',
                            'depends_on': [],
                            'state': 'held',
                        },
                    ],
                },
            },
        )
        defaults.update(kwargs)
        return Item.objects.create(**defaults)

    def test_terminal_debt_gate_blocks_done_with_open_slices(self):
        item = self._item_with_open_plan()
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._validate_terminal_debt_gate(item, 'done')
        self.assertIn('plan_hash', str(ctx.exception))

    def test_terminal_debt_gate_blocks_deferred_and_cancelled(self):
        """rei P1: debt applies to every terminal status, not only done."""
        item = self._item_with_open_plan()
        for terminal in ('deferred', 'cancelled', 'closed', 'resolved'):
            with self.assertRaises(WorkflowValidationError):
                WorkflowService._validate_terminal_debt_gate(item, terminal)

    def test_terminal_debt_gate_allows_done_when_slices_closed(self):
        item = self._item_with_open_plan(
            plan_hash="plan-completed",
            status="implementation_active",
            metadata={
                'accepted_plan': {
                    'schema': 'shizuha.pulse.held-plan.v1',
                    'plan_hash': 'plan-completed',
                    'slices': [
                        {
                            'id': 'slice-1',
                            'team': 'engineering',
                            'depends_on': [],
                            'state': 'done',
                        },
                    ],
                },
            },
        )
        WorkflowService._validate_terminal_debt_gate(item, 'done')

    def test_terminal_debt_gate_blocks_when_plan_hash_without_slices(self):
        """Deleting HeldPlanTarget rows must not clear debt (rei P1)."""
        item = Item.objects.create(
            project=self.project, title="Orphan hash", item_type="task.generic",
            plan_hash="plan-orphan",
            plan_generation=1,
            metadata={},
        )
        with self.assertRaises(WorkflowValidationError):
            WorkflowService._validate_terminal_debt_gate(item, 'done')

    def test_terminal_debt_gate_allows_done_without_plan(self):
        item = Item.objects.create(
            project=self.project, title="No plan", item_type="task.generic",
            status="in_progress",
        )
        WorkflowService._validate_terminal_debt_gate(item, 'done')

    def test_terminal_debt_gate_skips_non_terminal(self):
        item = self._item_with_open_plan()
        WorkflowService._validate_terminal_debt_gate(item, 'in_progress')

    def test_done_blocked_with_open_plan_slices(self):
        item = self._item_with_open_plan()
        self.assertIsNotNone(item.plan_hash)
        with self.assertRaises(WorkflowValidationError):
            WorkflowService._validate_terminal_debt_gate(item, 'done')

    def test_done_allowed_when_plan_closed(self):
        item = self._item_with_open_plan(
            plan_hash="plan-completed",
            metadata={
                'accepted_plan': {
                    'schema': 'shizuha.pulse.held-plan.v1',
                    'plan_hash': 'plan-completed',
                    'slices': [
                        {
                            'id': 'slice-1',
                            'team': 'engineering',
                            'depends_on': [],
                            'state': 'done',
                        },
                    ],
                },
            },
        )
        WorkflowService._validate_terminal_debt_gate(item, 'done')

    def test_no_plan_allows_done(self):
        item = Item.objects.create(
            project=self.project, title="Plain", item_type="task.generic",
        )
        self.assertIsNone(item.plan_hash)
        WorkflowService._validate_terminal_debt_gate(item, 'done')


class Plat7783RefenceOnResumeTests(_AcceptFixtureMixin, TestCase):
    """mint → generation drift → re-fence → Accept CAS matches."""

    def test_refence_moves_fence_not_plan_bytes(self):
        from tasks.services.publication_handoff import refence_publication_binding_cas

        item = self._make_item(state_generation=3)
        binding = self._make_binding(item, carrier_state_generation=3)
        frozen_pages = list((binding.binding_tuple or {}).get('pages') or [])
        frozen_round = (binding.binding_tuple or {}).get('review_round_id')
        Item.objects.filter(pk=item.pk).update(priority='high')
        item.refresh_from_db()
        self.assertEqual(item.state_generation, 4)
        self.assertTrue(refence_publication_binding_cas(item))
        binding.refresh_from_db()
        bt = binding.binding_tuple
        self.assertEqual(int(bt['carrier_state_generation']), 4)
        self.assertEqual(bt.get('pages'), frozen_pages)
        self.assertEqual(bt.get('review_round_id'), frozen_round)

    def test_accept_passes_after_refence_following_generation_drift(self):
        from tasks.services.publication_handoff import refence_publication_binding_cas

        item = self._make_item(state_generation=3)
        self._make_binding(item, carrier_state_generation=3)
        self._post_role_proofs(item)
        Item.objects.filter(pk=item.pk).update(priority='high')
        item.refresh_from_db()
        self.assertFalse(
            WorkflowService._validate_accept_capacity_held(
                item, self._valid_payload(),
            )
        )
        refence_publication_binding_cas(item)
        item.refresh_from_db()
        self.assertTrue(
            WorkflowService._validate_accept_capacity_held(
                item, self._valid_payload(),
            )
        )


class Plat7783LazyRefenceTests(_AcceptFixtureMixin, TestCase):
    """PLAT-7783 residual matrix: fence-only drift after the last trigger-time
    re-fence must not re-deadlock Accept — Capacity-Held (live-verified
    negative on CTX-625, 09-08: auto-unblock resume + later auto-failover
    assignment left the fence stale and the accept 400'd)."""

    def _mirror_binding(self, item, binding):
        """Write the item-level mirror exactly as the real finalize/refence does."""
        meta = dict(item.metadata or {})
        meta['publication_binding'] = dict(binding.binding_tuple or {})
        meta['publication_binding_digest'] = binding.binding_digest
        item.metadata = meta
        item.save(update_fields=['metadata', 'updated_at'])

    def test_refence_if_fence_only_drift_refences_intact_content(self):
        from tasks.services.publication_handoff import refence_if_fence_only_drift

        item = self._make_item(state_generation=3)
        binding = self._make_binding(item, carrier_state_generation=3)
        self._mirror_binding(item, binding)
        Item.objects.filter(pk=item.pk).update(priority='high')
        item.refresh_from_db()
        self.assertEqual(item.state_generation, 4)
        self.assertTrue(refence_if_fence_only_drift(item))
        binding.refresh_from_db()
        self.assertEqual(int(binding.binding_tuple['carrier_state_generation']), 4)

    def test_refence_if_fence_only_drift_refuses_without_mirror(self):
        from tasks.services.publication_handoff import refence_if_fence_only_drift

        item = self._make_item(state_generation=3)
        self._make_binding(item, carrier_state_generation=3)
        Item.objects.filter(pk=item.pk).update(priority='high')
        item.refresh_from_db()
        # No item-level mirror: fail closed (cannot prove row integrity).
        self.assertFalse(refence_if_fence_only_drift(item))

    def test_refence_if_fence_only_drift_refuses_on_content_tamper(self):
        from tasks.services.publication_handoff import refence_if_fence_only_drift

        item = self._make_item(state_generation=3)
        binding = self._make_binding(item, carrier_state_generation=3)
        self._mirror_binding(item, binding)
        # Direct row edit bypassing the refence path: content drift.
        bt = dict(binding.binding_tuple or {})
        bt['snapshot_digest'] = 'tampered-digest'
        PublicationBinding.objects.filter(pk=binding.pk).update(binding_tuple=bt)
        Item.objects.filter(pk=item.pk).update(priority='high')
        item.refresh_from_db()
        self.assertFalse(refence_if_fence_only_drift(item))

    def test_accept_succeeds_after_post_refence_state_mutation(self):
        """The CTX-625 round-trip: mint → re-fence → LATER state-kind mutation
        → Accept reaches accepted_capacity_held via the lazy re-fence."""
        _wf, tr = self._seed_accept_transition()
        item = self._make_item(status='rfc_design')
        binding = self._make_binding(item)
        self._mirror_binding(item, binding)
        self._post_role_proofs(item)
        from tasks.services.publication_handoff import refence_publication_binding_cas
        # First drift + trigger-time re-fence (e.g. the auto-unblock restore).
        Item.objects.filter(pk=item.pk).update(priority='high')
        item.refresh_from_db()
        self.assertTrue(refence_publication_binding_cas(item))
        # The residual: a legitimate state-kind mutation AFTER the re-fence
        # (auto-failover assignment) re-stales the fence.
        Item.objects.filter(pk=item.pk).update(priority='urgent')
        item.refresh_from_db()
        self.assertNotEqual(
            int(binding.binding_tuple['carrier_state_generation']),
            int(item.state_generation),
        )
        user = SimpleNamespace(id=1, email=ARCH_EMAIL)
        screen = self._plan_screen(
            capacity_reason='capacity held for e2e',
            teams=['engineering'],
        )
        WorkflowService.execute_transition(
            item,
            'accepted_capacity_held',
            user=user,
            screen_data=screen,
            transition_id=str(tr.id),
        )
        item.refresh_from_db()
        self.assertEqual(item.status, 'accepted_capacity_held')

    def test_accept_fails_closed_when_binding_content_tampered(self):
        """A directly-edited binding row must not be rescued by the lazy
        re-fence: the gate still refuses and the carrier stays put."""
        _wf, tr = self._seed_accept_transition()
        item = self._make_item(status='rfc_design')
        binding = self._make_binding(item)
        self._mirror_binding(item, binding)
        self._post_role_proofs(item)
        bt = dict(binding.binding_tuple or {})
        bt['snapshot_digest'] = 'tampered-digest'
        PublicationBinding.objects.filter(pk=binding.pk).update(binding_tuple=bt)
        Item.objects.filter(pk=item.pk).update(priority='high')
        item.refresh_from_db()
        before = item.status
        user = SimpleNamespace(id=1, email=ARCH_EMAIL)
        with self.assertRaises(WorkflowValidationError):
            WorkflowService.execute_transition(
                item,
                'accepted_capacity_held',
                user=user,
                screen_data=self._plan_screen(),
                transition_id=str(tr.id),
            )
        self.assertEqual(Item.objects.get(pk=item.pk).status, before)
