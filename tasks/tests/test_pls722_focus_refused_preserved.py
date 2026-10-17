"""PLS-722: the focus guard refuses without destroying the author's body.

The guard's purpose (PLS-117) is preserved verbatim — a comment on a
focus-gated item is still refused, and no work is started. What changes is that
the refused content is kept as a mechanical `FOCUS_REFUSED` record instead of
being discarded, so a diagnosis finished against a gated task has somewhere to
go and the stall itself is documented.

The load-bearing half of this file is the *consumer* isolation. A preserved
record writes rows that three live consumers read as "the agent worked this
item"; if it were counted, the refusal would manufacture the very appearance of
progress the guard exists to prevent. The worst case is
`_org_health_recent_active_team_activity`, which suppresses a whole-team outage
on a threshold of ONE event and counts BOTH Comment and ItemActivity rows — so
an uncounted refusal is the difference between seeing a real outage and hiding
it.
"""

from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from tasks.models import (
    AgentAvailability,
    Comment,
    ExternalMcpClient,
    ExternalMcpToken,
    Item,
    ItemActivity,
    ItemLink,
    Project,
    ProjectMembership,
)
from tasks.services.workflow_service import (
    WorkflowService,
    WorkflowValidationError,
)

User = get_user_model()


class FocusRefusedCommentPreservedTests(APITestCase):
    def setUp(self):
        self.client = APIClient()
        self.agent = User.objects.create_user(
            username='pls722-agent',
            email='pls722-agent@shizuha.com',
            password='x',
            is_staff=False,
            is_superuser=False,
        )
        self.agent.organization_memberships = {1: 'member'}
        self.client.force_authenticate(user=self.agent)
        self.project = Project.objects.create(
            name='PLS-722 project', key='P722', org_id=1,
        )
        ProjectMembership.objects.create(
            project=self.project,
            user_id=self.agent.id,
            user_email=self.agent.email,
            role='member',
        )

        # `_org_health_recent_active_team_activity` returns None outright unless
        # a member has a durable AgentAvailability(active=True) row. Without
        # this the activity assertions below pass because the counter never
        # runs — the exclusion and a broken counter look identical. The inverse
        # control at the end of this file is what surfaced that.
        AgentAvailability.objects.update_or_create(
            email=self.agent.email,
            defaults={'active': True, 'status': 'active', 'updated_by': 'test'},
        )

        with patch(
            'tasks.services.admin_service.admin_service.is_org_member',
            return_value=True,
        ):
            self.root = self._item('owned blocker root', priority='urgent')
            self.descendant = self._item(
                'blocked descendant', priority='urgent',
            )
            self.gated = self._item(
                'lower-impact owned task', priority='normal',
            )
        self._link_without_auto_block(self.root, self.descendant)
        self.descendant.status = 'blocked'
        self.descendant.save(update_fields=['status'])

    def _item(self, title, status='open', priority='normal'):
        return Item.objects.create(
            title=title,
            mode='task',
            project=self.project,
            status=status,
            priority=priority,
            workflow_name='autonomous-dev',
            assignee_id=self.agent.id,
            assignee_email=self.agent.email,
        )

    def _link_without_auto_block(self, blocker, dependent):
        link = ItemLink(
            source_item=blocker,
            target_item=dependent,
            link_type='blocks',
            created_by_id=self.agent.id,
            created_by_email=self.agent.email,
        )
        link._skip_auto_block = True
        link.save()
        return link

    def _assert_guard_armed(self):
        """A fixture that stopped arming the guard would pass everything below."""
        self.assertIsNotNone(
            WorkflowService._higher_impact_owned_blocker(
                self.gated, self.agent.email,
            ),
            'fixture no longer arms the focus guard; these assertions would be '
            'vacuous',
        )

    def _refuse(self, content='Diagnosis complete: root cause is X.'):
        return self.client.post(
            '/api/comments/',
            {'item': self.gated.id, 'content': content},
            format='json',
        )

    # ---- the guard still refuses (PLS-117's contract, unchanged) ----------

    def test_comment_on_gated_item_is_still_refused(self):
        self._assert_guard_armed()
        response = self._refuse()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['code'], 'BLOCKER_FOCUS_REQUIRED')

    def test_operator_comment_on_gated_item_is_allowed(self):
        """PLS-846 follow-up: the comment-path focus guard is an AGENT-queue
        mechanism. A human operator has no stale runtime to correct and records
        decisions as the deliverable, so their comment on a focus-gated owned
        item must NOT be frozen on an unrelated owned blocker root.
        """
        from tasks.services.workflow_service import WorkflowService
        self.assertTrue(
            WorkflowService._is_human_account('operator@example.test'),
            'fixture depends on the operator email being a registered human '
            'account',
        )

        operator = User.objects.create_user(
            username='pls722-operator',
            email='operator@example.test',
            password='x',
            is_staff=True,
            is_superuser=True,
        )
        # The operator owns the gated item too (same identity as the root's
        # owner), reproducing the live decision-surface freeze: the operator
        # owns BOTH the blocker root and the decision task.
        for owned in (self.root, self.descendant, self.gated):
            owned.assignee_id = operator.id
            owned.assignee_email = operator.email
            with patch(
                'tasks.services.admin_service.admin_service.is_org_member',
                return_value=True,
            ):
                owned.save(update_fields=['assignee_id', 'assignee_email'])

        # The guard would be armed for an agent (root still unstarted + owned).
        self.assertIsNotNone(
            WorkflowService._higher_impact_owned_blocker(
                self.gated, operator.email,
            ),
            'fixture must still arm the underlying guard for a same-identity '
            'owner; the human exemption is what makes the difference',
        )

        operator_client = APIClient()
        operator_client.force_authenticate(user=operator)
        response = operator_client.post(
            '/api/comments/',
            {'item': self.gated.pk, 'content': 'OPERATOR DECISION: approve as reviewed'},
            format='json',
        )
        self.assertEqual(
            response.status_code, 201,
            msg=(
                'a human operator recording a decision on an owned item must '
                f'not be focus-frozen on an unrelated root. got {response.content!r}'
            ),
        )
        self.assertFalse(
            Comment.objects.filter(
                item=self.gated, origin_kind=Comment.ORIGIN_FOCUS_REFUSED,
            ).exists(),
            'no focus-refusal record may be written for a human comment',
        )

    def test_agent_comment_on_gated_item_is_still_refused_after_operator_exemption(self):
        """Inverse control for the human exemption: the SAME gated item is
        still refused for a non-human agent. The exemption must not leak the
        comment guard open for agents.
        """
        self._assert_guard_armed()
        # The base fixture agent owns the gated item and is refused.
        response = self._refuse()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['code'], 'BLOCKER_FOCUS_REQUIRED')

    def test_operator_transition_on_gated_item_is_allowed(self):
        """PLS-846 parity for the transition path: the queue focus guard is an
        AGENT-queue mechanism. A human operator closing/deciding work they own
        must NOT be frozen on an unrelated owned blocker root — same rationale
        as the comment-path exemption.
        """
        from tasks.services.workflow_service import WorkflowService
        self.assertTrue(
            WorkflowService._is_human_account('operator@example.test'),
            'fixture depends on the operator email being a registered human '
            'account',
        )

        operator = User.objects.create_user(
            username='pls722-operator-trans',
            email='operator@example.test',
            password='x',
            is_staff=True,
            is_superuser=True,
        )
        for owned in (self.root, self.descendant, self.gated):
            owned.assignee_id = operator.id
            owned.assignee_email = operator.email
            with patch(
                'tasks.services.admin_service.admin_service.is_org_member',
                return_value=True,
            ):
                owned.save(update_fields=['assignee_id', 'assignee_email'])

        # The guard is still armed for the operator's identity (root unstarted).
        self.assertIsNotNone(
            WorkflowService._higher_impact_owned_blocker(
                self.gated, operator.email,
            ),
            'fixture must still arm the underlying guard for a same-identity '
            'owner; the human exemption is what makes the difference',
        )

        operator_client = APIClient()
        operator_client.force_authenticate(user=operator)
        response = operator_client.post(
            f'/api/items/{self.gated.pk}/execute-transition/',
            {'to_status': 'in_progress'},
            format='json',
        )
        self.assertNotEqual(
            response.status_code, 400,
            msg=(
                'a human operator closing/deciding an owned item must not be '
                f'focus-frozen on an unrelated root. got {response.content!r}'
            ),
        )
        self.gated.refresh_from_db()
        self.assertEqual(self.gated.status, 'in_progress')

    def test_agent_transition_on_gated_item_is_still_refused(self):
        """Inverse control for the transition exemption: a non-human agent is
        still focus-refused when starting work on a gated owned item.
        """
        self._assert_guard_armed()
        response = self.client.post(
            f'/api/items/{self.gated.pk}/execute-transition/',
            {'to_status': 'in_progress'},
            format='json',
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['code'], 'BLOCKER_FOCUS_REQUIRED')

    def test_root_already_in_progress_releases_unrelated_comments(self):
        """PLS-846: an elected root that entered in_progress no longer freezes
        unrelated comment/routing mutations on other owned items.

        Production-order regression fixture (from the ticket's acceptance):
        elected root → start exact root → root is in_progress → unrelated
        comment is allowed.
        """
        self._assert_guard_armed()

        # Start the elected root — it is now the active WIP occupant.
        self.root.status = 'in_progress'
        self.root.save(update_fields=['status'])

        # The same agent comments on an unrelated owned item.
        with patch(
            'tasks.services.admin_service.admin_service.is_org_member',
            return_value=True,
        ):
            unrelated = self._item(
                'unrelated-owned-item', priority='normal',
            )
        response = self.client.post(
            '/api/comments/',
            {'item': unrelated.pk, 'content': 'routing note, unrelated to root'},
            format='json',
        )
        self.assertEqual(
            response.status_code, 201,
            msg=(
                'comment on unrelated owned item must be allowed once the '
                'elected root is in_progress — otherwise the queue freezes for '
                f'the whole root lifetime (PLS-846). got {response.content!r}'
            ),
        )

    def test_root_already_in_progress_releases_unrelated_transition(self):
        """PLS-846: a routing correction (Start Progress / team-routing) on an
        unrelated owned item must not be frozen once the elected root is active.
        """
        self._assert_guard_armed()
        self.root.status = 'in_progress'
        self.root.save(update_fields=['status'])

        with patch(
            'tasks.services.admin_service.admin_service.is_org_member',
            return_value=True,
        ):
            unrelated = self._item(
                'unrelated-owned-work', status='open', priority='normal',
            )
        # Start Progress on an unrelated item must pass the focus guard (only
        # WIP-capacity policy governs new parallel work — AC-2). If the
        # transition succeeds or fails for a NON-focus reason, the fix holds;
        # only a BLOCKER_FOCUS_REQUIRED error would mean the queue is still
        # frozen by the active root.
        try:
            WorkflowService.validate_transition(
                unrelated, 'in_progress', user=None, screen_data={},
            )
        except WorkflowValidationError as exc:
            self.assertNotEqual(
                exc.code, 'BLOCKER_FOCUS_REQUIRED',
                'routing/start on unrelated owned item must not be frozen once '
                'the elected root is in_progress (PLS-846)',
            )

    def test_unstarted_root_still_focus_pins_unrelated_comment(self):
        """PLS-846 inverse control: while the elected root is genuinely
        UNSTARTED (open/todo), focus enforcement is preserved — the unrelated
        comment is still refused. This is the AC that must not regress.
        """
        self._assert_guard_armed()
        with patch(
            'tasks.services.admin_service.admin_service.is_org_member',
            return_value=True,
        ):
            unrelated = self._item(
                'unrelated-owned-item', priority='normal',
            )
        response = self.client.post(
            '/api/comments/',
            {'item': unrelated.pk, 'content': 'should still be gated'},
            format='json',
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['code'], 'BLOCKER_FOCUS_REQUIRED')

    def test_refusal_tells_the_author_where_the_body_went(self):
        """@hiro's condition: 'persisted' must mean READABLE, not just written.

        A preserved record the author never learns about is the same loss in a
        different place — they would still believe the diagnosis evaporated and
        would still have to redo or re-route it by hand.
        """
        self._assert_guard_armed()
        response = self._refuse()

        self.assertEqual(response.status_code, 400)
        preserved = Comment.objects.get(
            item=self.gated, origin_kind=Comment.ORIGIN_FOCUS_REFUSED,
        )
        # DRF coerces values in a ValidationError payload to ErrorDetail
        # strings, so compare as strings rather than to the raw int pk.
        self.assertEqual(
            str(response.data.get('preserved_comment_id')), str(preserved.pk),
            'the refusal must point the author at the preserved record',
        )
        self.assertIn('NOT lost', str(response.data.get('preserved_note', '')))

    def test_transition_on_gated_item_is_still_refused(self):
        self._assert_guard_armed()
        with self.assertRaises(WorkflowValidationError) as caught:
            WorkflowService.validate_transition(
                self.gated, 'in_progress', user=None, screen_data={},
            )
        self.assertEqual(caught.exception.code, 'BLOCKER_FOCUS_REQUIRED')

    # ---- but the body survives -------------------------------------------

    def test_refused_body_is_preserved_with_provenance(self):
        self._assert_guard_armed()
        self._refuse('Root cause is X; handing the cluster half to ichi.')

        preserved = Comment.objects.get(
            item=self.gated, origin_kind=Comment.ORIGIN_FOCUS_REFUSED,
        )
        # The factory renders content from the template, so the body is carried
        # verbatim INSIDE the rendered record rather than being the whole of it.
        self.assertIn(
            'Root cause is X; handing the cluster half to ichi.',
            preserved.content,
        )
        self.assertEqual(preserved.origin_event, 'focus_guard.comment_refused')
        self.assertEqual(preserved.caused_by_email_snapshot, self.agent.email)
        # Mechanical authorship is what keeps this out of the activity
        # counters — it must NOT be attributed to the refused agent.
        self.assertEqual(preserved.author_email, 'workflow@pulse')

        payload = preserved.origin_payload or {}
        self.assertEqual(
            payload.get('refused_body'),
            'Root cause is X; handing the cluster half to ichi.',
        )
        self.assertEqual(payload.get('refused_author'), self.agent.email)
        self.assertEqual(
            payload.get('required_task_key'),
            self.root.item_key or str(self.root.pk),
        )
        # Status at refusal makes a degraded-branch refusal auditable after the
        # fact instead of depending on someone writing it down.
        self.assertEqual(payload.get('refused_item_status'), self.gated.status)

    def test_repeated_refusal_does_not_accumulate_duplicates(self):
        """A retrying client must not pile up preservation rows."""
        self._assert_guard_armed()
        for _ in range(3):
            self._refuse('same body, retried')

        self.assertEqual(
            Comment.objects.filter(
                item=self.gated, origin_kind=Comment.ORIGIN_FOCUS_REFUSED,
            ).count(),
            1,
        )

    def test_aliases_dedupe_one_refusal_across_drf_and_mcp(self):
        """The two write doors share one identity-folded preservation key."""
        self._assert_guard_armed()
        body = 'same diagnosis through both comment doors'

        native = self._refuse(body)
        self.assertEqual(native.status_code, 400, native.content)
        first = Comment.objects.get(
            item=self.gated, origin_kind=Comment.ORIGIN_FOCUS_REFUSED,
        )

        raw_token = 'pls722-cross-surface-alias'
        mcp_client = ExternalMcpClient.objects.create(
            org_id=self.project.org_id,
            name='PLS-722 alias client',
            owner_user_id=self.agent.id,
            owner_email='pls722-agent@agents.shizuha.io',
            allowed_project_ids=[self.project.id],
            scopes=['task.read', 'comment.write'],
            rate_limit_policy={'per_minute': 100},
        )
        ExternalMcpToken.objects.create(
            client=mcp_client,
            token_hash=ExternalMcpToken.hash_token(raw_token),
            scopes=['task.read', 'comment.write'],
            expires_at=timezone.now() + timedelta(days=1),
        )
        mcp = APIClient()
        mcp.credentials(HTTP_AUTHORIZATION=f'Bearer {raw_token}')
        refused = mcp.post(
            '/api/external-mcp/call/',
            {
                'tool': 'pulse_add_comment',
                'arguments': {
                    'task_id': str(self.gated.pk),
                    'content': body,
                },
            },
            format='json',
            HTTP_IDEMPOTENCY_KEY='pls722-cross-surface-alias',
        )
        self.assertEqual(refused.status_code, 409, refused.content)
        self.assertEqual(
            str(refused.json().get('preserved_comment_id')), str(first.pk),
        )

        preserved = Comment.objects.filter(
            item=self.gated, origin_kind=Comment.ORIGIN_FOCUS_REFUSED,
        )
        self.assertEqual(preserved.count(), 1)
        self.assertEqual(preserved.get().pk, first.pk)
        self.assertEqual(
            (preserved.get().origin_payload or {}).get('refused_author'),
            self.agent.email,
        )

        discovery = mcp.get('/api/external-mcp/tools/')
        self.assertEqual(discovery.status_code, 200, discovery.content)
        tool_names = {tool['name'] for tool in discovery.json()['tools']}
        self.assertNotIn('pulse_update_comment', tool_names)
        self.assertNotIn('pulse_delete_comment', tool_names)

    def test_preserved_record_is_immutable_even_to_staff(self):
        self._assert_guard_armed()
        self._refuse()
        preserved = Comment.objects.get(
            item=self.gated, origin_kind=Comment.ORIGIN_FOCUS_REFUSED,
        )
        original_content = preserved.content
        staff = User.objects.create_user(
            username='pls722-staff',
            email='pls722-staff@shizuha.com',
            password='x',
            is_staff=True,
        )
        staff_client = APIClient()
        staff_client.force_authenticate(user=staff)

        for verb, url in (
            ('patch', f'/api/comments/{preserved.id}/'),
            ('delete', f'/api/comments/{preserved.id}/'),
        ):
            with self.subTest(verb=verb):
                call = getattr(staff_client, verb)
                response = (
                    call(url, {'content': 'rewritten'}, format='json')
                    if verb == 'patch' else call(url)
                )
                self.assertEqual(response.status_code, 400, response.content)
                preserved.refresh_from_db()
                self.assertEqual(preserved.content, original_content)

    # ---- consumer isolation: the half that makes this safe ---------------

    def test_refusal_does_not_write_a_commented_activity(self):
        """`commented` is whitelisted by the WIP scan and flatline detector."""
        self._assert_guard_armed()
        self._refuse()

        self.assertFalse(
            ItemActivity.objects.filter(
                item=self.gated, action='commented',
            ).exists(),
            'a refusal must not register as work on the item it refused',
        )
        self.assertTrue(
            ItemActivity.objects.filter(
                item=self.gated, action='comment_refused',
            ).exists(),
        )

    def test_refusal_is_not_counted_as_team_activity(self):
        """The outage suppressor fires on ONE event and counts both tables."""
        from tasks.tasks import _org_health_recent_active_team_activity

        self._assert_guard_armed()
        members = [self.agent.email]

        # Measure the DELTA. Fixture setup writes its own activity rows, so an
        # absolute assertion here would be testing setUp, not the refusal.
        before = _org_health_recent_active_team_activity(members, timezone.now())
        before_events = (before or {}).get('events', 0)

        self._refuse()

        after = _org_health_recent_active_team_activity(members, timezone.now())
        after_events = (after or {}).get('events', 0)

        self.assertEqual(
            after_events, before_events,
            'a focus refusal must not corroborate that the team is producing '
            'work — it is evidence the agent was stopped. This counter '
            'suppresses a whole-team outage on a threshold of ONE event, so a '
            f'counted refusal hides a real outage. before={before} after={after}',
        )

    def test_a_real_comment_still_counts_as_team_activity(self):
        """The inverse control — without it the test above passes vacuously."""
        from tasks.tasks import _org_health_recent_active_team_activity

        ungated = Item.objects.create(
            title='someone else owns this',
            mode='task',
            status='open',
            priority='normal',
            workflow_name='autonomous-dev',
            assignee_id=self.agent.id + 1,
            assignee_email='other@shizuha.com',
        )
        Comment.objects.create(
            item=ungated,
            content='real work',
            author_id=self.agent.id,
            author_email=self.agent.email,
            author_name='agent',
            origin_kind=Comment.ORIGIN_USER_AUTHORED,
        )

        result = _org_health_recent_active_team_activity(
            [self.agent.email], timezone.now(),
        )
        self.assertIsNotNone(
            result,
            'genuine user-authored work must still corroborate team liveness, '
            'otherwise the exclusion above is indistinguishable from the '
            'counter being broken',
        )
        self.assertGreaterEqual(result['events'], 1)
