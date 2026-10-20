"""PLS-258: capability-aware routing skips healthy but incapable seats."""
from types import SimpleNamespace
from unittest.mock import patch
import os

from django.core.cache import cache
from django.test import TestCase, override_settings

from tasks.models import Comment, Item, Project, TeamRoster
from tasks.services.workflow_service import WorkflowService


class _FakeUserService:
    users = {
        'sandbox@shizuha.com': {'id': 201, 'email': 'sandbox@shizuha.com', 'username': 'sandbox'},
        'ops@shizuha.com': {'id': 202, 'email': 'ops@shizuha.com', 'username': 'ops'},
        'kai@shizuha.com': {'id': 301, 'email': 'kai@shizuha.com', 'username': 'kai'},
        'ichi@shizuha.com': {'id': 302, 'email': 'ichi@shizuha.com', 'username': 'ichi'},
    }

    def get_user_by_email(self, email):
        return self.users.get((email or '').strip().lower())

    def get_user_by_id(self, user_id):
        for user in self.users.values():
            if user.get('id') == int(user_id):
                return user
        return None


@override_settings(PULSE_HEALTH_EXPORTER_URL='disabled', PULSE_API_HEALTH_URL='disabled')
class PLS258CapabilityRoutingTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name='Pulse', key='PLS258', org_id=1, access_level='open')
        TeamRoster.objects.update_or_create(
            team_name='engineering',
            defaults={'tiers': [['sandbox@shizuha.com', 'ops@shizuha.com']]},
        )
        WorkflowService._invalidate_team_roster_caches()

    def tearDown(self):
        WorkflowService._invalidate_team_roster_caches()
        WorkflowService._db_avail_cache = None
        WorkflowService._health_avail_cache = None
        WorkflowService._disabled_emails_cache = None

    def _item(self, **kwargs):
        data = dict(
            project=self.project,
            title='cluster fix',
            mode='task',
            item_type='task.bug',
            workflow_name='autonomous-bug',
            status='open',
            workflow_status='open',
            assignment_group='engineering',
            labels={'needs:kubeconfig': True},
        )
        data.update(kwargs)
        return Item.objects.create(**data)

    @patch('tasks.user_service.user_service', _FakeUserService())
    @patch('tasks.services.workflow_service.WorkflowService._is_agent_available_for_assignment', return_value=True)
    def test_resolver_seats_an_available_member_without_the_capability(self, _avail):
        item = self._item()

        def caps(email):
            return {'kubeconfig'} if (email or '').startswith('ops@') else set()

        with patch.object(WorkflowService, '_agent_effective_capabilities', side_effect=caps):
            uid, email = WorkflowService._resolve_team_assignee('engineering', item=item)

        # Least-loaded available member of the team, not the one who happens
        # to hold kubeconfig.
        self.assertEqual(uid, 201)
        self.assertEqual(email, 'sandbox@shizuha.com')

    @patch('tasks.user_service.user_service', _FakeUserService())
    @patch('tasks.services.workflow_service.WorkflowService._is_agent_available_for_assignment', return_value=True)
    def test_resolver_seats_when_no_member_has_the_capability(self, _avail):
        item = self._item()

        with patch.object(WorkflowService, '_notify_capability_routing_hold', return_value=True), \
             patch.object(WorkflowService, '_agent_effective_capabilities', return_value=set()):
            uid, email = WorkflowService._resolve_team_assignee('engineering', item=item)

        self.assertEqual(uid, 201)
        self.assertEqual(email, 'sandbox@shizuha.com')
        self.assertFalse(Comment.objects.filter(
            item=item,
            content__contains='PLS-258 capability routing hold',
        ).exists())


    @patch('tasks.services.notifications.UserNotificationService.notify_user')
    @patch('requests.post')
    @patch('tasks.user_service.user_service', _FakeUserService())
    @patch('tasks.services.workflow_service.WorkflowService._is_agent_available_for_assignment', return_value=True)
    def test_resolver_hold_notifies_team_and_substrate_owner_once(self, _avail, post, notify_user):
        item = self._item()
        post.return_value = SimpleNamespace(status_code=201)

        def lead(team, **_context):
            return {
                'engineering': 'kai@shizuha.com',
                'devops': 'ichi@shizuha.com',
                'platform': None,
            }.get(team)

        with patch.object(WorkflowService, '_resolve_team_lead_email', side_effect=lead), \
             patch.object(WorkflowService, '_agent_effective_capabilities', return_value=set()):
            uid, email = WorkflowService._resolve_team_assignee('engineering', item=item)

        self.assertEqual(email, 'sandbox@shizuha.com')
        self.assertEqual(uid, 201)
        self.assertEqual(post.call_count, 0)
        self.assertEqual(notify_user.call_count, 0)

    def _review_transition_item(self, **overrides):
        values = dict(
            project=self.project,
            item_key=f'PLS258-R{Item.objects.count() + 1}',
            title='SCLI-324 shaped credential-broker review',
            mode='task',
            item_type='task.bug',
            workflow_name='autonomous-bug',
            status='in_review',
            workflow_status='in_review',
            priority='high',
            assignment_group='engineering',
            assignee_id=201,
            assignee_email='sandbox@shizuha.com',
            created_by_id=901,
            created_by_email='author@shizuha.com',
            labels={'no-pr': True, 'needs:credential-broker': True},
        )
        values.update(overrides)
        return Item.objects.create(**values)

    @override_settings(PULSE_ROUTABILITY_ENFORCE='enforce')
    @patch('tasks.services.notifications.UserNotificationService.notify_user')
    @patch('requests.post')
    @patch('tasks.user_service.user_service', _FakeUserService())
    @patch('tasks.services.workflow_service.WorkflowService._member_can_execute_from_status', return_value=True)
    @patch('tasks.services.workflow_service.WorkflowService._is_agent_available_for_assignment', return_value=True)
    def test_high_review_no_compatible_seat_escalates_same_cycle_once(
        self, _avail, _can_execute, post, notify_user,
    ):
        """PLS-461 recorded SCLI-324 recurrence.

        The assign-team event itself clears the stale implementer, preserves the
        task on the review group queue, and emits one deduplicated durable lead
        notification.  No 48h sweep or incompatible fallback is involved.
        """
        TeamRoster.objects.update_or_create(
            team_name='review', defaults={'tiers': [['kai@shizuha.com']]},
        )
        WorkflowService._invalidate_team_roster_caches()
        item = self._review_transition_item()
        transition = SimpleNamespace(
            post_functions=[{'type': 'assign_team', 'value': 'review'}],
        )
        post.return_value = SimpleNamespace(status_code=201)

        with patch.object(
            WorkflowService, '_resolve_team_lead_email',
            side_effect=lambda team, **_context: 'kai@shizuha.com' if team == 'review' else None,
        ), patch.object(
            WorkflowService, '_agent_effective_capabilities', return_value=set(),
        ), self.captureOnCommitCallbacks(execute=True):
            WorkflowService.execute_post_functions(transition, item)
            WorkflowService._ensure_assigned_if_working(item)
            # Repeated resolver/backstop attempts in the same event remain one alert.
            WorkflowService._record_review_seat_starvation(
                item,
                'review',
                required_caps={'credential-broker'},
                reasons=['seat_incapable'],
                source='duplicate-probe',
            )

        item.refresh_from_db()
        self.assertEqual(item.status, 'in_review')
        self.assertEqual(item.assignment_group, 'review')
        self.assertEqual(item.assignee_id, 301)
        self.assertEqual(item.assignee_email, 'kai@shizuha.com')
        comments = Comment.objects.filter(
            item=item,
            author_email='workflow@pulse',
            content__contains='PLS-461 review-seat starvation',
        )
        self.assertEqual(comments.count(), 1)
        self.assertIn(item.item_key, comments.first().content)
        self.assertIn('`credential-broker`', comments.first().content)
        starvation_posts = [
            call for call in post.call_args_list
            if '[Review Seat Starvation]' in str(
                (call.kwargs.get('json') or {}).get('content', '')
            )
        ]
        self.assertEqual(len(starvation_posts), 1)
        message = starvation_posts[0].kwargs['json']['content']
        self.assertIn('[Review Seat Starvation]', message)
        self.assertIn(item.item_key, message)
        self.assertIn('`credential-broker`', message)

    @override_settings(PULSE_ROUTABILITY_ENFORCE='enforce')
    @patch('tasks.services.notifications.UserNotificationService.notify_user')
    @patch('requests.post')
    @patch('tasks.user_service.user_service', _FakeUserService())
    @patch('tasks.services.workflow_service.WorkflowService._member_can_execute_from_status', return_value=True)
    @patch('tasks.services.workflow_service.WorkflowService._is_agent_available_for_assignment', return_value=True)
    def test_high_review_compatible_seat_keeps_normal_routing(
        self, _avail, _can_execute, post, notify_user,
    ):
        TeamRoster.objects.update_or_create(
            team_name='review', defaults={'tiers': [['kai@shizuha.com']]},
        )
        WorkflowService._invalidate_team_roster_caches()
        item = self._review_transition_item(item_key='PLS258-R-COMPATIBLE')
        transition = SimpleNamespace(
            post_functions=[{'type': 'assign_team', 'value': 'review'}],
        )

        with patch.object(
            WorkflowService, '_agent_effective_capabilities',
            return_value={'credential-broker'},
        ):
            WorkflowService.execute_post_functions(transition, item)

        item.refresh_from_db()
        self.assertEqual(item.assignment_group, 'review')
        self.assertEqual(item.assignee_id, 301)
        self.assertEqual(item.assignee_email, 'kai@shizuha.com')
        self.assertFalse(Comment.objects.filter(
            item=item,
            content__contains='PLS-461 review-seat starvation',
        ).exists())
        self.assertFalse(any(
            '[Review Seat Starvation]' in str(call.kwargs.get('json', {}).get('content', ''))
            for call in post.call_args_list
        ))
        self.assertFalse(any(
            'Review-seat starvation' in str(call.args)
            for call in notify_user.call_args_list
        ))

    def test_required_capabilities_from_structured_declarations_only(self):
        # Structured JSON-label dict declarations remain authoritative.
        item = SimpleNamespace(labels={'needs': ['kubeconfig', 'host_exec']}, tags='')

        self.assertEqual(
            WorkflowService._task_required_capabilities(item),
            {'kubeconfig', 'host-exec'},
        )

    def test_plain_labels_and_tags_never_declare_capabilities(self):
        """Operator simplification 2026-09-16: free-form `needs-*`/`needs:`
        prefix parsing on plain string labels/tags is deleted.

        Flag labels from unrelated features collided with the requirement
        syntax and stranded unassignable items: the PLS-256 DoR quality flag
        `needs-detail` (35 stranded) and the MCP triage flag
        `needs-cos-triage` (33 stranded) were both parsed as hard capability
        requirements no seat holds. Plain labels/tags are descriptive; team
        routing + roster capabilities decide ordinary assignment. Substrate
        caps are declared via structured fields (labels-dict `needs` keys or
        metadata) and still enforced with signed per-seat evidence.
        """
        dor_flagged = SimpleNamespace(
            labels=['hive', 'needs-detail', 'auto-rollback', 'no-pr'], tags='')
        self.assertEqual(WorkflowService._task_required_capabilities(dor_flagged), set())

        triage_flagged = SimpleNamespace(labels=['needs-cos-triage'], tags='')
        self.assertEqual(WorkflowService._task_required_capabilities(triage_flagged), set())

        needs_true_garbage = SimpleNamespace(labels={'needs': True}, tags='')
        self.assertEqual(WorkflowService._task_required_capabilities(needs_true_garbage), set())

        tag_prefixed = SimpleNamespace(labels=[], tags='needs:kubeconfig,other')
        self.assertEqual(WorkflowService._task_required_capabilities(tag_prefixed), set())

    def test_free_text_and_subject_inference_is_not_a_capability_requirement(self):
        """ADR-0016 v7 Routing-input safety rule (Aoi arbitration 2026-07-17).

        DESCRIPTIVE prose — bare subject/taxonomy labels (even the canonical slug
        `kubeconfig`), generic k8s nouns, evidence, and PR/rebase text — is NOT a
        capability declaration. Only explicit `needs:*`/workflow-stage metadata is
        authority. The ONE bounded migration fallback (PLS-459) still synthesizes a
        provisional `kubeconfig` from an affirmative, current-stage, executable
        Kubernetes ACTION; NO OTHER capability is inferred from prose.
        """
        # Descriptive k8s/secret subject text + bare labels (no executable
        # verb coupled to a k8s resource) -> NO inferred cap.
        descriptive = SimpleNamespace(
            labels=['kubernetes', 'kubeconfig'],
            tags='auth,security-sensitive',
            assignment_group='devops',
            title='search-read-secret / search-ingest-secret subject for the k8s search deploy',
            description='Tracks the READ_TOKEN / INGEST_TOKEN Kubernetes secrets and the pending pod rollout for the search deployment.',
        )
        self.assertEqual(WorkflowService._task_required_capabilities(descriptive), set())

        # PR/rebase prose is not a Kubernetes action -> no cap, and no other
        # capability may be inferred from prose (code-substrate/sara2574-push
        # come only from needs:*).
        orig73_like = SimpleNamespace(
            labels=['no-pr'],
            tags='agent-runtime,user-facing',
            assignment_group='review',
            title='Rebase 18 conflicting PRs for Origin cutover',
            description='Conflict-free all open pull requests and push the branch updates.',
        )
        self.assertEqual(WorkflowService._task_required_capabilities(orig73_like), set())

        # Affirmative executable Kubernetes ACTION -> provisional kubeconfig
        # (the single bounded PLS-459 migration fallback).
        executable = SimpleNamespace(
            labels=[],
            tags='',
            assignment_group='devops',
            title='Provision search secrets for the k8s deploy',
            description='Run kubectl apply to populate the Kubernetes secrets and roll out the deployment.',
        )
        self.assertEqual(WorkflowService._task_required_capabilities(executable), {'kubeconfig'})

        # Explicit needs:* declarations remain authoritative in every lane.
        explicit = SimpleNamespace(
            labels={'needs:code-substrate': True, 'needs:sara2574-push': True},
            tags='',
            assignment_group='review',
            title='Rebase PRs',
            description='',
        )
        self.assertEqual(
            WorkflowService._task_required_capabilities(explicit),
            {'code-substrate', 'sara2574-push'},
        )

    def test_review_lane_does_not_inherit_devops_capability_from_subject_text(self):
        """Reviewing infra code does not itself require cluster credentials.

        The free-text kubeconfig heuristic is an execution requirement for the
        DevOps lane. Applying it after a workflow handoff to Review stranded any
        review whose original title/description mentioned a pod, namespace, or
        cluster, even though every review seat was otherwise healthy. Explicit
        ``needs:kubeconfig`` declarations remain authoritative in every lane.
        """
        review = SimpleNamespace(
            labels=['hive', 'frontend', 'kubernetes'],
            tags='agent-runtime,user-facing',
            assignment_group='review',
            title='Review stale runtime metadata for a live k8s pod',
            description='The Kubernetes deployment and cluster feed disagree.',
        )
        self.assertNotIn(
            'kubeconfig', WorkflowService._task_required_capabilities(review)
        )

        explicitly_gated = SimpleNamespace(
            labels={'needs:kubeconfig': True},
            tags='',
            assignment_group='review',
            title='Review live cluster behavior',
            description='',
        )
        self.assertIn(
            'kubeconfig',
            WorkflowService._task_required_capabilities(explicitly_gated),
        )

    def test_architecture_subject_labels_do_not_become_runtime_requirements(self):
        """PLAT-4732: subject taxonomy must not suppress concrete ownership.

        Architecture/code work may discuss the kubeconfig routing capability
        without needing live cluster credentials to edit and test the resolver.
        A real privileged execution requirement remains authoritative only when
        the task or workflow declares it explicitly.
        """
        architecture_code_work = SimpleNamespace(
            labels=['pulse-routing', 'capability-aware', 'kubeconfig'],
            tags='agent-runtime,kubernetes,security-sensitive',
            assignment_group='architecture',
            title='Correct capability-aware ownership resolution',
            description='Change resolver code and add regression tests.',
        )
        self.assertNotIn(
            'kubeconfig',
            WorkflowService._task_required_capabilities(architecture_code_work),
        )

        explicitly_gated = SimpleNamespace(
            labels={'needs:kubeconfig': True},
            tags='agent-runtime',
            assignment_group='architecture',
            title='Verify resolver behavior against the live cluster',
            description='',
        )
        self.assertIn(
            'kubeconfig',
            WorkflowService._task_required_capabilities(explicitly_gated),
        )

    @patch.dict(os.environ, {
        'PULSE_AGENT_CAPABILITIES_JSON': (
            '{"ops@shizuha.com": {'
            '"default_credential_grant_scopes": ["github", "github:sara2574:push"], '
            '"identity_capabilities": ["code_substrate"], '
            '"sensitive_access_flags": {"credential_broker": true, "host_exec": false}'
            '}}'
        ),
    })
    def test_agent_effective_capabilities_reads_credential_identity_fields(self):
        caps = WorkflowService._agent_effective_capabilities('ops@shizuha.com')
        self.assertTrue({'github-token', 'sara2574-push', 'code-substrate', 'credential-broker'}.issubset(caps))
        self.assertNotIn('host-exec', caps)

    @patch.dict(os.environ, {
        'PULSE_AGENT_CAPABILITIES_JSON': '',
        'PULSE_HIVE_EFFECTIVE_CAPABILITIES_URL': '',
    })
    def test_team_conferred_capability_satisfies_without_per_agent_source(self):
        """PLAT-4223: substrate capabilities are TEAM-conferred (Hive baseline SoT).

        A seat on a conferring team (devops → kubeconfig/host-exec/credential-broker)
        satisfies the requirement with NO per-agent capability source configured —
        the gate resolves it from Pulse's own TeamRoster, not a per-agent map or a
        cross-service lookup. A seat on a non-conferring team fails closed.
        """
        TeamRoster.objects.update_or_create(
            team_name='devops',
            defaults={'tiers': [['ichi@shizuha.com']]},
        )
        WorkflowService._invalidate_team_roster_caches()
        # No PULSE_AGENT_CAPABILITIES_JSON / URL → per-agent source is None; the
        # devops team baseline alone must satisfy kubeconfig.
        self.assertEqual(
            WorkflowService._email_conferred_capabilities('ichi@shizuha.com'),
            {'kubeconfig', 'credential-broker', 'host-exec'},
        )
        self.assertTrue(
            WorkflowService._seat_satisfies_required_capability('ichi@shizuha.com', {'kubeconfig'}))
        self.assertTrue(
            WorkflowService._seat_satisfies_required_capability(
                'ichi@shizuha.com', {'kubeconfig', 'credential-broker'}))
        # engineering (setUp) confers no substrate capability → fail closed.
        self.assertFalse(
            WorkflowService._seat_satisfies_required_capability('sandbox@shizuha.com', {'kubeconfig'}))
        # No required capability → any seat passes.
        self.assertTrue(
            WorkflowService._seat_satisfies_required_capability('sandbox@shizuha.com', set()))

    @patch.dict(os.environ, {'PULSE_GITHUB_IDENTITIES_JSON': ''})
    def test_scm_identity_is_per_agent_local_part(self):
        """PLS-497 / ORIG-18: SCM identity is the agent local-part (Forgejo login),
        not the collapsed PLAT-4238 kai2574/sara2574 team seats. Distinct agents
        on write teams must not share one login or merge SoD starves."""
        TeamRoster.objects.update_or_create(team_name='merge', defaults={'tiers': [['m@shizuha.com']]})
        TeamRoster.objects.update_or_create(team_name='review', defaults={'tiers': [['r@shizuha.com']]})
        WorkflowService._invalidate_team_roster_caches()
        self.assertEqual(WorkflowService._agent_github_identity('m@shizuha.com'), 'm')
        self.assertEqual(WorkflowService._agent_github_identity('r@shizuha.com'), 'r')
        self.assertEqual(WorkflowService._agent_github_identity('sandbox@shizuha.com'), 'sandbox')
        self.assertEqual(WorkflowService._agent_scm_identity('aoi@shizuha.com'), 'aoi')

    @patch.dict(os.environ, {'PULSE_SCM_LEGACY_TEAM_SEATS': '1', 'PULSE_SCM_IDENTITIES_JSON': '', 'PULSE_GITHUB_IDENTITIES_JSON': ''})
    def test_scm_identity_legacy_team_seats_opt_in(self):
        """Legacy kai2574/sara2574 team seats remain available behind an opt-in flag."""
        TeamRoster.objects.update_or_create(team_name='merge', defaults={'tiers': [['m@shizuha.com']]})
        TeamRoster.objects.update_or_create(team_name='review', defaults={'tiers': [['r@shizuha.com']]})
        WorkflowService._invalidate_team_roster_caches()
        self.assertEqual(WorkflowService._agent_github_identity('m@shizuha.com'), 'kai2574')
        self.assertEqual(WorkflowService._agent_github_identity('r@shizuha.com'), 'sara2574')

    @patch.dict(os.environ, {
        'PULSE_API_HEALTH_URL': 'disabled',
        'PULSE_HEALTH_EXPORTER_URL': 'disabled',
        'PULSE_ROUTABILITY_ENFORCE': 'shadow',
        'PULSE_WIP_CAP_TEAMS': 'engineering',
    })
    @patch('tasks.user_service.user_service', _FakeUserService())
    @patch('tasks.services.workflow_service.WorkflowService._member_can_execute_from_status', return_value=True)
    @patch('tasks.services.workflow_service.WorkflowService._is_agent_available_for_assignment', return_value=True)
    def test_rebalance_load_balance_does_not_move_capability_item_to_incapable_peer(
        self, _avail, _can_execute,
    ):
        """Recorded repro for PLS-380.

        PLS-258 fixed direct resolver/capacity-pull helpers, but the rebalance
        sweep's load-balancing mover still used only status-role eligibility.
        A `needs:kubeconfig` item owned by the capable agent was moved to the
        lighter sandbox peer in shadow/default mode.
        """
        from tasks.tasks import sweep_rebalance_pods

        item = self._item(
            assignee_id=202,
            assignee_email='ops@shizuha.com',
            labels={'needs:kubeconfig': True},
            status='open',
            priority='high',
        )

        def caps(email):
            return {'kubeconfig'} if (email or '').startswith('ops@') else set()

        with patch.object(WorkflowService, '_agent_effective_capabilities', side_effect=caps), \
             patch.object(WorkflowService, '_record_capability_routing_hold', return_value=True) as record_hold:
            result = sweep_rebalance_pods.run(max_skew=0)

        item.refresh_from_db()
        self.assertEqual(result['moves'], 0)
        self.assertEqual(item.assignee_email, 'ops@shizuha.com')
        record_hold.assert_called_once()
        self.assertEqual(record_hold.call_args.kwargs.get('source'), 'rebalance-load-balance')

    @patch.dict(os.environ, {
        'PULSE_WIP_CAP': '5',
        'PULSE_INFLIGHT_CAP': '10',
        'PULSE_WIP_CAP_TEAMS': 'engineering',
    })
    @patch('tasks.user_service.user_service', _FakeUserService())
    @patch('tasks.services.workflow_service.WorkflowService._member_can_execute_from_status', return_value=True)
    @patch('tasks.services.workflow_service.WorkflowService._is_agent_available_for_assignment', return_value=True)
    @patch('tasks.services.workflow_service.WorkflowService._agent_active_load', return_value=0)
    @patch('tasks.services.workflow_service.WorkflowService._agent_workable_load', return_value=0)
    def test_capacity_pull_skips_agent_lacking_required_capability(
        self, _workable_load, _active_load, _avail, _can_execute,
    ):
        queued = self._item(assignee_id=None, assignee_email='', priority='high')

        def caps(email):
            return {'kubeconfig'} if (email or '').startswith('ops@') else set()

        with patch.object(WorkflowService, '_notify_capability_routing_hold', return_value=True), \
             patch.object(WorkflowService, '_agent_effective_capabilities', side_effect=caps):
            pulled = WorkflowService._pull_next_queued_for(201, 'sandbox@shizuha.com')
        self.assertIsNone(pulled)
        queued.refresh_from_db()
        self.assertIsNone(queued.assignee_id)
        self.assertIn(queued.assignee_email, (None, ''))
        self.assertTrue(Comment.objects.filter(
            item=queued,
            content__contains='PLS-258 capability routing hold',
            author_email='workflow@pulse',
        ).exists())

        with patch.object(WorkflowService, '_agent_effective_capabilities', side_effect=caps):
            pulled = WorkflowService._pull_next_queued_for(202, 'ops@shizuha.com')

        self.assertIsNotNone(pulled)
        queued.refresh_from_db()
        self.assertEqual(queued.assignee_id, 202)
        self.assertEqual(queued.assignee_email, 'ops@shizuha.com')

    @override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}})
    @patch('tasks.user_service.user_service', _FakeUserService())
    @patch('tasks.services.workflow_service.WorkflowService._is_agent_available_for_assignment', return_value=True)
    def test_hold_failure_counter_increments_on_exception(self, _avail):
        """PLS-365: cache counter bumps when _record_capability_routing_hold fails."""
        item = self._item()
        key = 'pulse:pls365_capability_routing_hold_failures'
        cache.delete(key)

        with patch(
            'tasks.services.mechanical_comments.MechanicalCommentFactory.create',
            side_effect=Exception("db error"),
        ):
            WorkflowService._record_capability_routing_hold(
                item, 'engineering', {'kubeconfig'}, source='team-resolver',
            )

        self.assertEqual(cache.get(key), 1)

    def test_db_team_capability_profile_confers_code_baseline(self):
        """Operator 2026-07-12: the DB TeamCapabilityProfile is the SoT the routing
        gate reads, so a code team that DECLARES code-substrate + sara2574-push
        satisfies code work from the team baseline alone — no per-agent source and
        no silent 'no capable seat' hold (the bug that stalled PLAT-4244 ~14h)."""
        from tasks.models import TeamCapabilityProfile
        TeamRoster.objects.update_or_create(
            team_name='architecture', defaults={'tiers': [['aoi@shizuha.com']]})
        # Full code baseline: repo + push identity + token (sara2574-push implies
        # github-token in the required-capability normalizer).
        TeamCapabilityProfile.objects.update_or_create(
            team_name='architecture',
            defaults={'capabilities': ['code-substrate', 'sara2574-push', 'github-token']})
        with patch.object(WorkflowService, '_agent_effective_capabilities', return_value=set()):
            self.assertTrue(WorkflowService._seat_satisfies_required_capability(
                'aoi@shizuha.com', {'code-substrate', 'sara2574-push'}))
            # a seat NOT on a conferring team is still correctly refused (fail-closed)
            self.assertFalse(WorkflowService._seat_satisfies_required_capability(
                'stranger@shizuha.com', {'code-substrate'}))


@override_settings(PULSE_HEALTH_EXPORTER_URL='disabled', PULSE_API_HEALTH_URL='disabled')
class PLAT4732SubstrateEnforcementTests(TestCase):
    """PLAT-4732 / ADR-0016 v5: signed-evidence-only gating for runtime-substrate caps.

    Regression suite for the tightening accepted in sora's Architecture
    Request-Changes and Hiro's contract triage. Behind PULSE_SUBSTRATE_CAP_ENFORCE,
    substrate caps (kubeconfig/host-exec/credential-broker/bench-runner) are
    satisfied ONLY by signed Hive effective-capabilities — team membership is never
    a fallback and unknown evidence fails closed. The flag defaults OFF so deploy is
    a no-op (legacy team-baseline behavior) until the signed-evidence source is wired
    and a shadow bake completes; the flip is coordinated with DevOps.
    """

    def setUp(self):
        self.project = Project.objects.create(
            name='Pulse', key='PLAT4732', org_id=1, access_level='open')
        # Mio and San both on the devops team; the team baseline confers kubeconfig.
        TeamRoster.objects.update_or_create(
            team_name='devops',
            defaults={'tiers': [['san@shizuha.com', 'mio@shizuha.com']]},
        )
        WorkflowService._invalidate_team_roster_caches()

    def tearDown(self):
        WorkflowService._invalidate_team_roster_caches()
        WorkflowService._db_avail_cache = None
        WorkflowService._health_avail_cache = None
        WorkflowService._disabled_emails_cache = None

    def _kube_item(self, **kwargs):
        data = dict(
            project=self.project, title='PLAT-3412 host-plane replacement cycle',
            mode='task', item_type='task.bug', workflow_name='autonomous-bug',
            status='open', workflow_status='open', assignment_group='devops',
            labels={'needs:kubeconfig': True},
        )
        data.update(kwargs)
        return Item.objects.create(**data)

    # (a) PLAT-3412 root cause: a devops team member whose SIGNED evidence lacks
    #     kubeconfig is NOT a candidate in strict mode — the team baseline that made
    #     Mio "capable" is no longer a fallback.
    @patch.dict(os.environ, {'PULSE_SUBSTRATE_CAP_MODE': 'enforce'})
    def test_enforce_signed_verdict_not_team_baseline(self):
        # Under enforce, substrate authority is the signed V2 verdict — NEVER the
        # team baseline. Mio's evidence proves kubeconfig ABSENT (denied); San's
        # proves PRESENT (authorized). Both sit on the devops team.
        from tasks.services import capability_evidence as ce
        item = self._kube_item()
        denied = ce.SubstrateVerdict('enforce', ce.OUTCOME_DENIED, 'seat_incapable_substrate')
        ok = ce.SubstrateVerdict('enforce', ce.OUTCOME_AUTHORIZED, 'ok', frozenset({'kubeconfig'}))
        with patch.object(WorkflowService, '_substrate_evidence_verdict', return_value=denied):
            self.assertFalse(WorkflowService._seat_satisfies_required_capability(
                'mio@shizuha.com', {'kubeconfig'}, item=item))
        with patch.object(WorkflowService, '_substrate_evidence_verdict', return_value=ok):
            self.assertTrue(WorkflowService._seat_satisfies_required_capability(
                'san@shizuha.com', {'kubeconfig'}, item=item))

    # Legacy (default flag-off) mode: the team baseline still satisfies — deploy no-op.
    def test_legacy_mode_team_baseline_still_satisfies_substrate(self):
        with patch.object(WorkflowService, '_agent_effective_capabilities', return_value=set()):
            self.assertTrue(
                WorkflowService._seat_satisfies_required_capability('mio@shizuha.com', {'kubeconfig'}))

    # (c) Source uncertainty (None evidence) fails closed in strict mode; a genuinely
    #     signed-capable seat still passes.
    @patch.dict(os.environ, {'PULSE_SUBSTRATE_CAP_MODE': 'enforce'})
    def test_enforce_authority_unknown_fails_closed(self):
        from tasks.services import capability_evidence as ce
        item = self._kube_item()
        unknown = ce.SubstrateVerdict('enforce', ce.OUTCOME_AUTHORITY_UNKNOWN, 'producer_unreachable')
        ok = ce.SubstrateVerdict('enforce', ce.OUTCOME_AUTHORIZED, 'ok', frozenset({'kubeconfig'}))
        with patch.object(WorkflowService, '_substrate_evidence_verdict', return_value=unknown):
            self.assertFalse(WorkflowService._seat_satisfies_required_capability(
                'san@shizuha.com', {'kubeconfig'}, item=item))
        with patch.object(WorkflowService, '_substrate_evidence_verdict', return_value=ok):
            self.assertTrue(WorkflowService._seat_satisfies_required_capability(
                'san@shizuha.com', {'kubeconfig'}, item=item))

    @patch.dict(os.environ, {'PULSE_SUBSTRATE_CAP_MODE': 'enforce'})
    def test_enforce_without_item_uses_legacy(self):
        # No item context -> cannot build the exact-seat request -> legacy path.
        # The 5 owner-writing callers always pass item; this is the safe fallback.
        with patch.object(WorkflowService, '_agent_effective_capabilities', return_value=set()):
            self.assertTrue(WorkflowService._seat_satisfies_required_capability(
                'mio@shizuha.com', {'kubeconfig'}))  # team baseline satisfies

    # (b) Descriptive kubeconfig/kubernetes labels on code work do NOT demand
    #     kubeconfig; explicit needs:code-substrate is the only requirement and is
    #     satisfiable by a code seat.
    @patch.dict(os.environ, {'PULSE_SUBSTRATE_CAP_MODE': 'enforce'})
    def test_descriptive_labels_do_not_demand_substrate(self):
        code_work = SimpleNamespace(
            labels={'kubeconfig': True, 'needs:code-substrate': True},
            tags='kubernetes', assignment_group='engineering',
            title='Fix kubeconfig routing resolver code', description='',
        )
        required = WorkflowService._task_required_capabilities(code_work)
        self.assertEqual(required, {'code-substrate'})
        self.assertNotIn('kubeconfig', required)
        with patch.object(WorkflowService, '_agent_effective_capabilities', return_value={'code-substrate'}):
            self.assertTrue(
                WorkflowService._seat_satisfies_required_capability('dev@shizuha.com', required))

    # (a)+(d) Verdict: in strict mode a substrate-incapable seat is HARD-blocked even
    #     while the broader PLS-278 predicate stays in shadow, and the deduplicable
    #     shortage reason is present.
    @patch.dict(os.environ, {'PULSE_SUBSTRATE_CAP_MODE': 'enforce', 'PULSE_ROUTABILITY_ENFORCE': 'shadow'})
    @patch('tasks.services.workflow_service.WorkflowService._member_can_execute_from_status', return_value=True)
    @patch('tasks.services.workflow_service.WorkflowService._is_agent_available_for_assignment', return_value=True)
    def test_substrate_incapacity_is_hard_invariant_under_enforce(self, _avail, _can):
        from tasks.services import capability_evidence as ce
        item = self._kube_item()
        assignable = {'devops': [['san@shizuha.com', 'mio@shizuha.com']]}
        denied = ce.SubstrateVerdict('enforce', ce.OUTCOME_DENIED, 'seat_incapable_substrate')
        with patch.object(WorkflowService, '_substrate_evidence_verdict', return_value=denied):
            verdict = WorkflowService._seat_routability_verdict(
                item, 'devops', 'mio@shizuha.com', assignable=assignable)
        self.assertIn('seat_incapable', verdict.reasons)
        self.assertIn('seat_incapable_substrate', verdict.reasons)
        # The shortage is still visible on the verdict. It does not refuse
        # the seat: an available member of the team is assigned, and they
        # reroute if they cannot do the work.
        self.assertFalse(WorkflowService._routability_enforced())
        self.assertTrue(WorkflowService._seat_assignment_allowed(verdict))

    @patch.dict(os.environ, {'PULSE_SUBSTRATE_CAP_MODE': 'shadow', 'PULSE_ROUTABILITY_ENFORCE': 'shadow'})
    @patch('tasks.services.workflow_service.WorkflowService._member_can_execute_from_status', return_value=True)
    @patch('tasks.services.workflow_service.WorkflowService._is_agent_available_for_assignment', return_value=True)
    def test_shadow_observes_but_does_not_hard_block(self, _avail, _can):
        # In shadow the gate returns the legacy outcome (mio via team baseline) and
        # emits a breadcrumb — a denied V2 verdict must NOT hard-block yet.
        from tasks.services import capability_evidence as ce
        item = self._kube_item()
        assignable = {'devops': [['san@shizuha.com', 'mio@shizuha.com']]}
        denied = ce.SubstrateVerdict('shadow', ce.OUTCOME_DENIED, 'seat_incapable_substrate')
        with patch.object(WorkflowService, '_substrate_evidence_verdict', return_value=denied):
            verdict = WorkflowService._seat_routability_verdict(
                item, 'devops', 'mio@shizuha.com', assignable=assignable)
        self.assertNotIn('seat_incapable_substrate', verdict.reasons)
        self.assertNotIn('seat_incapable', verdict.reasons)

    # Default (flag-off) shadow: the substrate hard reason is NOT emitted and the seat
    #     is not hard-blocked — proving the deploy is a behavior no-op.
    @patch.dict(os.environ, {'PULSE_ROUTABILITY_ENFORCE': 'shadow'})
    @patch('tasks.services.workflow_service.WorkflowService._member_can_execute_from_status', return_value=True)
    @patch('tasks.services.workflow_service.WorkflowService._is_agent_available_for_assignment', return_value=True)
    def test_default_mode_no_substrate_hard_block(self, _avail, _can):
        item = self._kube_item()
        assignable = {'devops': [['san@shizuha.com', 'mio@shizuha.com']]}
        with patch.object(WorkflowService, '_agent_effective_capabilities', return_value=set()):
            verdict = WorkflowService._seat_routability_verdict(
                item, 'devops', 'mio@shizuha.com', assignable=assignable)
        # Legacy team-baseline satisfies kubeconfig → no capability reason at all.
        self.assertNotIn('seat_incapable_substrate', verdict.reasons)
        self.assertNotIn('seat_incapable', verdict.reasons)


class PLS459EvidenceUrlCapabilityTests(TestCase):
    """PLS-459: an Origin evidence URL must never imply `kubeconfig`.

    The reported regression: `_task_required_capabilities()` substring-matched the
    bare token `cluster`, and every security-ci finding embeds an Origin run URL
    containing `.svc.cluster.local`. Seven findings (PLAT-4257/4259/4260/4261 and
    PLAT-4313/4314/4315) were therefore routed as Kubernetes work and stranded,
    because no Security seat holds `kubeconfig`.

    ADR-0016 v7 fixed the inference, but no test pinned the *reported shape* — the
    existing coverage asserts the principle with descriptive prose, and
    `.svc.cluster.local` appears nowhere in this module. These fixtures close that
    gap so the evidence-URL path cannot silently regress (it already recurred once
    as PLAT-4612).
    """

    # The exact evidence line these findings carry.
    ORIGIN_RUN_URL = (
        'http://forgejo-http.origin.svc.cluster.local/shizuha-labs/'
        'shizuha-tasks/actions/runs/3680'
    )

    def _finding(self, title, description, **kw):
        data = dict(
            labels=kw.pop('labels', ['security-ci']),
            tags=kw.pop('tags', 'security-sensitive'),
            assignment_group=kw.pop('assignment_group', 'security'),
            title=title,
            description=description,
        )
        data.update(kw)
        return SimpleNamespace(**data)

    def test_origin_evidence_url_does_not_require_kubeconfig(self):
        """The literal PLS-459 shape: a run URL is evidence, not an instruction."""
        item = self._finding(
            'Semgrep: hardcoded credential in tasks/services/mailer.py',
            f'Detected by security-ci.\n\nRun: {self.ORIGIN_RUN_URL}\n'
            'Triage the finding and dispose it in the ledger.',
        )
        self.assertEqual(WorkflowService._task_required_capabilities(item), set())

    def test_seven_affected_security_finding_shapes_stay_unrouted(self):
        """All seven stranded findings (2026-07-12) must infer no capability."""
        shapes = (
            ('PLAT-4257', 'Dependency advisory: urllib3 CVE triage'),
            ('PLAT-4259', 'Semgrep: subprocess call with shell=True'),
            ('PLAT-4260', 'Dependency advisory: requests CVE triage'),
            ('PLAT-4261', 'Semgrep: insecure deserialization candidate'),
            ('PLAT-4313', 'Trivy: base image advisory rollup'),
            ('PLAT-4314', 'Post-merge verification of the advisory ledger'),
            ('PLAT-4315', 'Dependency advisory: certifi bundle refresh'),
        )
        for key, title in shapes:
            with self.subTest(finding=key):
                item = self._finding(
                    f'[{key}] {title}',
                    f'security-ci finding for {key}.\n'
                    f'Evidence: {self.ORIGIN_RUN_URL}\n'
                    'Review the advisory and record the disposition.',
                )
                self.assertEqual(
                    WorkflowService._task_required_capabilities(item), set(),
                    f'{key} must not require kubeconfig from an evidence URL',
                )

    def test_schemeless_cluster_local_hostname_does_not_require_kubeconfig(self):
        """Hardening: the same host without a URL scheme is still not an action.

        The URL scrubber only strips `scheme://...`, so a bare hostname survives
        into the prose. It must still infer nothing, because inference is
        action-bearing rather than substring-based.
        """
        item = self._finding(
            'security-ci log retrieval for the advisory ledger',
            'Logs are served by ci-logs.origin.svc.cluster.local:8080 for this run. '
            'Read the finding and dispose it.',
        )
        self.assertEqual(WorkflowService._task_required_capabilities(item), set())

    def test_evidence_url_does_not_mask_an_explicit_needs_declaration(self):
        """Positive control: `needs:*` stays authoritative alongside evidence."""
        item = self._finding(
            'Rotate the security-ci runner secret',
            f'Evidence: {self.ORIGIN_RUN_URL}',
            labels={'needs:kubeconfig': True},
        )
        self.assertEqual(
            WorkflowService._task_required_capabilities(item), {'kubeconfig'})

    def test_real_kubernetes_action_still_requires_kubeconfig(self):
        """Positive control: a genuine action is still gated, evidence or not."""
        item = self._finding(
            'Roll the security-ci runner deployment',
            f'Evidence: {self.ORIGIN_RUN_URL}\n'
            'Run kubectl apply to update the runner deployment, then roll it out.',
            assignment_group='devops',
            labels=[],
        )
        self.assertEqual(
            WorkflowService._task_required_capabilities(item), {'kubeconfig'})
