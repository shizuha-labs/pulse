"""PLS-184 focused QA task automation."""
from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from tasks.models import (
    DeploymentEvent,
    Item,
    ItemLink,
    Project,
    TeamRoster,
    WorkflowTransition,
)
from tasks.services.deployment_events import upsert_deployment_event
from tasks.services.focused_qa import (
    ensure_focused_qa_for_item,
    reconcile_focused_qa_routes,
    risk_labels_for_item,
)
from tasks.views import ItemViewSet, TransitionExecuteView


class _U:
    id = 1
    pk = 1
    email = 'qa-author@example.com'
    is_authenticated = True
    is_staff = True
    is_superuser = True


class FocusedQATests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(key='PLS', org_id=1, name='Pulse')
        # PLS-571: mint-successful tests traverse routing, and routing
        # resolution (classify_roster_email -> shizuha-id) is post-eligibility
        # by design. Stub it identity-preservingly so no test hits the live
        # ID service; per-test patches override where a specific target is
        # needed.
        roster_patch = patch(
            'tasks.services.canonical_identity.classify_roster_email',
            # Replicate the offline environment the suite was built against:
            # the authority is unreachable, callers fail open
            # ('resolution_error' — keep existing behavior, never phantom).
            side_effect=lambda email, **kw: ('resolution_error', email),
        )
        roster_patch.start()
        self.addCleanup(roster_patch.stop)

    def test_risk_label_normalization_reads_labels_and_tags(self):
        item = Item.objects.create(
            project=self.project,
            title='Auth task',
            labels={'requires-live-qa': True, 'risk': 'auth'},
            tags='user-facing,other',
        )
        self.assertEqual(risk_labels_for_item(item), {'requires-live-qa', 'auth', 'user-facing'})

    def test_label_update_endpoint_creates_one_focused_qa_task(self):
        # PLS-571: eligibility requires typed live-testable evidence — the
        # risk label alone no longer qualifies, so the fixture is development
        # work (task.bug) whose label update routes it to focused QA.
        item = Item.objects.create(
            project=self.project, title='Needs live QA', status='open',
            item_type='task.bug',
        )
        view = ItemViewSet.as_view({'patch': 'partial_update'})
        factory = APIRequestFactory()

        req = factory.patch(f'/items/{item.pk}/', {'labels': {'requires-live-qa': True}}, format='json')
        force_authenticate(req, user=_U())
        resp = view(req, pk=item.pk)
        self.assertEqual(resp.status_code, 200)
        qa = Item.objects.get(source='focused-qa-automation')
        self.assertEqual(qa.assignment_group, 'qa')
        # PLS-825: the generator binds new focused-QA tasks to the dedicated
        # focused-qa workflow and one immutable generation row once the rollout
        # gate is green.
        self.assertEqual(qa.workflow_name, 'focused-qa')
        self.assertEqual(qa.metadata['focused_qa_origin_item_id'], str(item.pk))
        self.assertEqual(qa.metadata['focused_qa_risk_label'], 'requires-live-qa')
        self.assertTrue(hasattr(qa, 'focused_qa_generation'))
        self.assertEqual(qa.focused_qa_generation.generation_key, f'origin:{item.pk}:requires-live-qa:live')

        req = factory.patch(f'/items/{item.pk}/', {'labels': {'requires-live-qa': True}}, format='json')
        force_authenticate(req, user=_U())
        resp = view(req, pk=item.pk)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(Item.objects.filter(source='focused-qa-automation').count(), 1)

    def test_deployment_event_for_high_risk_source_task_creates_focused_qa(self):
        source = Item.objects.create(
            project=self.project,
            item_key=self.project.get_next_item_key(),
            title='Runtime replay fix',
            status='done',
            labels={'replay': True},
        )
        event, created, devops_task = upsert_deployment_event({
            'project_key': 'PLS',
            'repo': 'shizuha-labs/shizuha-tasks',
            'service': 'pulse',
            'environment': 'live',
            'cluster': 'shizuha-k3s',
            'commit_sha': '1234567890abcdef',
            'image_digest': 'sha256:abc',
            'pipeline_run_id': 'run-1',
            'state': 'smoke_passed',
            'smoke_result': 'passed',
            'source_task_keys': [source.item_key],
            'metadata': {'risk_labels': ['replay']},
        })
        self.assertTrue(created)
        self.assertIsNone(devops_task)
        qa = Item.objects.get(source='focused-qa-automation')
        self.assertEqual(qa.metadata['focused_qa_deployment_event_id'], str(event.id))
        self.assertEqual(qa.metadata['focused_qa_origin_item_key'], source.item_key)
        self.assertEqual(qa.metadata['focused_qa_risk_label'], 'replay')
        self.assertEqual(Item.objects.filter(source='focused-qa-automation').count(), 1)

        upsert_deployment_event({
            'project_key': 'PLS',
            'repo': 'shizuha-labs/shizuha-tasks',
            'service': 'pulse',
            'environment': 'live',
            'cluster': 'shizuha-k3s',
            'commit_sha': '1234567890abcdef',
            'image_digest': 'sha256:abc',
            'pipeline_run_id': 'run-1',
            'state': 'smoke_passed',
            'smoke_result': 'passed',
            'source_task_keys': [source.item_key],
        })
        self.assertEqual(Item.objects.filter(source='focused-qa-automation').count(), 1)

    def test_reingest_same_event_after_qa_closed_does_not_remint(self):
        """Re-ingesting the SAME deployment event after the focused-QA task is
        closed must NOT mint a fresh QA-of-QA task (infinite-loop regression:
        dojo UX closes observed DOJO-62..74 being re-minted on every event
        update because the open-task dedupe excludes closed tasks)."""
        source = Item.objects.create(
            project=self.project,
            item_key=self.project.get_next_item_key(),
            title='Runtime replay fix',
            status='done',
            labels={'replay': True},
        )
        payload = {
            'project_key': 'PLS',
            'repo': 'shizuha-labs/shizuha-tasks',
            'service': 'pulse',
            'environment': 'live',
            'cluster': 'shizuha-k3s',
            'commit_sha': '1234567890abcdef',
            'image_digest': 'sha256:abc',
            'pipeline_run_id': 'run-1',
            'state': 'smoke_passed',
            'smoke_result': 'passed',
            'source_task_keys': [source.item_key],
            'metadata': {'risk_labels': ['replay']},
        }
        event, created, _ = upsert_deployment_event(payload)
        self.assertTrue(created)
        qa = Item.objects.get(source='focused-qa-automation')
        self.assertEqual(Item.objects.filter(source='focused-qa-automation').count(), 1)
        # Close the focused-QA task (simulating a completed verification).
        qa.status = 'done'
        qa.save(update_fields=['status', 'updated_at'])
        # Re-ingest the same event (CI retry / same idempotency key): must NOT
        # create a new focused-QA task for the same origin+risk+env+event.
        upsert_deployment_event(payload)
        self.assertEqual(
            Item.objects.filter(source='focused-qa-automation').count(), 1,
            're-ingesting the same deployment event after QA closed must not re-mint',
        )

    def test_upsert_merges_metadata_instead_of_replacing(self):
        """PLS-952: a partial re-ingest (e.g. a DevOps carrier recording
        replicas_ready evidence) must MERGE producer metadata, never replace it
        wholesale — otherwise typed fields like risk_labels are silently
        destroyed by a payload that does not restate them."""
        payload = {
            'project_key': 'PLS',
            'repo': 'shizuha-labs/shizuha-tasks',
            'service': 'pulse',
            'environment': 'live',
            'cluster': 'shizuha-k3s',
            'commit_sha': '1234567890abcdef',
            'image_digest': 'sha256:abc',
            'pipeline_run_id': 'run-1',
            'state': 'smoke_passed',
            'smoke_result': 'passed',
            'metadata': {'risk_labels': ['requires-live-qa', 'user-facing'], 'producer_note': 'keep me'},
        }
        event, created, _ = upsert_deployment_event(payload)
        self.assertTrue(created)
        self.assertEqual(event.metadata['risk_labels'], ['requires-live-qa', 'user-facing'])

        # Partial re-ingest: records replicas_ready evidence but does NOT
        # restate risk_labels / producer_note. Both must survive the merge.
        event2, created2, _ = upsert_deployment_event({
            'project_key': 'PLS',
            'repo': 'shizuha-labs/shizuha-tasks',
            'service': 'pulse',
            'environment': 'live',
            'cluster': 'shizuha-k3s',
            'commit_sha': '1234567890abcdef',
            'image_digest': 'sha256:abc',
            'pipeline_run_id': 'run-1',
            'state': 'smoke_passed',
            'smoke_result': 'passed',
            'metadata': {'all_replicas_ready': True, 'replicas_ready': 3},
        })
        self.assertFalse(created2)
        event2.refresh_from_db()
        self.assertEqual(event2.pk, event.pk)
        self.assertEqual(event2.metadata['risk_labels'], ['requires-live-qa', 'user-facing'])
        self.assertEqual(event2.metadata['producer_note'], 'keep me')
        self.assertTrue(event2.metadata['all_replicas_ready'])
        self.assertEqual(event2.metadata['replicas_ready'], 3)

        # Incoming keys win on conflict; producer keys absent from the payload
        # are preserved.
        event3, created3, _ = upsert_deployment_event({
            'project_key': 'PLS',
            'repo': 'shizuha-labs/shizuha-tasks',
            'service': 'pulse',
            'environment': 'live',
            'cluster': 'shizuha-k3s',
            'commit_sha': '1234567890abcdef',
            'image_digest': 'sha256:abc',
            'pipeline_run_id': 'run-1',
            'state': 'smoke_passed',
            'smoke_result': 'passed',
            'metadata': {'risk_labels': ['auth']},
        })
        self.assertFalse(created3)
        event3.refresh_from_db()
        self.assertEqual(event3.metadata['risk_labels'], ['auth'])
        self.assertEqual(event3.metadata['producer_note'], 'keep me')
        self.assertTrue(event3.metadata['all_replicas_ready'])

    def test_deployment_event_names_required_operator_surfaces(self):
        event, created, devops_task = upsert_deployment_event({
            'project_key': 'PLS',
            'repo': 'shizuha-labs/hive',
            'service': 'hive-frontend',
            'environment': 'live',
            'cluster': 'shizuha-k3s',
            'commit_sha': '3234567890abcdef',
            'image_digest': 'sha256:hive-ui',
            'pipeline_run_id': 'run-hive-ui',
            'state': 'smoke_passed',
            'smoke_result': 'passed',
            'metadata': {
                'risk_labels': ['requires-live-qa'],
                'qa_surfaces': ['/hive/agents', '/hive/subscriptions'],
            },
        })

        self.assertTrue(created)
        self.assertIsNone(devops_task)
        qa = Item.objects.get(source='focused-qa-automation')
        self.assertIn('`/hive/agents`', qa.description)
        self.assertIn('`/hive/subscriptions`', qa.description)

    def test_deployment_event_metadata_risk_uses_origin_item_identity(self):
        source = Item.objects.create(
            project=self.project,
            item_key=self.project.get_next_item_key(),
            title='User-facing deploy without item label',
            status='done',
        )

        event, created, devops_task = upsert_deployment_event({
            'project_key': 'PLS',
            'repo': 'shizuha-labs/shizuha-tasks',
            'service': 'pulse',
            'environment': 'live',
            'cluster': 'shizuha-k3s',
            'commit_sha': '2234567890abcdef',
            'image_digest': 'sha256:def',
            'pipeline_run_id': 'run-1',
            'state': 'smoke_passed',
            'smoke_result': 'passed',
            'source_task_keys': [source.item_key],
            'metadata': {'risk_labels': ['user-facing']},
        })

        self.assertTrue(created)
        self.assertIsNone(devops_task)
        qa = Item.objects.get(source='focused-qa-automation')
        self.assertEqual(qa.metadata['focused_qa_origin_item_id'], str(source.pk))
        self.assertEqual(qa.metadata['focused_qa_deployment_event_id'], str(event.id))
        self.assertEqual(qa.metadata['focused_qa_risk_label'], 'user-facing')

        upsert_deployment_event({
            'project_key': 'PLS',
            'repo': 'shizuha-labs/shizuha-tasks',
            'service': 'pulse',
            'environment': 'live',
            'cluster': 'shizuha-k3s',
            'commit_sha': '2234567890abcdef',
            'image_digest': 'sha256:def',
            'pipeline_run_id': 'run-2',
            'state': 'smoke_passed',
            'smoke_result': 'passed',
            'source_task_keys': [source.item_key],
            'metadata': {'risk_labels': ['user-facing']},
        })
        self.assertEqual(Item.objects.filter(source='focused-qa-automation').count(), 1)

    def test_label_update_then_deployment_event_updates_existing_focused_qa(self):
        # Risk-neutral title: PLAT-1209 derives risk labels from title/description
        # at insert, and a second derived label would (correctly) mean a second
        # focused-QA task per the one-per-origin/risk/environment contract. This
        # test pins the single-label update path, so the only risk label must be
        # the explicit requires-live-qa.
        source = Item.objects.create(
            project=self.project,
            item_key=self.project.get_next_item_key(),
            title='Sidebar copy tweak',
            status='done',
            item_type='task.bug',
            labels={'requires-live-qa': True},
        )
        first = ensure_focused_qa_for_item(source, trigger='label_update')[0]
        self.assertIsNone(first.metadata['focused_qa_deployment_event_id'])

        event, created, devops_task = upsert_deployment_event({
            'project_key': 'PLS',
            'repo': 'shizuha-labs/shizuha-tasks',
            'service': 'pulse',
            'environment': 'live',
            'cluster': 'shizuha-k3s',
            'commit_sha': 'abcdef1234567890',
            'image_digest': 'sha256:abc',
            'pipeline_run_id': 'run-1',
            'state': 'smoke_passed',
            'smoke_result': 'passed',
            'source_task_keys': [source.item_key],
        })
        self.assertTrue(created)
        self.assertIsNone(devops_task)
        self.assertEqual(Item.objects.filter(source='focused-qa-automation').count(), 1)
        first.refresh_from_db()
        self.assertEqual(first.metadata['focused_qa_deployment_event_id'], str(event.id))
        self.assertEqual(first.metadata['focused_qa_trigger'], 'deployment_event')
        self.assertIn(str(event.id), first.description)

        upsert_deployment_event({
            'project_key': 'PLS',
            'repo': 'shizuha-labs/shizuha-tasks',
            'service': 'pulse',
            'environment': 'live',
            'cluster': 'shizuha-k3s',
            'commit_sha': 'abcdef1234567890',
            'image_digest': 'sha256:retry',
            'pipeline_run_id': 'run-2',
            'state': 'smoke_passed',
            'smoke_result': 'passed',
            'source_task_keys': [source.item_key],
        })
        self.assertEqual(Item.objects.filter(source='focused-qa-automation').count(), 1)

    def test_direct_service_dedupes_per_origin_risk_environment(self):
        # PLS-571: the eligibility gate requires typed live-testable evidence —
        # a bare risk label ('payments-books') alone no longer qualifies. This
        # fixture represents a real payment-flow change (task.bug = development
        # work), so both triggers create/dedupe one QA item.
        item = Item.objects.create(
            project=self.project, title='Payment flow',
            item_type='task.bug', labels={'payments-books': True},
        )
        a = ensure_focused_qa_for_item(item, trigger='label_update')
        b = ensure_focused_qa_for_item(item, trigger='status_transition')
        self.assertEqual(a[0].pk, b[0].pk)

    def test_org_without_qa_routes_to_origin_assignees_exact_local_team(self):
        digital = Project.objects.create(key='BKSD', org_id=2, name='Books Digital')
        TeamRoster.objects.create(
            team_name='accounting',
            organization_slug='shizuha-digital',
            organization_id=2,
            tiers=[['kane@shizuha.com']],
        )
        origin = Item.objects.create(
            project=digital,
            item_key=digital.get_next_item_key(),
            title='Digital LLP accounting backfill',
            status='done',
            item_type='task.bug',
            assignee_email='kane@shizuha.com',
            labels={'data-migration': True},
        )

        qa = ensure_focused_qa_for_item(
            origin, trigger='status_transition',
        )[0]

        self.assertEqual(qa.project, digital)
        self.assertEqual(qa.assignment_group, 'accounting')
        self.assertEqual(
            qa.metadata['focused_qa_routing_mode'],
            'origin-team-fallback',
        )
        self.assertIn('qa-independence-exception', qa.tags)
        self.assertIn('no staffed QA team', qa.description)
        self.assertNotEqual(qa.assignee_email, 'zen@shizuha.com')

    def test_repeated_trigger_preserves_proven_local_team_after_origin_enters_qa(self):
        digital = Project.objects.create(
            key='BKSD', org_id=2, name='Books Digital',
        )
        TeamRoster.objects.create(
            team_name='accounting',
            organization_slug='shizuha-digital',
            organization_id=2,
            tiers=[['kane@shizuha.com']],
        )
        origin = Item.objects.create(
            project=digital,
            item_key=digital.get_next_item_key(),
            title='Digital LLP capsule repair',
            status='in_progress',
            item_type='task.bug',
            assignment_group='accounting',
            assignee_email='kane@shizuha.com',
            created_by_email='automation@shizuha.com',
            labels={'data-migration': True},
        )
        qa = ensure_focused_qa_for_item(
            origin, trigger='label_update',
        )[0]
        self.assertEqual(qa.assignment_group, 'accounting')

        # Submitting the source for review temporarily moves it to a phantom
        # local QA lane and clears its assignee. A repeated risk-label trigger
        # must not erase the already proven exact-org fallback.
        origin.assignment_group = 'qa'
        origin.assignee_email = None
        origin.status = 'in_review'
        origin.save(update_fields=[
            'assignment_group', 'assignee_email', 'status', 'updated_at',
        ])

        repeated = ensure_focused_qa_for_item(
            origin, trigger='status_transition',
        )[0]

        self.assertEqual(repeated.pk, qa.pk)
        self.assertEqual(repeated.assignment_group, 'accounting')
        self.assertEqual(
            repeated.metadata['focused_qa_routing_mode'],
            'origin-team-fallback',
        )
        self.assertEqual(
            repeated.metadata['focused_qa_assignment_group'],
            'accounting',
        )
        self.assertIn('qa-independence-exception', repeated.tags)
        self.assertNotIn('qa-routing-blocked', repeated.tags)

    def test_reconciler_recovers_local_team_from_exact_org_reporter(self):
        digital = Project.objects.create(
            key='BKSD', org_id=2, name='Books Digital',
        )
        TeamRoster.objects.create(
            team_name='accounting',
            organization_slug='shizuha-digital',
            organization_id=2,
            tiers=[['kane@shizuha.com']],
        )
        origin = Item.objects.create(
            project=digital,
            item_key=digital.get_next_item_key(),
            title='Digital LLP capsule repair in review',
            status='in_review',
            assignment_group='qa',
            created_by_email='kane@shizuha.com',
        )
        qa = Item.objects.create(
            project=digital,
            item_key=digital.get_next_item_key(),
            title='Stripped focused QA route',
            source='focused-qa-automation',
            status='todo',
            assignment_group=None,
            workflow_name='reviewed-task',
            tags='focused-qa,qa-routing-blocked',
            metadata={
                'focused_qa_origin_item_id': str(origin.pk),
                'focused_qa_risk_label': 'data-migration',
                'focused_qa_environment': 'live',
                'focused_qa_routing_mode': 'unroutable-local',
                'focused_qa_assignment_group': None,
            },
        )

        self.assertEqual(reconcile_focused_qa_routes(), 1)

        qa.refresh_from_db()
        self.assertEqual(qa.assignment_group, 'accounting')
        self.assertEqual(
            qa.metadata['focused_qa_routing_mode'],
            'origin-team-fallback',
        )
        self.assertIn('qa-independence-exception', qa.tags)
        self.assertNotIn('qa-routing-blocked', qa.tags)

    def test_reconciler_retries_lease_after_local_seat_becomes_available(self):
        digital = Project.objects.create(
            key='BKSD', org_id=2, name='Books Digital',
        )
        TeamRoster.objects.create(
            team_name='accounting',
            organization_slug='shizuha-digital',
            organization_id=2,
            tiers=[['kane@shizuha.com']],
        )
        origin = Item.objects.create(
            project=digital,
            item_key=digital.get_next_item_key(),
            title='Digital LLP capsule repair in review',
            status='in_review',
            assignment_group='qa',
            created_by_email='nova@shizuha.com',
        )
        qa = Item.objects.create(
            project=digital,
            item_key=digital.get_next_item_key(),
            title='Focused QA waiting for its local seat',
            source='focused-qa-automation',
            status='todo',
            assignment_group='accounting',
            workflow_name='reviewed-task',
            metadata={
                'focused_qa_origin_item_id': str(origin.pk),
                'focused_qa_routing_mode': 'origin-team-fallback',
                'focused_qa_assignment_group': 'accounting',
            },
        )

        with patch(
            'tasks.services.workflow_service.WorkflowService.'
            '_resolve_team_assignee',
            return_value=(None, None),
        ) as resolve:
            self.assertEqual(reconcile_focused_qa_routes(), 0)
        resolve.assert_called_once_with(
            'accounting',
            exclude_email=None,
            hard_exclude_emails={'nova@shizuha.com'},
            enforce_cap=True,
            project_key='BKSD',
            item=qa,
        )

        with patch(
            'tasks.services.workflow_service.WorkflowService.'
            '_resolve_team_assignee',
            return_value=(42, 'kane@shizuha.com'),
        ) as resolve:
            self.assertEqual(reconcile_focused_qa_routes(), 1)
        resolve.assert_called_once_with(
            'accounting',
            exclude_email=None,
            hard_exclude_emails={'nova@shizuha.com'},
            enforce_cap=True,
            project_key='BKSD',
            item=qa,
        )

        qa.refresh_from_db()
        self.assertEqual(qa.assignee_id, 42)
        self.assertEqual(qa.assignee_email, 'kane@shizuha.com')
        self.assertEqual(qa.assignment_group, 'accounting')
        self.assertEqual(
            qa.metadata['focused_qa_routing_mode'],
            'origin-team-fallback',
        )

    def test_submit_for_review_preserves_org_local_focused_qa_team(self):
        from tasks.services.workflow_service import WorkflowService

        digital = Project.objects.create(
            key='BKSD',
            org_id=2,
            name='Books Digital',
        )
        TeamRoster.objects.create(
            team_name='accounting',
            organization_slug='shizuha-digital',
            organization_id=2,
            tiers=[['kane@shizuha.com']],
        )
        qa = Item.objects.create(
            project=digital,
            item_key=digital.get_next_item_key(),
            title='Digital accounting focused QA',
            source='focused-qa-automation',
            status='in_review',
            priority='high',
            assignment_group='accounting',
            assignee_id=42,
            assignee_email='kane@shizuha.com',
            created_by_email='qa-automation@shizuha.com',
            workflow_name='reviewed-task',
            metadata={
                'focused_qa_routing_mode': 'origin-team-fallback',
                'focused_qa_assignment_group': 'accounting',
            },
        )
        transition = WorkflowTransition.objects.get(
            workflow__slug='reviewed-task',
            from_status='in_progress',
            to_status='in_review',
            name='Submit for Review',
        )

        with patch.object(
            WorkflowService,
            '_resolve_team_assignee',
            return_value=(42, 'kane@shizuha.com'),
        ) as resolve:
            WorkflowService.execute_post_functions(transition, qa)

        qa.refresh_from_db()
        self.assertEqual(qa.assignment_group, 'accounting')
        self.assertEqual(qa.assignee_id, 42)
        self.assertEqual(qa.assignee_email, 'kane@shizuha.com')
        self.assertEqual(
            resolve.call_args.args[0],
            'accounting',
        )

    def test_reconciler_repairs_existing_phantom_qa_route_org_locally(self):
        digital = Project.objects.create(key='BKSD', org_id=2, name='Books Digital')
        TeamRoster.objects.create(
            team_name='accounting',
            organization_slug='shizuha-digital',
            organization_id=2,
            tiers=[['kane@shizuha.com']],
        )
        origin = Item.objects.create(
            project=digital,
            item_key=digital.get_next_item_key(),
            title='Completed accounting migration',
            status='done',
            assignee_email='kane@shizuha.com',
        )
        qa = Item.objects.create(
            project=digital,
            item_key=digital.get_next_item_key(),
            title='Existing focused QA',
            source='focused-qa-automation',
            status='todo',
            assignment_group='qa',
            workflow_name='reviewed-task',
            metadata={
                'focused_qa_origin_item_id': str(origin.pk),
                'focused_qa_risk_label': 'data-migration',
                'focused_qa_environment': 'live',
            },
        )

        self.assertEqual(reconcile_focused_qa_routes(), 1)

        qa.refresh_from_db()
        self.assertEqual(qa.assignment_group, 'accounting')
        self.assertEqual(
            qa.metadata['focused_qa_routing_mode'],
            'origin-team-fallback',
        )
        self.assertTrue(
            qa.comments.filter(
                author_email='qa-automation@shizuha.com',
                content__contains='no staffed QA team',
            ).exists(),
        )

    def test_reconciler_clears_stale_fallback_tag_when_local_qa_exists(self):
        digital = Project.objects.create(key='BKSD', org_id=2, name='Books Digital')
        TeamRoster.objects.create(
            team_name='qa',
            organization_slug='shizuha-digital',
            organization_id=2,
            tiers=[['local-qa@shizuha.com']],
        )
        origin = Item.objects.create(
            project=digital,
            item_key=digital.get_next_item_key(),
            title='Completed accounting migration',
            status='done',
        )
        qa = Item.objects.create(
            project=digital,
            item_key=digital.get_next_item_key(),
            title='Existing focused QA',
            source='focused-qa-automation',
            status='todo',
            assignment_group='accounting',
            workflow_name='reviewed-task',
            tags='focused-qa,qa-independence-exception',
            metadata={
                'focused_qa_origin_item_id': str(origin.pk),
                'focused_qa_routing_mode': 'origin-team-fallback',
            },
        )

        self.assertEqual(reconcile_focused_qa_routes(), 1)

        qa.refresh_from_db()
        self.assertEqual(qa.assignment_group, 'qa')
        self.assertNotIn('qa-independence-exception', qa.tags)
        self.assertEqual(qa.metadata['focused_qa_routing_mode'], 'qa-roster')

    def test_focused_qa_request_changes_files_owning_project_bug(self):
        origin = Item.objects.create(
            project=self.project,
            item_key=self.project.get_next_item_key(),
            title='Auth change',
            status='done',
            assignment_group='identity',
        )
        qa = Item.objects.create(
            project=self.project,
            item_key=self.project.get_next_item_key(),
            title='Focused QA auth check',
            source='focused-qa-automation',
            source_id='qa',
            status='in_review',
            workflow_name='reviewed-task',
            assignment_group='qa',
            labels={'no-pr': True},
            metadata={
                'focused_qa_origin_item_id': str(origin.pk),
                'focused_qa_origin_item_key': origin.item_key,
                'focused_qa_risk_label': 'auth',
                'focused_qa_environment': 'live',
            },
        )
        transition = WorkflowTransition.objects.get(
            workflow__slug='reviewed-task',
            from_status='in_review',
            to_status='in_progress',
            name='Request Changes',
        )
        view = TransitionExecuteView.as_view()
        factory = APIRequestFactory()
        req = factory.post(
            f'/items/{qa.pk}/execute-transition/',
            {
                'transition_id': str(transition.id),
                'comment': 'Observed login returns 500 for priority user.',
            },
            format='json',
        )
        force_authenticate(req, user=_U())

        resp = view(req, item_id=qa.pk)

        self.assertEqual(resp.status_code, 200)
        bug = Item.objects.get(source='focused-qa-failure')
        self.assertEqual(bug.workflow_name, 'autonomous-bug')
        self.assertEqual(bug.status, 'needs_triage')
        self.assertEqual(bug.project, self.project)
        self.assertEqual(bug.assignment_group, 'identity')
        self.assertIn('Observed login returns 500', bug.description)
        self.assertEqual(bug.metadata['focused_qa_task_id'], str(qa.pk))
        self.assertTrue(ItemLink.objects.filter(source_item=qa, target_item=bug).exists())
        self.assertTrue(ItemLink.objects.filter(source_item=origin, target_item=bug).exists())

    def test_qa_lane_origin_failure_routes_bug_to_engineering(self):
        origin = Item.objects.create(
            project=self.project,
            item_key=self.project.get_next_item_key(),
            title='Hive visual truth audit',
            status='done',
            assignment_group='qa',
        )
        qa = Item.objects.create(
            project=self.project,
            item_key=self.project.get_next_item_key(),
            title='Focused QA Hive health accounting',
            source='focused-qa-automation',
            status='in_review',
            workflow_name='reviewed-task',
            assignment_group='qa',
            metadata={'focused_qa_origin_item_id': str(origin.pk)},
        )

        from tasks.services.focused_qa import create_bug_from_qa_failure
        bug = create_bug_from_qa_failure(
            qa,
            title='[QA failure] Hive card contradicts API',
            reproduction='Sara renders Unavailable while explicit provider state is available.',
        )

        self.assertEqual(bug.assignment_group, 'engineering')
        self.assertEqual(bug.workflow_name, 'autonomous-bug')

    def test_pls609_reconcile_preserves_in_boundary_direct_assignee(self):
        """Direct assign -> re-assertion keeps the exact-org QA member."""
        project = Project.objects.create(
            key='P609', org_id=609, name='PLS-609 regression',
        )
        TeamRoster.objects.create(
            team_name='qa',
            organization_slug='pls609-test',
            organization_id=609,
            tiers=[['zen@shizuha.com', 'nova@shizuha.com']],
        )
        origin = Item.objects.create(
            project=project,
            item_key=project.get_next_item_key(),
            title='SoD fixture requested by nova',
            status='done',
            created_by_email='nova@shizuha.com',
        )
        qa = Item.objects.create(
            project=project,
            item_key=project.get_next_item_key(),
            title='Focused QA SoD handoff',
            source='focused-qa-automation',
            status='todo',
            workflow_name='reviewed-task',
            assignment_group='qa',
            assignee_id=909,
            assignee_email='zen@shizuha.com',
            metadata={
                'focused_qa_origin_item_id': str(origin.pk),
                'focused_qa_routing_mode': 'origin-team-fallback',
                'focused_qa_assignment_group': 'qa',
                'direct_assignment': {
                    'by': 'reika@shizuha.com',
                    'reason': 'SoD: nova requested the fixture under test',
                },
            },
        )

        with patch(
            'tasks.services.workflow_service.WorkflowService.'
            '_resolve_team_assignee',
            return_value=(910, 'nova@shizuha.com'),
        ) as resolve:
            self.assertEqual(reconcile_focused_qa_routes(limit=50), 1)
        resolve.assert_not_called()

        qa.refresh_from_db()
        self.assertEqual(qa.assignee_id, 909)
        self.assertEqual(qa.assignee_email, 'zen@shizuha.com')
        self.assertEqual(qa.assignment_group, 'qa')
        self.assertEqual(
            qa.metadata['focused_qa_routing_mode'], 'qa-roster',
        )
        self.assertFalse(
            qa.comments.filter(
                author_email='qa-automation@shizuha.com',
                content__contains='Routing boundary',
            ).exists(),
        )

    def test_pls609_reconcile_still_clears_out_of_boundary_assignee(self):
        """Negative control: an outsider still takes the re-routing path."""
        project = Project.objects.create(
            key='P609', org_id=609, name='PLS-609 regression',
        )
        TeamRoster.objects.create(
            team_name='qa',
            organization_slug='pls609-test',
            organization_id=609,
            tiers=[['zen@shizuha.com', 'nova@shizuha.com']],
        )
        TeamRoster.objects.create(
            team_name='engineering',
            organization_slug='pls609-test',
            organization_id=609,
            tiers=[['outsider@shizuha.com']],
        )
        origin = Item.objects.create(
            project=project,
            item_key=project.get_next_item_key(),
            title='Fixture awaiting focused QA',
            status='done',
        )
        qa = Item.objects.create(
            project=project,
            item_key=project.get_next_item_key(),
            title='Focused QA with stale outsider',
            source='focused-qa-automation',
            status='todo',
            workflow_name='reviewed-task',
            assignment_group='qa',
            metadata={
                'focused_qa_origin_item_id': str(origin.pk),
                'focused_qa_routing_mode': 'origin-team-fallback',
                'focused_qa_assignment_group': 'qa',
            },
        )
        # Simulate an audited legacy writer that supplied the DB guard's exact
        # one-use ownership proof before the newer Item.save() boundary existed.
        # Reconcile must still repair this in-org, out-of-team row.
        from django.db import connection
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('pulse.assignment_org_proof', %s, true)",
                [
                    f'609|{project.pk}|808|'
                    'outsider@shizuha.com|qa',
                ],
            )
        Item.objects.filter(pk=qa.pk).update(
            assignee_id=808,
            assignee_email='outsider@shizuha.com',
        )
        qa.refresh_from_db()

        with patch(
            'tasks.services.workflow_service.WorkflowService.'
            '_resolve_team_assignee',
            return_value=(None, None),
        ) as resolve:
            self.assertEqual(reconcile_focused_qa_routes(limit=50), 1)
        resolve.assert_called_once_with(
            'qa',
            exclude_email=None,
            hard_exclude_emails=set(),
            enforce_cap=True,
            project_key='P609',
            item=qa,
        )

        qa.refresh_from_db()
        self.assertIsNone(qa.assignee_id)
        self.assertIsNone(qa.assignee_email)
        self.assertEqual(qa.assignment_group, 'qa')
        self.assertEqual(
            qa.metadata['focused_qa_routing_mode'], 'qa-roster',
        )
        self.assertTrue(
            qa.comments.filter(
                author_email='qa-automation@shizuha.com',
                content__contains='Routing boundary',
            ).exists(),
        )

    def test_pls609_retention_does_not_override_conflict_exclusion(self):
        """An in-team but conflicted owner is cleared and safely replaced."""
        project = Project.objects.create(
            key='P609', org_id=609, name='PLS-609 regression',
        )
        TeamRoster.objects.create(
            team_name='qa',
            organization_slug='pls609-test',
            organization_id=609,
            tiers=[['zen@shizuha.com', 'nova@shizuha.com']],
        )
        origin = Item.objects.create(
            project=project,
            item_key=project.get_next_item_key(),
            title='Fixture requested by nova',
            status='done',
            created_by_email='nova@shizuha.com',
        )
        qa = Item.objects.create(
            project=project,
            item_key=project.get_next_item_key(),
            title='Focused QA initially on requester',
            source='focused-qa-automation',
            status='todo',
            workflow_name='reviewed-task',
            assignment_group='qa',
            assignee_email='nova@shizuha.com',
            metadata={
                'focused_qa_origin_item_id': str(origin.pk),
                'focused_qa_routing_mode': 'origin-team-fallback',
                'focused_qa_assignment_group': 'qa',
            },
        )

        with patch(
            'tasks.services.workflow_service.WorkflowService.'
            '_resolve_team_assignee',
            return_value=(None, 'zen@shizuha.com'),
        ) as resolve:
            self.assertEqual(reconcile_focused_qa_routes(limit=50), 1)
        self.assertEqual(
            resolve.call_args.kwargs['hard_exclude_emails'],
            {'nova@shizuha.com'},
        )

        qa.refresh_from_db()
        self.assertIsNone(qa.assignee_id)
        self.assertEqual(qa.assignee_email, 'zen@shizuha.com')


class PLS300FocusedQAGuardTests(TestCase):
    """PLS-300: focused-QA must NOT fire for non-fix dispositions or static-scan/
    dep-CVE findings, and must coalesce to ONE task per finding."""

    def setUp(self):
        self.project = Project.objects.create(key='PLS', org_id=1, name='Pulse')
        # PLS-571: mint-successful tests traverse routing, and routing
        # resolution (classify_roster_email -> shizuha-id) is post-eligibility
        # by design. Stub it identity-preservingly so no test hits the live
        # ID service; per-test patches override where a specific target is
        # needed.
        roster_patch = patch(
            'tasks.services.canonical_identity.classify_roster_email',
            # Replicate the offline environment the suite was built against:
            # the authority is unreachable, callers fail open
            # ('resolution_error' — keep existing behavior, never phantom).
            side_effect=lambda email, **kw: ('resolution_error', email),
        )
        roster_patch.start()
        self.addCleanup(roster_patch.stop)

    def _qa_count(self):
        return Item.objects.filter(source='focused-qa-automation').count()

    def test_rejected_finding_does_not_spawn_qa(self):
        from tasks.models import Resolution
        rej, _ = Resolution.objects.get_or_create(slug='rejected', defaults={'name': 'Rejected'})
        item = Item.objects.create(project=self.project, title='rejected FP',
                                   status='done', resolution=rej,
                                   labels={'auth': True, 'security-sensitive': True})
        created = ensure_focused_qa_for_item(item, trigger='status_transition')
        self.assertEqual(created, [])
        self.assertEqual(self._qa_count(), 0)

    def test_wont_fix_resolution_does_not_spawn_qa(self):
        from tasks.models import Resolution
        wf, _ = Resolution.objects.get_or_create(slug='wont_fix', defaults={'name': "Won't Fix"})
        item = Item.objects.create(project=self.project, title='wont fix',
                                   status='closed', resolution=wf, labels={'auth': True})
        self.assertEqual(ensure_focused_qa_for_item(item, trigger='status_transition'), [])
        self.assertEqual(self._qa_count(), 0)

    def test_static_scan_security_ci_finding_does_not_spawn_qa(self):
        # dep-CVE / static-scan finding: security-ci label, no live surface.
        item = Item.objects.create(project=self.project, title='[security-ci][deps] upgrade vite',
                                   status='done',
                                   labels={'security-ci': True, 'security-sensitive': True},
                                   tags='security-ci,tool:deps,severity:high')
        self.assertEqual(ensure_focused_qa_for_item(item, trigger='status_transition'), [])
        self.assertEqual(self._qa_count(), 0)

    def test_security_ci_source_finding_does_not_spawn_qa(self):
        item = Item.objects.create(project=self.project, title='bandit finding',
                                   status='done', source='security-ci:abc123',
                                   labels={'security-sensitive': True})
        self.assertEqual(ensure_focused_qa_for_item(item, trigger='status_transition'), [])
        self.assertEqual(self._qa_count(), 0)

    def test_multi_label_finding_coalesces_to_one_task(self):
        # PLS-571: eligibility requires typed live-testable evidence — the
        # fixture is development work (task.bug) carrying three risk labels.
        item = Item.objects.create(project=self.project, title='real auth+ui dev task',
                                   status='done', item_type='task.bug',
                                   labels={'auth': True, 'user-facing': True, 'security-sensitive': True})
        created = ensure_focused_qa_for_item(item, trigger='status_transition')
        self.assertEqual(len(created), 1)
        self.assertEqual(self._qa_count(), 1)
        qa = created[0]
        # primary label wins the identity; all matched labels ride along.
        self.assertEqual(qa.metadata['focused_qa_risk_label'], 'auth')
        self.assertEqual(set(qa.metadata['focused_qa_risk_labels']),
                         {'auth', 'user-facing', 'security-sensitive'})
        for lbl in ('auth', 'user-facing', 'security-sensitive'):
            self.assertIn(lbl, qa.tags)
        # re-trigger stays at one task (dedupe on primary+origin+env).
        ensure_focused_qa_for_item(item, trigger='status_transition')
        self.assertEqual(self._qa_count(), 1)

    def test_later_requires_live_qa_label_does_not_mint_a_sibling(self):
        """Origin first labelled data-migration, then also requires-live-qa.

        `_primary_risk_label` ranks requires-live-qa first, so the second
        trigger used to miss the open data-migration task and mint a sibling
        (live 2026-08-29 source-flood: focused-qa-automation).
        """
        # PLS-571 eligibility contract: risk labels prioritize live-testable
        # work but never make an otherwise non-live artifact eligible, so the
        # fixture carries typed implementation identity (task.bug) alongside
        # the labels. The sibling-prevention contract under test is unchanged.
        item = Item.objects.create(
            project=self.project, title='migrate then live-qa',
            status='done', item_type='task.bug',
            labels={'data-migration': True},
        )
        first = ensure_focused_qa_for_item(item, trigger='status_transition')
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].metadata['focused_qa_risk_label'], 'data-migration')
        item.labels = {'data-migration': True, 'requires-live-qa': True}
        item.save(update_fields=['labels'])
        second = ensure_focused_qa_for_item(item, trigger='status_transition')
        self.assertEqual(len(second), 1)
        self.assertEqual(self._qa_count(), 1)
        qa = second[0]
        self.assertEqual(qa.pk, first[0].pk)
        self.assertEqual(set(qa.metadata['focused_qa_risk_labels']),
                         {'data-migration', 'requires-live-qa'})
        self.assertEqual(qa.metadata['focused_qa_risk_label'], 'requires-live-qa')

    def test_preexisting_origin_siblings_are_cancelled_on_ensure(self):
        """Historical sequential-label extras must not stay open next trigger."""
        # PLS-571 eligibility contract: typed implementation identity required
        # for the spawn-expecting fixtures (risk labels alone never establish
        # eligibility). Sibling-prevention contract under test is unchanged.
        item = Item.objects.create(
            project=self.project, title='already has two qa tasks',
            status='done', item_type='task.bug',
            labels={'data-migration': True, 'requires-live-qa': True},
        )
        keeper = ensure_focused_qa_for_item(item, trigger='status_transition')[0]
        sibling = Item.objects.create(
            project=self.project,
            item_key=self.project.get_next_item_key(),
            title='[Focused QA:data-migration] leftover sibling',
            item_type='task.focused_qa',
            source='focused-qa-automation',
            status='todo',
            workflow_name='focused-qa',
            metadata={
                'focused_qa_origin_item_id': str(item.pk),
                'focused_qa_environment': 'live',
                'focused_qa_risk_label': 'data-migration',
            },
        )
        ensure_focused_qa_for_item(item, trigger='status_transition')
        sibling.refresh_from_db()
        keeper.refresh_from_db()
        self.assertEqual(sibling.status, 'cancelled')
        self.assertNotEqual(keeper.status, 'cancelled')
        open_qa = Item.objects.filter(
            source='focused-qa-automation',
        ).exclude(status__in=('cancelled', 'done', 'closed'))
        self.assertEqual(open_qa.count(), 1)
        self.assertEqual(open_qa.get().pk, keeper.pk)

    def test_generated_focused_qa_task_does_not_spawn_recursive_qa(self):
        qa = Item.objects.create(
            project=self.project,
            item_key=self.project.get_next_item_key(),
            title='[Focused QA:security-sensitive] HIVE-341 — verify isolation guard (live)',
            item_type='task.focused_qa',
            source='focused-qa-automation',
            source_id='hive-341:security-sensitive:live',
            workflow_name='reviewed-task',
            status='todo',
            assignment_group='qa',
            labels={'risk_label': 'security-sensitive', 'source': 'focused-qa'},
            tags='focused-qa,security-sensitive,live',
            metadata={
                'focused_qa_origin_item_id': '341',
                'focused_qa_origin_item_key': 'HIVE-341',
                'focused_qa_risk_label': 'security-sensitive',
                'focused_qa_environment': 'live',
            },
        )
        before = self._qa_count()

        created = ensure_focused_qa_for_item(qa, trigger='label_update')

        self.assertEqual(created, [])
        self.assertEqual(self._qa_count(), before)

    def test_reviewed_task_focused_qa_status_transition_does_not_spawn_recursive_qa(self):
        # PLS-370 repro: CTX-309 was itself focused-QA work in the
        # reviewed-task workflow. Its status_transition must not create
        # CTX-338/339-style recursive focused-QA children even though it carries
        # live risk labels.
        qa = Item.objects.create(
            project=self.project,
            item_key='CTX-309',
            title='[Focused QA:infra-control-plane] CTX-307 — Run CTX-292 §9 clean-window acceptance remeasure (live fleet metrics) and post numbers (live)',
            workflow_name='reviewed-task',
            status='done',
            assignment_group='qa',
            created_by_email='qa-automation@shizuha.com',
            labels={'source': 'focused-qa', 'risk_label': 'infra-control-plane', 'environment': 'live'},
            tags='focused-qa,infra-control-plane,live,user-facing',
        )
        before = self._qa_count()

        created = ensure_focused_qa_for_item(qa, trigger='status_transition')

        self.assertEqual(created, [])
        self.assertEqual(self._qa_count(), before)

    def test_manual_focused_qa_verification_task_does_not_spawn_qa(self):
        item = Item.objects.create(
            project=self.project,
            item_key=self.project.get_next_item_key(),
            title='[Focused QA][HIVE-326] Verify claw namespace isolation drift guard',
            workflow_name='reviewed-task',
            status='todo',
            assignment_group='qa',
            labels={'security-sensitive': True},
            tags='focused-qa,security-sensitive,live',
        )

        created = ensure_focused_qa_for_item(item, trigger='label_update')

        self.assertEqual(created, [])
        self.assertEqual(self._qa_count(), 0)


    def test_qa_automation_reviewed_task_artifact_does_not_spawn_qa(self):
        item = Item.objects.create(
            project=self.project,
            item_key=self.project.get_next_item_key(),
            title='Verify auth rollout after deploy',
            workflow_name='reviewed-task',
            status='todo',
            assignment_group='qa',
            created_by_email='qa-automation@shizuha.com',
            labels={'auth': True, 'security-sensitive': True},
        )

        created = ensure_focused_qa_for_item(item, trigger='label_update')

        self.assertEqual(created, [])
        self.assertEqual(self._qa_count(), 0)

    def test_plain_reviewed_task_without_qa_identity_can_spawn_qa(self):
        # PLS-571: a plain reviewed-task item spawns QA only when it carries
        # typed live-testable evidence (task.bug) — the eligibility gate.
        item = Item.objects.create(
            project=self.project,
            item_key=self.project.get_next_item_key(),
            title='Reviewed auth rollout after deploy',
            workflow_name='reviewed-task',
            status='done',
            item_type='task.bug',
            assignment_group='engineering',
            labels={'auth': True, 'security-sensitive': True},
        )

        created = ensure_focused_qa_for_item(item, trigger='status_transition')

        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].metadata['focused_qa_origin_item_key'], item.item_key)

    def test_focused_qa_source_prefix_artifact_does_not_spawn_qa(self):
        item = Item.objects.create(
            project=self.project,
            item_key=self.project.get_next_item_key(),
            title='QA failure filed from focused QA',
            source='focused-qa-failure',
            status='needs_triage',
            labels={'user-facing': True},
        )

        created = ensure_focused_qa_for_item(item, trigger='label_update')

        self.assertEqual(created, [])
        self.assertEqual(self._qa_count(), 0)

    def test_deployment_event_does_not_spawn_from_focused_qa_source_task(self):
        source = Item.objects.create(
            project=self.project,
            item_key=self.project.get_next_item_key(),
            title='[Focused QA][HIVE-326] Verify claw namespace isolation drift guard',
            workflow_name='reviewed-task',
            status='done',
            assignment_group='qa',
            labels={'security-sensitive': True},
            tags='focused-qa,security-sensitive,live',
        )

        upsert_deployment_event({
            'project_key': 'PLS',
            'repo': 'shizuha-labs/hive',
            'service': 'hive',
            'environment': 'live',
            'cluster': 'shizuha-k3s',
            'commit_sha': 'abcdef1234567890',
            'image_digest': 'sha256:focusedqa',
            'pipeline_run_id': 'run-focused-qa',
            'state': 'smoke_passed',
            'smoke_result': 'passed',
            'source_task_keys': [source.item_key],
            'metadata': {'risk_labels': ['security-sensitive']},
        })

        self.assertEqual(self._qa_count(), 0)

    def test_deploy_verify_process_task_does_not_spawn_qa(self):
        item = Item.objects.create(
            project=self.project,
            title='Deploy-verify PLS-779 (shizuha-tasks#255): record DeploymentEvent + QA verdict, then close',
            status='done',
            workflow_name='simple',
            labels={'requires-live-qa': True, 'user-facing': True},
        )
        self.assertEqual(ensure_focused_qa_for_item(item, trigger='status_transition'), [])
        self.assertEqual(self._qa_count(), 0)

    def test_formal_review_process_task_does_not_spawn_qa(self):
        item = Item.objects.create(
            project=self.project,
            title='Formal review of forge#17 @ 72dd2fdc (PLAT-5807, no verdict at head)',
            status='done',
            workflow_name='simple',
            labels={'auth': True},
        )
        self.assertEqual(ensure_focused_qa_for_item(item, trigger='status_transition'), [])
        self.assertEqual(self._qa_count(), 0)

    def test_reapprove_process_task_does_not_spawn_qa(self):
        item = Item.objects.create(
            project=self.project,
            title='Re-approve shizuha-tasks#335 at current head 9fde46f6 (head moved after approval)',
            status='done',
            workflow_name='simple',
            labels={'user-facing': True},
        )
        self.assertEqual(ensure_focused_qa_for_item(item, trigger='status_transition'), [])
        self.assertEqual(self._qa_count(), 0)

    def test_waiver_mint_process_task_does_not_spawn_qa(self):
        """PLAT-7600 follow-up (live repro FRG-68): waiver-mint executor tasks
        are process-meta bookkeeping — the receipt lands on the waiver TARGET,
        the mint task has no deployable artifact, and QA-of-QA on it could
        only close via waiver-for-waiver recursion."""
        for title in (
            'Mint role-gated deploy-verify waiver for FRG-61 '
            '(no identifiable merged head; live fix verified — endpoint call documented)',
            'Mint deploy-verify waiver for HIVE-1401 '
            '(test-discovery artifact, DeploymentEvent recorded) and close',
            'Mint deploy-verify waiver for PLS-619 '
            '(CI-only artifact, DeploymentEvent recorded) and close',
        ):
            item = Item.objects.create(
                project=self.project,
                title=title,
                status='done',
                workflow_name='simple',
                labels={'auth': True},
            )
            self.assertEqual(
                ensure_focused_qa_for_item(item, trigger='status_transition'),
                [],
                title,
            )
        self.assertEqual(self._qa_count(), 0)

    def test_waiver_endpoint_builder_task_still_spawns_qa(self):
        """Negative control: building the mint endpoint is real code on a live
        surface — the imperative 'Mint …' anchor must not suppress its QA."""
        from tasks.models import Resolution
        fixed, _ = Resolution.objects.get_or_create(slug='fixed', defaults={'name': 'Fixed'})
        item = Item.objects.create(
            project=self.project,
            title='Add role-gated deploy-verify waiver mint endpoint',
            status='done',
            item_type='task.bug',
            resolution=fixed,
            labels={'auth': True},
        )
        created = ensure_focused_qa_for_item(item, trigger='status_transition')
        self.assertEqual(len(created), 1)
        self.assertEqual(self._qa_count(), 1)

    def test_preexisting_qa_on_process_origin_is_cancelled(self):
        item = Item.objects.create(
            project=self.project,
            title='Deploy-verify CTX-165 (cortex#141): record DeploymentEvent + QA verdict, then close',
            status='done',
            workflow_name='simple',
            labels={'requires-live-qa': True},
        )
        leftover = Item.objects.create(
            project=self.project,
            item_key=self.project.get_next_item_key(),
            title='[Focused QA:requires-live-qa] leftover process-meta QA',
            item_type='task.focused_qa',
            source='focused-qa-automation',
            status='todo',
            workflow_name='focused-qa',
            metadata={
                'focused_qa_origin_item_id': str(item.pk),
                'focused_qa_environment': 'live',
                'focused_qa_risk_label': 'requires-live-qa',
            },
        )
        self.assertEqual(ensure_focused_qa_for_item(item, trigger='status_transition'), [])
        leftover.refresh_from_db()
        self.assertEqual(leftover.status, 'cancelled')
        self.assertEqual(
            Item.objects.filter(source='focused-qa-automation')
            .exclude(status__in=('cancelled', 'done', 'closed'))
            .count(),
            0,
        )

    def test_fixed_live_surface_finding_still_spawns_one(self):
        # positive control: a genuine fix on a live surface still gets QA.
        # PLS-571: eligibility requires typed live-testable evidence — the
        # fixture is development work (task.bug), not a bare risk label.
        from tasks.models import Resolution
        fixed, _ = Resolution.objects.get_or_create(slug='fixed', defaults={'name': 'Fixed'})
        item = Item.objects.create(project=self.project, title='real auth fix',
                                   status='done', resolution=fixed,
                                   item_type='task.bug', labels={'auth': True})
        created = ensure_focused_qa_for_item(item, trigger='status_transition')
        self.assertEqual(len(created), 1)
        self.assertEqual(self._qa_count(), 1)

    def test_pls557_shaped_design_review_spawns_no_qa(self):
        """PLS-571 acceptance regression (PLS-557 → invalid child PLS-570).

        A non-QA reviewed-task HLD review carrying a high-risk label and
        explicit no-pr/no-live-mutation scope, with no deployment event and
        no implementation artifact, must produce ZERO focused-QA children on
        any trigger — the risk label alone never establishes eligibility.
        """
        item = Item.objects.create(
            project=self.project,
            item_key=self.project.get_next_item_key(),
            title='DevOps/SRE HLD review — exact-version refreeze',
            workflow_name='reviewed-task',
            status='done',
            labels={'data-migration': True, 'no-pr': True, 'no-live-mutation': True},
        )
        for trigger in ('label_update', 'status_transition'):
            created = ensure_focused_qa_for_item(item, trigger=trigger)
            self.assertEqual(created, [], trigger)
        self.assertEqual(self._qa_count(), 0)

    def test_design_review_without_scope_label_still_spawns_no_qa(self):
        """PLS-571 companion: even WITHOUT the explicit no-live-mutation
        label, a pure reviewed-task design/sign-off artifact with only a
        risk label has no live-testable change boundary — zero children.
        The typed-evidence requirement is the gate, not the scope label.
        """
        item = Item.objects.create(
            project=self.project,
            item_key=self.project.get_next_item_key(),
            title='Design sign-off — migration plan review',
            workflow_name='reviewed-task',
            status='done',
            labels={'data-migration': True},
        )
        for trigger in ('label_update', 'status_transition'):
            created = ensure_focused_qa_for_item(item, trigger=trigger)
            self.assertEqual(created, [], trigger)
        self.assertEqual(self._qa_count(), 0)

    def test_reviewed_operational_check_with_explicit_live_target_spawns_qa(self):
        """PLS-571 positive control (acceptance bullet 3, second half):
        reviewed operational work that explicitly carries a valid live
        target stays QA-eligible — the predicate preserves the positive
        control while refusing label-only artifacts.
        """
        item = Item.objects.create(
            project=self.project,
            item_key=self.project.get_next_item_key(),
            title='Reviewed ops check — verify migrated data read/write',
            workflow_name='reviewed-task',
            status='done',
            assignment_group='engineering',
            labels={'data-migration': True},
            metadata={'live_target': 'https://scs.shizuha.com/api/hosting/quota/'},
        )
        created = ensure_focused_qa_for_item(item, trigger='label_update')
        self.assertEqual(len(created), 1)
        self.assertEqual(self._qa_count(), 1)

    def test_kill_switch_disables_spawning(self):
        from unittest.mock import patch
        item = Item.objects.create(project=self.project, title='auth fix',
                                   status='done', labels={'auth': True})
        with patch.dict('os.environ', {'PULSE_FOCUSED_QA_ENABLED': '0'}):
            self.assertEqual(ensure_focused_qa_for_item(item, trigger='status_transition'), [])
        self.assertEqual(self._qa_count(), 0)
