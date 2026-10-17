"""PLAT-1253 (PLAT-1247 Security Program 1): STRIDE-lite threat-model gate.

Trust-boundary HLD/design tasks must not reach approval/close/implementation-
ready without (1) a STRIDE-lite Threat Model section and (2) Security sign-off.
Enforcement is transition-time in WorkflowService._validate_stride_gate —
event-driven, no sweep. Kill-switch: PULSE_STRIDE_HLD_GATE_ENABLED.
"""
from django.test import TestCase, TransactionTestCase, override_settings

from tasks.models import Comment, Item, Project
from tasks.services.workflow_service import (
    WorkflowService, WorkflowValidationError, SecurityGateLinkProtectedError,
)


THREAT_MODEL = """
## Threat Model (STRIDE-lite)
Assets: agent identities, org contact graph. Trust boundaries: Connect DM edge.
Data flows: hive -> connect -> runtime bridge.
Spoofing: agent identity tokens verified. Tampering: payloads signed.
Repudiation: delivery audit log. Information Disclosure: owner-scoped listing.
Denial of Service: per-owner rate limits. Elevation of Privilege: no admin paths.
Mitigation: shared-org authorization; residual risk accepted for test orgs.
"""


class StrideGateTests(TestCase):
    SECURITY_REVIEWER = 'akira@shizuha.com'

    def setUp(self):
        # The free-text 'Security sign-off' path is author-gated (PLAT-1253 P1):
        # only a Security-team member can clear it. Seed the security roster and
        # reset the 30s process-level roster cache so the lookup sees it.
        WorkflowService._roster_cache = None
        from tasks.models import TeamRoster
        # Migrations seed a canonical `security` roster, so a bare create()
        # collides on tasks_team_roster_team_name_key. update_or_create is
        # idempotent and pins the roster to exactly this reviewer for the gate.
        TeamRoster.objects.update_or_create(
            team_name='security',
            defaults=dict(tiers=[[self.SECURITY_REVIEWER]], is_active=True))
        # PLS-458: a non-security team so "reassigned away from Security"
        # fixtures can move the successor to a valid org member (rin@) that
        # satisfies main's DB org-authz trigger while NOT being on security.
        TeamRoster.objects.update_or_create(
            team_name='engineering',
            defaults=dict(tiers=[['rin@shizuha.com']], is_active=True))
        self.project = Project.objects.create(
            name='Hive', key='HIVE', org_id=1,
            access_level='private', is_active=True,
        )

    def _item(self, title='HLD: Hive external onboarding via Connect DM boundary',
              description='External onboarding lets external users receive outbound DM '
                          'messages across a cross-org contact path.',
              labels=None, tags=None, **kw):
        return Item.objects.create(
            project=self.project,
            title=title,
            description=description,
            labels=labels or [],
            tags=tags or '',
            status='in_review',
            workflow_name='autonomous-dev',
            item_type='task',
            created_by_id=1,
            created_by_email='sora@shizuha.com',
            **kw,
        )

    def _comment(self, item, text):
        return Comment.objects.create(item=item, content=text, author_id=7,
                                      author_email='rin@shizuha.com')

    def _security_comment(self, item, text):
        """A comment authored by a Security-team member (roster-resolvable)."""
        return Comment.objects.create(item=item, content=text, author_id=99,
                                      author_email=self.SECURITY_REVIEWER)

    # ── classification ──────────────────────────────────────────────────────

    def test_con129_like_text_is_classified_as_trust_boundary(self):
        item = self._item()
        desc, _ = WorkflowService._stride_gate_texts(item)
        self.assertEqual(
            WorkflowService._trust_boundary_classification(item, desc), 'text')

    def test_explicit_label_classification_wins(self):
        item = self._item(description='innocuous', labels=['trust-boundary'])
        desc, _ = WorkflowService._stride_gate_texts(item)
        self.assertEqual(
            WorkflowService._trust_boundary_classification(item, desc), 'label')

    def test_plain_design_not_classified(self):
        item = self._item(
            title='HLD: internal cache eviction policy',
            description='LRU eviction for the task list cache.')
        desc, _ = WorkflowService._stride_gate_texts(item)
        self.assertIsNone(
            WorkflowService._trust_boundary_classification(item, desc))

    def test_non_design_task_not_gated(self):
        item = self._item(title='Fix cross-org contact path bug in DM routing')
        # not an HLD/design artifact → gate never fires even at approval
        WorkflowService._validate_stride_gate(item, 'done')  # no raise

    # ── requirement 1: threat-model section ────────────────────────────────

    def test_trust_boundary_design_without_threat_model_blocked(self):
        item = self._item()
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._validate_stride_gate(item, 'approved')
        self.assertIn('Threat Model', str(ctx.exception))

    def test_gate_only_fires_on_approval_class_targets(self):
        item = self._item()
        WorkflowService._validate_stride_gate(item, 'in_review')  # no raise
        WorkflowService._validate_stride_gate(item, 'blocked')    # no raise

    def test_partial_stride_section_blocked(self):
        item = self._item(description=self._item().description +
                          '\n## Threat Model\nSpoofing: n/a. Tampering: n/a. '
                          'Mitigation: none. trust boundary: DM edge.')
        with self.assertRaises(WorkflowValidationError):
            WorkflowService._validate_stride_gate(item, 'done')

    def test_threat_model_in_comment_satisfies_requirement_1(self):
        item = self._item(labels=['trust-boundary'])
        self._comment(item, THREAT_MODEL)
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._validate_stride_gate(item, 'approved')
        # requirement 1 satisfied → the remaining block is the sign-off
        self.assertIn('Security sign-off', str(ctx.exception))

    # ── requirement 2: security sign-off ────────────────────────────────────

    def test_threat_model_without_signoff_blocked(self):
        item = self._item(description=self._item().description + THREAT_MODEL)
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._validate_stride_gate(item, 'done')
        self.assertIn('Security sign-off', str(ctx.exception))

    def test_signoff_comment_from_security_author_unblocks(self):
        item = self._item(description=self._item().description + THREAT_MODEL)
        self._security_comment(
            item, 'Security sign-off: approved — reviewed the DM boundary.')
        WorkflowService._validate_stride_gate(item, 'done')  # no raise

    def test_signoff_comment_from_non_security_author_does_not_unblock(self):
        """PLAT-1253 review P1: a design author (or any non-security commenter)
        posting the exact sign-off text must NOT clear the gate — otherwise the
        Security-review control is trivially self-approvable."""
        item = self._item(description=self._item().description + THREAT_MODEL)
        # rin@ is not on the security roster
        self._comment(
            item, 'Security sign-off: approved — I reviewed my own design, lgtm.')
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._validate_stride_gate(item, 'done')
        self.assertIn('Security sign-off', str(ctx.exception))

    def test_bare_signoff_label_does_not_unblock(self):
        """PLAT-1253 review P1 (2nd): `labels` is an author-writable field, so a
        bare `security-signed-off` label carries no verifiable author and must NOT
        clear the gate — otherwise a task editor self-approves via a label."""
        item = self._item(
            description=self._item().description + THREAT_MODEL,
            labels=['trust-boundary', 'security-signed-off'])
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._validate_stride_gate(item, 'approved')
        self.assertIn('Security sign-off', str(ctx.exception))

    def test_bare_metadata_flag_does_not_unblock(self):
        """A bare-truthy `metadata.security_signoff` is likewise author-settable
        and must NOT clear the gate."""
        item = self._item(
            description=self._item().description + THREAT_MODEL,
            metadata={'security_signoff': True})
        with self.assertRaises(WorkflowValidationError):
            WorkflowService._validate_stride_gate(item, 'approved')

    def test_metadata_signoff_naming_non_security_signer_does_not_unblock(self):
        item = self._item(
            description=self._item().description + THREAT_MODEL,
            metadata={'security_signoff': {'by': 'rin@shizuha.com'}})  # not security
        with self.assertRaises(WorkflowValidationError):
            WorkflowService._validate_stride_gate(item, 'approved')

    def test_metadata_signoff_naming_security_signer_still_does_not_unblock(self):
        """PLAT-1253 review P1 (3rd): `metadata` is a client-writable value with no
        authenticated author — an editor can simply NAME a real Security member as
        the signer without being them, so even a metadata record that names a
        security signer must NOT clear the gate. The comment path (author_email is
        server-stamped) is the only trusted signal."""
        item = self._item(
            description=self._item().description + THREAT_MODEL,
            metadata={'security_signoff': {'by_email': self.SECURITY_REVIEWER,
                                           'at': '2026-07-03T00:00:00Z'}})
        with self.assertRaises(WorkflowValidationError):
            WorkflowService._validate_stride_gate(item, 'approved')

    # ── opt-out semantics ───────────────────────────────────────────────────

    def test_heuristic_classification_waivable_with_audit_marker(self):
        item = self._item()  # text-classified
        self._comment(item, 'stride: N/A — mentions DMs only as a notification '
                            'consumer; no boundary change.')
        WorkflowService._validate_stride_gate(item, 'done')  # no raise

    def test_label_classification_not_waivable(self):
        item = self._item(labels=['trust-boundary'])
        self._comment(item, 'stride: N/A — trying to skip the gate.')
        with self.assertRaises(WorkflowValidationError):
            WorkflowService._validate_stride_gate(item, 'done')

    def test_empty_na_reason_does_not_waive(self):
        item = self._item()
        self._comment(item, 'stride: N/A —   ')
        with self.assertRaises(WorkflowValidationError):
            WorkflowService._validate_stride_gate(item, 'done')

    # ── kill switch + end-to-end ────────────────────────────────────────────

    @override_settings(PULSE_STRIDE_HLD_GATE_ENABLED=False)
    def test_kill_switch_disables_gate(self):
        item = self._item()
        WorkflowService._validate_stride_gate(item, 'done')  # no raise

    def test_validate_transition_invokes_gate(self):
        """The gate is wired into the cross-workflow validate_transition path."""
        item = self._item()
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService.validate_transition(item, 'done')
        self.assertIn('STRIDE gate', str(ctx.exception))


# ── PLS-458: successor topology (fail-closed Security review chain) ────────


class StrideGateSuccessorTests(TestCase):
    """PLS-458: the STRIDE gate can be satisfied by a fail-closed successor
    topology (parent blocks Security-assigned successor, both block same anchor)
    instead of a direct Security sign-off comment.  Tests cover the successor
    discovery, the atomic commit-time revalidation, and the link-deletion guard.
    """

    SECURITY_REVIEWER = 'akira@shizuha.com'

    def setUp(self):
        WorkflowService._roster_cache = None
        from tasks.models import TeamRoster
        TeamRoster.objects.update_or_create(
            team_name='security',
            defaults=dict(tiers=[[self.SECURITY_REVIEWER]], is_active=True))
        # PLS-458: a non-security team so "reassigned away from Security"
        # fixtures can move the successor to a valid org member (rin@) that
        # satisfies main's DB org-authz trigger while NOT being on security.
        TeamRoster.objects.update_or_create(
            team_name='engineering',
            defaults=dict(tiers=[['rin@shizuha.com']], is_active=True))
        self.project = Project.objects.create(
            name='Hive', key='HIVE', org_id=1,
            access_level='private', is_active=True,
        )
        # Seed the autonomous-dev Workflow so execute_transition can resolve
        # it and validate 'done' as a valid target status.  The test DB does
        # not run data migrations, so we create it here.
        self._ensure_workflow()

    def _ensure_workflow(self):
        """Create the autonomous-dev Workflow with statuses the tests need."""
        from tasks.models import Status, Workflow, WorkflowStatus, WorkflowTransition
        for slug, name, category in (
            ('open', 'Open', 'todo'),
            ('in_progress', 'In Progress', 'in_progress'),
            ('in_review', 'In Review', 'in_progress'),
            ('done', 'Done', 'done'),
        ):
            Status.objects.get_or_create(
                slug=slug,
                defaults={'name': name, 'category': category, 'is_system': True},
            )
        open_status = Status.objects.get(slug='open')
        wf, _ = Workflow.objects.get_or_create(
            slug='autonomous-dev',
            defaults={
                'name': 'Autonomous Dev Workflow',
                'initial_status': open_status,
                'is_system': True,
                'is_active': True,
            },
        )
        for position, slug in enumerate(('open', 'in_progress', 'in_review', 'done')):
            WorkflowStatus.objects.get_or_create(
                workflow=wf,
                status=Status.objects.get(slug=slug),
                defaults={'position': position},
            )
        # Wire a simple in_review → done transition so execute_transition
        # can find a matching WorkflowTransition.
        WorkflowTransition.objects.get_or_create(
            workflow=wf,
            from_status='in_review',
            to_status='done',
            defaults={'name': 'Done', 'position': 0},
        )

    def _parent(self, **kw):
        """A trust-boundary design item with a threat model."""
        return Item.objects.create(
            project=self.project,
            title='HLD: external onboarding via Connect DM boundary',
            description=kw.pop('description', (
                'External onboarding lets external users receive outbound DM '
                'messages across a cross-org contact path.\n'
                '## Threat Model (STRIDE-lite)\n'
                'Assets: agent identities. Trust boundaries: Connect DM edge.\n'
                'Spoofing: tokens. Tampering: signed. Repudiation: audit log.\n'
                'Information Disclosure: scoped. Denial of Service: rate limits.\n'
                'Elevation of Privilege: no admin paths.\n'
                'Mitigation: shared-org auth; residual risk accepted.'
            )),
            labels=kw.pop('labels', None) or [],
            tags=kw.pop('tags', None) or '',
            status='in_review',
            workflow_name='autonomous-dev',
            item_type='task',
            created_by_id=1,
            created_by_email='sora@shizuha.com',
            **kw,
        )

    def _successor(self, **kw):
        """A Security-assigned active item."""
        return Item.objects.create(
            project=self.project,
            title='Security review: external onboarding DM boundary',
            description='Review the Connect DM cross-org boundary.',
            labels=[],
            tags='',
            status='in_progress',
            workflow_name='autonomous-dev',
            item_type='task',
            created_by_id=1,
            created_by_email='akira@shizuha.com',
            assignee_email=self.SECURITY_REVIEWER,
            **kw,
        )

    def _anchor(self, **kw):
        """A downstream release/design item that both parent and successor block."""
        return Item.objects.create(
            project=self.project,
            title='Release: Hive external onboarding v2',
            description='Ship external onboarding feature.',
            labels=[],
            tags='',
            status='in_progress',
            workflow_name='autonomous-dev',
            item_type='task',
            created_by_id=1,
            created_by_email='sora@shizuha.com',
            **kw,
        )

    def _link(self, source, target, link_type='blocks'):
        from tasks.models import ItemLink
        return ItemLink.objects.create(
            source_item=source,
            target_item=target,
            link_type=link_type,
            created_by_id=1,
        )

    def _setup_topology(self):
        """Create parent, successor, anchor with the exact successor topology:
            parent ──blocks──▶ successor (Security-assigned, active)
            parent ──blocks──▶ anchor (active)
            successor ──blocks──▶ anchor (same item)
        Returns (parent, successor, anchor).
        """
        parent = self._parent()
        successor = self._successor()
        anchor = self._anchor()
        self._link(parent, successor)   # parent blocks successor
        self._link(parent, anchor)      # parent blocks anchor
        self._link(successor, anchor)   # successor blocks anchor
        return parent, successor, anchor

    # ── successor topology discovery ────────────────────────────────────────

    def test_has_linked_active_security_successor_detects_topology(self):
        parent, _, _ = self._setup_topology()
        self.assertTrue(
            WorkflowService._has_linked_active_security_successor(parent))

    def test_has_linked_active_security_successor_false_without_links(self):
        parent = self._parent()
        self.assertFalse(
            WorkflowService._has_linked_active_security_successor(parent))

    def test_has_linked_active_security_successor_false_when_successor_terminal(self):
        parent, successor, anchor = self._setup_topology()
        successor.status = 'done'
        successor.save(update_fields=['status'])
        self.assertFalse(
            WorkflowService._has_linked_active_security_successor(parent))

    def test_has_linked_active_security_successor_false_when_successor_not_security(self):
        parent, successor, anchor = self._setup_topology()
        # main added save-time + DB-trigger org-authz on assignee_email; use a
        # direct update under a team rin@ belongs to, so the successor is
        # legitimately reassigned AWAY from Security without tripping it.
        Item.objects.filter(pk=successor.pk).update(
            assignee_email='rin@shizuha.com', assignment_group='engineering')
        successor.refresh_from_db()
        self.assertFalse(
            WorkflowService._has_linked_active_security_successor(parent))

    def test_has_linked_active_security_successor_false_when_anchor_terminal(self):
        parent, successor, anchor = self._setup_topology()
        anchor.status = 'done'
        anchor.save(update_fields=['status'])
        self.assertFalse(
            WorkflowService._has_linked_active_security_successor(parent))

    # ── successor topology satisfies the STRIDE gate ────────────────────────

    def test_successor_topology_passes_stride_gate(self):
        parent, _, _ = self._setup_topology()
        # Should not raise — the successor topology satisfies the gate
        WorkflowService._validate_stride_gate(parent, 'done')
        self.assertTrue(getattr(parent, '_stride_passed_via_successor', False))

    def test_successor_topology_creates_security_gate_state(self):
        from tasks.models import SecurityGateState
        parent, successor, anchor = self._setup_topology()
        WorkflowService._validate_stride_gate(parent, 'done')
        gate = SecurityGateState.objects.filter(
            parent_item=parent, status='active').first()
        self.assertIsNotNone(gate)
        self.assertEqual(gate.successor_item_id, successor.pk)
        self.assertEqual(gate.anchor_item_id, anchor.pk)

    def test_successor_topology_without_threat_model_still_blocked(self):
        """The successor topology only bypasses the sign-off requirement, not
        the threat-model requirement."""
        parent = self._parent(description='No threat model here.')
        successor = self._successor()
        anchor = self._anchor()
        self._link(parent, successor)
        self._link(parent, anchor)
        self._link(successor, anchor)
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._validate_stride_gate(parent, 'done')
        self.assertIn('Threat Model', str(ctx.exception))

    # ── atomic commit-time revalidation ─────────────────────────────────────

    def test_atomic_revalidation_passes_with_intact_topology(self):
        parent, _, _ = self._setup_topology()
        WorkflowService._validate_stride_gate(parent, 'done')
        # The topology is intact — revalidation should pass
        gate = WorkflowService._validate_security_gate_state_atomic(parent)
        self.assertIsNotNone(gate)
        self.assertEqual(gate.status, 'active')

    def test_atomic_revalidation_fails_when_successor_anchor_link_deleted(self):
        parent, successor, anchor = self._setup_topology()
        WorkflowService._validate_stride_gate(parent, 'done')
        # Delete the successor→anchor blocks link
        from tasks.models import ItemLink
        ItemLink.objects.filter(
            source_item_id=successor.pk,
            target_item_id=anchor.pk,
            link_type='blocks',
        ).delete()
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._validate_security_gate_state_atomic(parent)
        self.assertIn('deleted after gate validation', str(ctx.exception))

    def test_atomic_revalidation_fails_when_parent_successor_link_deleted(self):
        parent, successor, anchor = self._setup_topology()
        WorkflowService._validate_stride_gate(parent, 'done')
        from tasks.models import ItemLink
        ItemLink.objects.filter(
            source_item_id=parent.pk,
            target_item_id=successor.pk,
            link_type='blocks',
        ).delete()
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._validate_security_gate_state_atomic(parent)
        self.assertIn('deleted after gate validation', str(ctx.exception))

    def test_atomic_revalidation_fails_when_parent_anchor_link_deleted(self):
        parent, successor, anchor = self._setup_topology()
        WorkflowService._validate_stride_gate(parent, 'done')
        from tasks.models import ItemLink
        ItemLink.objects.filter(
            source_item_id=parent.pk,
            target_item_id=anchor.pk,
            link_type='blocks',
        ).delete()
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._validate_security_gate_state_atomic(parent)
        self.assertIn('deleted after gate validation', str(ctx.exception))

    def test_atomic_revalidation_fails_when_successor_reassigned(self):
        parent, successor, anchor = self._setup_topology()
        WorkflowService._validate_stride_gate(parent, 'done')
        # main added save-time + DB-trigger org-authz on assignee_email; use a
        # direct update under a team rin@ belongs to, so the successor is
        # legitimately reassigned AWAY from Security without tripping it.
        Item.objects.filter(pk=successor.pk).update(
            assignee_email='rin@shizuha.com', assignment_group='engineering')
        successor.refresh_from_db()
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._validate_security_gate_state_atomic(parent)
        self.assertIn('reassigned away from the Security team',
                      str(ctx.exception))

    def test_atomic_revalidation_fails_when_successor_terminalized(self):
        parent, successor, anchor = self._setup_topology()
        WorkflowService._validate_stride_gate(parent, 'done')
        successor.status = 'done'
        successor.save(update_fields=['status'])
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._validate_security_gate_state_atomic(parent)
        self.assertIn('terminalized after gate validation',
                      str(ctx.exception))

    def test_atomic_revalidation_fails_when_anchor_terminalized(self):
        parent, successor, anchor = self._setup_topology()
        WorkflowService._validate_stride_gate(parent, 'done')
        anchor.status = 'done'
        anchor.save(update_fields=['status'])
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._validate_security_gate_state_atomic(parent)
        self.assertIn('terminalized after gate validation',
                      str(ctx.exception))

    def test_atomic_revalidation_fails_when_gate_state_deleted(self):
        parent, _, _ = self._setup_topology()
        WorkflowService._validate_stride_gate(parent, 'done')
        from tasks.models import SecurityGateState
        SecurityGateState.objects.filter(parent_item=parent).delete()
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._validate_security_gate_state_atomic(parent)
        self.assertIn('security gate state not found', str(ctx.exception))

    def test_atomic_revalidation_fails_when_no_gate_state(self):
        """If _validate_stride_gate was never called, there's no gate state."""
        parent, _, _ = self._setup_topology()
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._validate_security_gate_state_atomic(parent)
        self.assertIn('security gate state not found', str(ctx.exception))

    # ── execute_transition integration ──────────────────────────────────────

    def test_execute_transition_passes_with_intact_topology(self):
        """Full execute_transition should succeed when the successor topology
        is intact at commit time."""
        parent, _, _ = self._setup_topology()
        # execute_transition calls validate_transition → _validate_stride_gate
        # then _validate_security_gate_state_atomic before the status write
        item = WorkflowService.execute_transition(parent, 'done')
        self.assertEqual(item.status, 'done')

    def test_execute_transition_blocks_when_link_deleted_before_commit(self):
        """Simulate a race: the successor→anchor link is deleted between
        validation and the commit-time revalidation."""
        parent, successor, anchor = self._setup_topology()
        # Monkey-patch to simulate the race: delete the link right after
        # _validate_stride_gate sets _stride_passed_via_successor
        original_validate = WorkflowService._validate_stride_gate

        def _validate_and_delete(item, new_status):
            original_validate(item, new_status)
            if getattr(item, '_stride_passed_via_successor', False):
                from tasks.models import ItemLink
                ItemLink.objects.filter(
                    source_item_id=successor.pk,
                    target_item_id=anchor.pk,
                    link_type='blocks',
                ).delete()

        WorkflowService._validate_stride_gate = staticmethod(_validate_and_delete)
        try:
            with self.assertRaises(WorkflowValidationError) as ctx:
                WorkflowService.execute_transition(parent, 'done')
            self.assertIn('deleted after gate validation', str(ctx.exception))
        finally:
            WorkflowService._validate_stride_gate = staticmethod(original_validate)

    def test_execute_transition_blocks_when_successor_reassigned_before_commit(self):
        """Simulate a race: the successor is reassigned away from Security
        between validation and commit-time revalidation."""
        parent, successor, anchor = self._setup_topology()
        original_validate = WorkflowService._validate_stride_gate

        def _validate_and_reassign(item, new_status):
            original_validate(item, new_status)
            if getattr(item, '_stride_passed_via_successor', False):
                Item.objects.filter(pk=successor.pk).update(
                    assignee_email='rin@shizuha.com',
                    assignment_group='engineering')

        WorkflowService._validate_stride_gate = staticmethod(_validate_and_reassign)
        try:
            with self.assertRaises(WorkflowValidationError) as ctx:
                WorkflowService.execute_transition(parent, 'done')
            self.assertIn('reassigned away from the Security team',
                          str(ctx.exception))
        finally:
            WorkflowService._validate_stride_gate = staticmethod(original_validate)


# ── PLS-458: gate release on successor terminalization ─────────────────────


class StrideGateReleaseTests(TestCase):
    """PLS-458: the SecurityGateState must be released when the Security
    successor reaches a terminal state.  Tests cover the release mechanism,
    idempotency, and the post-release state of the gate record.
    """

    SECURITY_REVIEWER = 'akira@shizuha.com'

    def setUp(self):
        WorkflowService._roster_cache = None
        from tasks.models import TeamRoster
        TeamRoster.objects.update_or_create(
            team_name='security',
            defaults=dict(tiers=[[self.SECURITY_REVIEWER]], is_active=True))
        # PLS-458: a non-security team so "reassigned away from Security"
        # fixtures can move the successor to a valid org member (rin@) that
        # satisfies main's DB org-authz trigger while NOT being on security.
        TeamRoster.objects.update_or_create(
            team_name='engineering',
            defaults=dict(tiers=[['rin@shizuha.com']], is_active=True))
        self.project = Project.objects.create(
            name='Hive', key='HIVE', org_id=1,
            access_level='private', is_active=True,
        )
        self._ensure_workflow()

    def _ensure_workflow(self):
        from tasks.models import Status, Workflow, WorkflowStatus, WorkflowTransition
        for slug, name, category in (
            ('open', 'Open', 'todo'),
            ('in_progress', 'In Progress', 'in_progress'),
            ('in_review', 'In Review', 'in_progress'),
            ('done', 'Done', 'done'),
        ):
            Status.objects.get_or_create(
                slug=slug,
                defaults={'name': name, 'category': category, 'is_system': True},
            )
        open_status = Status.objects.get(slug='open')
        wf, _ = Workflow.objects.get_or_create(
            slug='autonomous-dev',
            defaults={
                'name': 'Autonomous Dev Workflow',
                'initial_status': open_status,
                'is_system': True,
                'is_active': True,
            },
        )
        for position, slug in enumerate(('open', 'in_progress', 'in_review', 'done')):
            WorkflowStatus.objects.get_or_create(
                workflow=wf,
                status=Status.objects.get(slug=slug),
                defaults={'position': position},
            )
        WorkflowTransition.objects.get_or_create(
            workflow=wf,
            from_status='in_progress',
            to_status='done',
            defaults={'name': 'Done', 'position': 0},
        )
        WorkflowTransition.objects.get_or_create(
            workflow=wf,
            from_status='in_review',
            to_status='done',
            defaults={'name': 'Done', 'position': 1},
        )

    def _parent(self, **kw):
        return Item.objects.create(
            project=self.project,
            title=kw.pop('title', 'HLD: external onboarding via Connect DM boundary'),
            description=kw.pop('description', (
                'External onboarding lets external users receive outbound DM '
                'messages across a cross-org contact path.\n'
                '## Threat Model (STRIDE-lite)\n'
                'Assets: agent identities. Trust boundaries: Connect DM edge.\n'
                'Spoofing: tokens. Tampering: signed. Repudiation: audit log.\n'
                'Information Disclosure: scoped. Denial of Service: rate limits.\n'
                'Elevation of Privilege: no admin paths.\n'
                'Mitigation: shared-org auth; residual risk accepted.'
            )),
            labels=kw.pop('labels', None) or [],
            tags=kw.pop('tags', None) or '',
            status=kw.pop('status', 'in_review'),
            workflow_name='autonomous-dev',
            item_type='task',
            created_by_id=1,
            created_by_email='sora@shizuha.com',
            **kw,
        )

    def _successor(self, **kw):
        return Item.objects.create(
            project=self.project,
            title=kw.pop('title', 'Security review: external onboarding DM boundary'),
            description=kw.pop('description', 'Review the Connect DM cross-org boundary.'),
            labels=kw.pop('labels', None) or [],
            tags=kw.pop('tags', None) or '',
            status=kw.pop('status', 'in_progress'),
            workflow_name='autonomous-dev',
            item_type='task',
            created_by_id=1,
            created_by_email='akira@shizuha.com',
            assignee_email=self.SECURITY_REVIEWER,
            **kw,
        )

    def _anchor(self, **kw):
        return Item.objects.create(
            project=self.project,
            title=kw.pop('title', 'Release: Hive external onboarding v2'),
            description=kw.pop('description', 'Ship external onboarding feature.'),
            labels=kw.pop('labels', None) or [],
            tags=kw.pop('tags', None) or '',
            status=kw.pop('status', 'in_progress'),
            workflow_name='autonomous-dev',
            item_type='task',
            created_by_id=1,
            created_by_email='sora@shizuha.com',
            **kw,
        )

    def _link(self, source, target, link_type='blocks'):
        from tasks.models import ItemLink
        return ItemLink.objects.create(
            source_item=source,
            target_item=target,
            link_type=link_type,
            created_by_id=1,
        )

    def _setup_topology(self):
        parent = self._parent()
        successor = self._successor()
        anchor = self._anchor()
        self._link(parent, successor)
        self._link(parent, anchor)
        self._link(successor, anchor)
        return parent, successor, anchor

    def _create_gate_state(self, parent, successor, anchor):
        """Create an active SecurityGateState for the topology."""
        from tasks.models import SecurityGateState
        return SecurityGateState.objects.create(
            parent_item=parent,
            successor_item=successor,
            anchor_item=anchor,
            status='active',
        )

    # ── gate release on successor terminalization ───────────────────────────

    def test_release_gate_when_successor_terminalizes(self):
        """The SecurityGateState should be released when the successor reaches
        a terminal state via execute_transition."""
        from tasks.models import SecurityGateState
        parent, successor, anchor = self._setup_topology()
        self._create_gate_state(parent, successor, anchor)

        # Terminalize the successor
        WorkflowService.execute_transition(successor, 'done')

        # The gate should be released
        gate = SecurityGateState.objects.get(parent_item=parent)
        self.assertEqual(gate.status, 'released')

    def test_release_gate_idempotent(self):
        """Calling _release_security_gate_for_successor on a successor with no
        active gate should be a no-op (not raise)."""
        parent, successor, anchor = self._setup_topology()
        # No gate state created — should not raise
        WorkflowService._release_security_gate_for_successor(successor)

    def test_release_gate_only_releases_this_successor(self):
        """If the same successor is used in multiple gate records, only the
        active ones should be released."""
        from tasks.models import SecurityGateState
        parent1, successor, anchor1 = self._setup_topology()
        parent2 = self._parent(title='HLD: another design')
        anchor2 = self._anchor(title='Release: another feature')
        self._link(parent2, successor)
        self._link(parent2, anchor2)
        self._link(successor, anchor2)

        gate1 = self._create_gate_state(parent1, successor, anchor1)
        gate2 = SecurityGateState.objects.create(
            parent_item=parent2,
            successor_item=successor,
            anchor_item=anchor2,
            status='active',
        )

        # Terminalize the successor
        WorkflowService.execute_transition(successor, 'done')

        # Both gates should be released
        gate1.refresh_from_db()
        gate2.refresh_from_db()
        self.assertEqual(gate1.status, 'released')
        self.assertEqual(gate2.status, 'released')

    def test_gate_state_does_not_block_non_security_terminalization(self):
        """A non-Security item's terminal transition should not affect
        SecurityGateState records (no false release)."""
        from tasks.models import SecurityGateState
        parent, successor, anchor = self._setup_topology()
        self._create_gate_state(parent, successor, anchor)

        # Terminalize a non-successor item (the anchor)
        WorkflowService.execute_transition(anchor, 'done')

        # The gate should still be active
        gate = SecurityGateState.objects.get(parent_item=parent)
        self.assertEqual(gate.status, 'active')

    # ── successor reassignment guard ────────────────────────────────────────

    def test_reassign_successor_to_security_member_allowed(self):
        """Reassigning the successor to another Security team member should be
        allowed — the successor stays Security-assigned."""
        from tasks.models import SecurityGateState
        parent, successor, anchor = self._setup_topology()
        self._create_gate_state(parent, successor, anchor)

        # Reassign to another Security member
        successor.assignee_email = 'rei@shizuha.com'
        # Need to seed the roster for rei
        from tasks.models import TeamRoster
        TeamRoster.objects.update_or_create(
            team_name='security',
            defaults=dict(tiers=[['akira@shizuha.com', 'rei@shizuha.com']], is_active=True))
        WorkflowService._roster_cache = None
        successor.save(update_fields=['assignee_email'])

        # Gate should still be active
        gate = SecurityGateState.objects.get(parent_item=parent)
        self.assertEqual(gate.status, 'active')

    def test_reassign_successor_to_non_security_blocked_by_guard(self):
        """Reassigning the successor to a non-Security member should be blocked
        by the guard in views.py."""
        from tasks.models import SecurityGateState
        parent, successor, anchor = self._setup_topology()
        self._create_gate_state(parent, successor, anchor)

        # The guard is in views.py perform_update, but we can test the
        # underlying check: SecurityGateState.objects.filter(successor_item=item,
        # status='active').exists() should be True, and
        # WorkflowService._is_security_team_email('rin@shizuha.com') should be False
        self.assertTrue(
            SecurityGateState.objects.filter(
                successor_item=successor, status='active').exists())
        self.assertFalse(
            WorkflowService._is_security_team_email('rin@shizuha.com'))

    # ── post-terminalization scenarios ──────────────────────────────────────

    def test_parent_cannot_reuse_released_gate(self):
        """After the successor terminalizes and the gate is released, the parent
        cannot re-use the same gate record — it must create a new successor
        topology."""
        from tasks.models import SecurityGateState
        parent, successor, anchor = self._setup_topology()
        self._create_gate_state(parent, successor, anchor)

        # Terminalize the successor
        WorkflowService.execute_transition(successor, 'done')

        # The gate is released
        gate = SecurityGateState.objects.get(parent_item=parent)
        self.assertEqual(gate.status, 'released')

        # The parent's _has_linked_active_security_successor should now be False
        # because the successor is terminal
        self.assertFalse(
            WorkflowService._has_linked_active_security_successor(parent))

    def test_anchor_swap_detected_by_revalidation(self):
        """If the anchor is swapped (parent blocks a different item) between
        validation and commit, the revalidation should catch it."""
        parent, successor, anchor = self._setup_topology()
        WorkflowService._validate_stride_gate(parent, 'done')

        # Swap the anchor: delete the parent→anchor link and create a new one
        from tasks.models import ItemLink
        ItemLink.objects.filter(
            source_item_id=parent.pk,
            target_item_id=anchor.pk,
            link_type='blocks',
        ).delete()
        new_anchor = self._anchor(title='Release: different feature')
        self._link(parent, new_anchor)

        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._validate_security_gate_state_atomic(parent)
        self.assertIn('deleted after gate validation', str(ctx.exception))

    def test_successor_reassignment_after_parent_terminalization(self):
        """After the parent terminalizes, the successor can be reassigned
        because the gate state is released when the successor terminalizes,
        not when the parent does.  The successor must still complete its
        Security review."""
        from tasks.models import SecurityGateState
        parent, successor, anchor = self._setup_topology()
        self._create_gate_state(parent, successor, anchor)

        # Terminalize the parent (this does NOT release the gate — the
        # successor must still complete its review)
        WorkflowService.execute_transition(parent, 'done')

        # Gate should still be active (successor hasn't terminalized yet)
        gate = SecurityGateState.objects.get(parent_item=parent)
        self.assertEqual(gate.status, 'active')

        # Now terminalize the successor
        WorkflowService.execute_transition(successor, 'done')

        # Gate should be released
        gate.refresh_from_db()
        self.assertEqual(gate.status, 'released')


# ── PLS-458 review P1 regressions ──────────────────────────────────────────

class _GateTopologyMixin:
    """Shared builders for the PLS-458 review-P1 regression suites."""

    SECURITY_REVIEWER = 'akira@shizuha.com'

    _TM = (
        '## Threat Model (STRIDE-lite)\n'
        'Assets: agent identities. Trust boundaries: Connect DM edge.\n'
        'Spoofing: tokens. Tampering: signed. Repudiation: audit log.\n'
        'Information Disclosure: scoped. Denial of Service: rate limits.\n'
        'Elevation of Privilege: no admin paths.\n'
        'Mitigation: shared-org auth; residual risk accepted.'
    )

    def _seed(self):
        WorkflowService._roster_cache = None
        from tasks.models import (
            Status, Workflow, WorkflowStatus, WorkflowTransition, TeamRoster,
        )
        TeamRoster.objects.update_or_create(
            team_name='security',
            defaults=dict(tiers=[[self.SECURITY_REVIEWER]], is_active=True))
        TeamRoster.objects.update_or_create(
            team_name='engineering',
            defaults=dict(tiers=[['rin@shizuha.com']], is_active=True))
        self.project = Project.objects.create(
            name='Hive', key='HIVE', org_id=1,
            access_level='private', is_active=True)
        # A SECOND, unrelated project for the cross-project negative controls.
        self.other_project = Project.objects.create(
            name='Cortex', key='CTX', org_id=1,
            access_level='private', is_active=True)
        for slug, name, category in (
            ('open', 'Open', 'todo'),
            ('in_progress', 'In Progress', 'in_progress'),
            ('in_review', 'In Review', 'in_progress'),
            ('done', 'Done', 'done'),
            ('cancelled', 'Cancelled', 'done'),
            ('rejected', 'Rejected', 'done'),
        ):
            Status.objects.get_or_create(
                slug=slug,
                defaults={'name': name, 'category': category, 'is_system': True})
        wf, _ = Workflow.objects.get_or_create(
            slug='autonomous-dev',
            defaults={'name': 'Autonomous Dev Workflow',
                      'initial_status': Status.objects.get(slug='open'),
                      'is_system': True, 'is_active': True})
        for position, slug in enumerate(
                ('open', 'in_progress', 'in_review', 'done', 'cancelled', 'rejected')):
            WorkflowStatus.objects.get_or_create(
                workflow=wf, status=Status.objects.get(slug=slug),
                defaults={'position': position})
        for i, to in enumerate(('done', 'cancelled', 'rejected')):
            WorkflowTransition.objects.get_or_create(
                workflow=wf, from_status='in_progress', to_status=to,
                defaults={'name': to.title(), 'position': i})
            WorkflowTransition.objects.get_or_create(
                workflow=wf, from_status='in_review', to_status=to,
                defaults={'name': to.title(), 'position': 10 + i})

    def _parent(self, project=None, **kw):
        return Item.objects.create(
            project=project or self.project,
            title=kw.pop('title', 'HLD: external onboarding via Connect DM boundary'),
            description=kw.pop('description',
                               'External onboarding across a trust boundary.\n' + self._TM),
            labels=[], tags='', status=kw.pop('status', 'in_review'),
            workflow_name='autonomous-dev', item_type='task',
            created_by_id=1, created_by_email='sora@shizuha.com', **kw)

    def _successor(self, project=None, **kw):
        return Item.objects.create(
            project=project or self.project,
            title=kw.pop('title', 'Security review: DM boundary'),
            description='Review the Connect DM boundary.', labels=[], tags='',
            status=kw.pop('status', 'in_progress'), workflow_name='autonomous-dev',
            item_type='task', created_by_id=1, created_by_email='akira@shizuha.com',
            assignee_email=kw.pop('assignee_email', self.SECURITY_REVIEWER), **kw)

    def _anchor(self, project=None, **kw):
        return Item.objects.create(
            project=project or self.project,
            title=kw.pop('title', 'Release: external onboarding v2'),
            description='Ship it.', labels=[], tags='',
            status=kw.pop('status', 'in_progress'), workflow_name='autonomous-dev',
            item_type='task', created_by_id=1, created_by_email='sora@shizuha.com', **kw)

    def _link(self, source, target, link_type='blocks'):
        from tasks.models import ItemLink
        return ItemLink.objects.create(
            source_item=source, target_item=target,
            link_type=link_type, created_by_id=1)

    def _topology(self, successor_project=None, anchor_project=None):
        parent = self._parent()
        successor = self._successor(project=successor_project or self.project)
        anchor = self._anchor(project=anchor_project or self.project)
        self._link(parent, successor)
        self._link(parent, anchor)
        self._link(successor, anchor)
        return parent, successor, anchor

    def _gate(self, parent, successor, anchor, status='active'):
        from tasks.models import SecurityGateState
        return SecurityGateState.objects.create(
            parent_item=parent, successor_item=successor,
            anchor_item=anchor, status=status)


class StrideGateCrossProjectTests(_GateTopologyMixin, TestCase):
    """PLS-458 review P1 #1: the successor topology must be SAME-PROJECT. A
    non-Security actor must not clear the STRIDE gate by wiring an unrelated
    Security task/anchor from another project (the error copy already promises
    that cross-project links do not count)."""

    def setUp(self):
        self._seed()

    def test_same_project_topology_passes_positive_control(self):
        parent, _, _ = self._topology()
        self.assertTrue(
            WorkflowService._has_linked_active_security_successor(parent))
        WorkflowService._validate_stride_gate(parent, 'done')
        self.assertTrue(getattr(parent, '_stride_passed_via_successor', False))

    def test_cross_project_successor_rejected(self):
        parent, _, _ = self._topology(successor_project=self.other_project)
        self.assertFalse(
            WorkflowService._has_linked_active_security_successor(parent))
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._validate_stride_gate(parent, 'done')
        self.assertIn('Security sign-off', str(ctx.exception))

    def test_cross_project_anchor_rejected(self):
        parent, _, _ = self._topology(anchor_project=self.other_project)
        self.assertFalse(
            WorkflowService._has_linked_active_security_successor(parent))

    def test_atomic_revalidation_rejects_cross_project_gate(self):
        """Even if a cross-project gate row somehow exists (stale row, direct
        write, or a topology assembled before this contract), the LOCKED
        commit-time revalidation must refuse it — defence in depth behind
        _has_linked_active_security_successor."""
        parent, successor, anchor = self._topology(
            successor_project=self.other_project)
        # Build the gate record directly: _validate_stride_gate would (correctly)
        # refuse to create one for this cross-project shape.
        self._gate(parent, successor, anchor)
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._validate_security_gate_state_atomic(parent)
        self.assertIn('spans projects', str(ctx.exception))


class StrideGateReleaseSuccessOnlyTests(_GateTopologyMixin, TestCase):
    """PLS-458 review P1 #3: the gate releases ONLY on an authenticated
    Security-SUCCESS completion. Non-success terminals and success terminals
    reached by a non-Security successor stay fail-closed."""

    def setUp(self):
        self._seed()

    def test_success_terminal_by_security_releases(self):
        from tasks.models import SecurityGateState
        parent, successor, anchor = self._topology()
        self._gate(parent, successor, anchor)
        WorkflowService.execute_transition(successor, 'done')
        self.assertEqual(
            SecurityGateState.objects.get(parent_item=parent).status, 'released')

    def test_cancelled_successor_keeps_gate_active(self):
        # 'cancelled' is a non-success terminal -> must NOT release. Driven
        # directly because execute_transition('cancelled') has an orthogonal
        # reason-category precondition; the 'rejected' sibling covers end-to-end.
        from tasks.models import SecurityGateState
        parent, successor, anchor = self._topology()
        self._gate(parent, successor, anchor)
        successor.status = 'cancelled'
        WorkflowService._release_security_gate_for_successor(
            successor, resolved_status='cancelled')
        self.assertEqual(
            SecurityGateState.objects.get(parent_item=parent).status, 'active')

    def test_rejected_successor_keeps_gate_active(self):
        from tasks.models import SecurityGateState
        parent, successor, anchor = self._topology()
        self._gate(parent, successor, anchor)
        WorkflowService.execute_transition(successor, 'rejected')
        self.assertEqual(
            SecurityGateState.objects.get(parent_item=parent).status, 'active')

    def test_success_terminal_by_non_security_stays_fail_closed(self):
        from tasks.models import SecurityGateState
        parent, successor, anchor = self._topology()
        self._gate(parent, successor, anchor)
        Item.objects.filter(pk=successor.pk).update(
            assignee_email='rin@shizuha.com', assignment_group='engineering')
        successor.refresh_from_db()
        WorkflowService._release_security_gate_for_successor(
            successor, resolved_status='done')
        self.assertEqual(
            SecurityGateState.objects.get(parent_item=parent).status, 'active')


class StrideGateLinkDeletionGuardTests(_GateTopologyMixin, TestCase):
    """PLS-458 review P1 #2: EVERY link-deletion path funnels through the single
    guarded boundary — the service boundary AND both API routes (the per-item
    `links/{id}` action and ItemLinkViewSet.destroy)."""

    def setUp(self):
        self._seed()
        from django.contrib.auth import get_user_model
        self.staff = get_user_model().objects.create_user(
            username='pls458-staff', password='x', is_staff=True)

    def test_guard_blocks_each_load_bearing_edge(self):
        from tasks.models import ItemLink
        parent, successor, anchor = self._topology()
        self._gate(parent, successor, anchor)
        for src, tgt in ((parent, successor), (parent, anchor), (successor, anchor)):
            link = ItemLink.objects.get(
                source_item=src, target_item=tgt, link_type='blocks')
            with self.assertRaises(SecurityGateLinkProtectedError):
                WorkflowService.delete_link_guarded(link)
            self.assertTrue(ItemLink.objects.filter(pk=link.pk).exists())

    def test_guard_allows_unrelated_link(self):
        from tasks.models import ItemLink
        parent, successor, anchor = self._topology()
        self._gate(parent, successor, anchor)
        extra = self._anchor(title='Unrelated doc')
        rel = self._link(parent, extra, link_type='relates_to')
        self.assertTrue(WorkflowService.delete_link_guarded(rel))
        self.assertFalse(ItemLink.objects.filter(pk=rel.pk).exists())

    def test_guard_allows_deletion_after_gate_released(self):
        from tasks.models import ItemLink
        parent, successor, anchor = self._topology()
        self._gate(parent, successor, anchor, status='released')
        link = ItemLink.objects.get(
            source_item=successor, target_item=anchor, link_type='blocks')
        self.assertTrue(WorkflowService.delete_link_guarded(link))
        self.assertFalse(ItemLink.objects.filter(pk=link.pk).exists())

    def test_api_destroy_blocked_by_guard(self):
        from rest_framework.test import APIRequestFactory, force_authenticate
        from tasks.models import ItemLink
        from tasks.views import ItemLinkViewSet
        parent, successor, anchor = self._topology()
        self._gate(parent, successor, anchor)
        link = ItemLink.objects.get(
            source_item=parent, target_item=successor, link_type='blocks')
        view = ItemLinkViewSet.as_view({'delete': 'destroy'})
        req = APIRequestFactory().delete(f'/api/links/{link.id}/')
        force_authenticate(req, user=self.staff)
        resp = view(req, pk=str(link.id))
        self.assertEqual(resp.status_code, 409)
        self.assertTrue(ItemLink.objects.filter(pk=link.pk).exists())

    def test_api_destroy_allows_unprotected_link(self):
        from rest_framework.test import APIRequestFactory, force_authenticate
        from tasks.models import ItemLink
        from tasks.views import ItemLinkViewSet
        parent, successor, anchor = self._topology()
        self._gate(parent, successor, anchor)
        extra = self._anchor(title='Unrelated doc')
        rel = self._link(parent, extra, link_type='relates_to')
        view = ItemLinkViewSet.as_view({'delete': 'destroy'})
        req = APIRequestFactory().delete(f'/api/links/{rel.id}/')
        force_authenticate(req, user=self.staff)
        resp = view(req, pk=str(rel.id))
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(ItemLink.objects.filter(pk=rel.pk).exists())


class StrideGateLinkDeletionConcurrencyTests(_GateTopologyMixin, TransactionTestCase):
    """PLS-458 review P1 #2 (concurrency): the guard's lock-and-recheck holds
    under concurrent deletion — a check-then-delete race cannot leave an active
    gate referencing a deleted link."""

    reset_sequences = True

    def setUp(self):
        self._seed()

    def test_concurrent_deletes_of_protected_link_all_blocked(self):
        import threading
        from tasks.models import ItemLink
        parent, successor, anchor = self._topology()
        self._gate(parent, successor, anchor)
        link_pk = ItemLink.objects.get(
            source_item=successor, target_item=anchor, link_type='blocks').pk

        results = []
        barrier = threading.Barrier(4)

        def worker():
            from django.db import connection
            try:
                barrier.wait(timeout=10)
                link = ItemLink.objects.get(pk=link_pk)
                WorkflowService.delete_link_guarded(link)
                results.append('deleted')
            except SecurityGateLinkProtectedError:
                results.append('blocked')
            except Exception as exc:  # pragma: no cover - surfaced in assert
                results.append(('err', repr(exc)))
            finally:
                connection.close()

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)

        self.assertEqual(results.count('blocked'), 4, results)
        self.assertTrue(ItemLink.objects.filter(pk=link_pk).exists())
