from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from tasks.authentication import FederatedUser
from tasks.models import AiGenerationRun, Comment, Item, ItemActivity, Project, Sprint
from tasks.views import AiRecapDetailView, AiRecapFeedbackView, AiRecapListCreateView


@override_settings(PULSE_HEALTH_EXPORTER_URL='disabled', PULSE_API_HEALTH_URL='disabled')
class PLS307AiRecapTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.user = FederatedUser(
            1001,
            email='recap-user@shizuha.com',
            username='recap-user',
            organization_memberships={1: 'member'},
        )
        self.other_user = FederatedUser(
            2002,
            email='outsider@shizuha.com',
            username='outsider',
            organization_memberships={2: 'member'},
        )
        self.project = Project.objects.create(name='Pulse', key='PLS', org_id=1, access_level='open')
        self.foreign_project = Project.objects.create(name='Hive', key='HIVE', org_id=2, access_level='open')
        self.sprint = Sprint.objects.create(project=self.project, name='July hardening')
        self.item = Item.objects.create(
            project=self.project,
            sprint=self.sprint,
            item_key='PLS-307',
            mode='task',
            workflow_name='autonomous-dev',
            status='in_progress',
            priority='high',
            title='AI sprint recaps need citations',
            created_by_id=1001,
            created_by_email='recap-user@shizuha.com',
            metadata={'pull_requests': [{'url': 'https://github.com/shizuha-labs/shizuha-tasks/pull/999'}]},
        )
        Comment.objects.create(
            item=self.item,
            author_id=1001,
            author_email='recap-user@shizuha.com',
            content='Review asked for source citations on every recap bullet.',
        )

    def _post_recap(self, user=None, payload=None):
        now = timezone.now()
        body = {
            'project_id': self.project.id,
            'sprint_id': self.sprint.id,
            'type': 'sprint',
            'source_window_start': (now - timedelta(hours=2)).isoformat(),
            'source_window_end': (now + timedelta(minutes=1)).isoformat(),
            'model': 'deterministic-pulse-recap-v1',
            'client_id': 'unit-test',
        }
        if payload:
            body.update(payload)
        request = self.factory.post('/api/ai/recaps/', body, format='json')
        force_authenticate(request, user=user or self.user)
        with patch('tasks.tasks.send_item_notification.delay', return_value=None), \
             patch('tasks.tasks.notify_comment_mentions.delay', return_value=None):
            return AiRecapListCreateView.as_view()(request)

    def test_create_sprint_recap_persists_citation_metadata_and_visible_output(self):
        response = self._post_recap()

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['status'], 'completed')
        self.assertEqual(response.data['prompt_template_version'], 'pulse-recap-v1')
        self.assertEqual(len(response.data['source_snapshot_hash']), 64)
        self.assertGreaterEqual(response.data['source_counts']['items'], 1)
        bullet_text = ' '.join(b['text'] for b in response.data['output']['bullets'])
        self.assertIn('PLS-307', bullet_text)
        self.assertTrue(any(c['kind'] == 'task' and c['item_key'] == 'PLS-307' for c in response.data['citations']))
        self.assertTrue(any(c['kind'] == 'pull_request' and c.get('url', '').endswith('/999') for c in response.data['citations']))

        request = self.factory.get(f"/api/ai/recaps/{response.data['id']}/")
        force_authenticate(request, user=self.user)
        detail = AiRecapDetailView.as_view()(request, run_id=response.data['id'])
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.data['id'], response.data['id'])

    def test_source_collection_includes_stale_items_with_in_window_comment_or_activity(self):
        """revi P1: recap windows must collect comments/activities even when the
        parent task itself was not created/updated/completed during the window."""
        from tasks.services import ai_recaps

        now = timezone.now()
        start = now - timedelta(hours=1)
        end = now + timedelta(minutes=1)
        old_time = now - timedelta(days=7)
        commented = Item.objects.create(
            project=self.project,
            sprint=self.sprint,
            item_key='PLS-OLD-COMMENT',
            mode='task',
            workflow_name='autonomous-dev',
            status='in_progress',
            priority='normal',
            title='Old task with new review comment',
            created_by_id=1001,
            created_by_email='recap-user@shizuha.com',
        )
        activity_only = Item.objects.create(
            project=self.project,
            sprint=self.sprint,
            item_key='PLS-OLD-ACTIVITY',
            mode='task',
            workflow_name='autonomous-dev',
            status='in_progress',
            priority='normal',
            title='Old task with new transition activity',
            created_by_id=1001,
            created_by_email='recap-user@shizuha.com',
        )
        Item.objects.filter(id__in=[commented.id, activity_only.id]).update(
            created_at=old_time,
            updated_at=old_time,
            completed_at=None,
        )
        comment = Comment.objects.create(
            item=commented,
            author_id=1001,
            author_email='recap-user@shizuha.com',
            content='New evidence on an otherwise stale task.',
        )
        activity = ItemActivity.objects.create(
            item=activity_only,
            action='status_changed',
            field_name='status',
            old_value='open',
            new_value='in_progress',
            user_id=1001,
            user_email='recap-user@shizuha.com',
        )

        sources = ai_recaps._collect_sources(self.project, start, end, self.sprint)

        item_ids = {item.id for item in sources['items']}
        self.assertIn(commented.id, item_ids)
        self.assertIn(activity_only.id, item_ids)
        self.assertIn(comment.id, {row.id for row in sources['comments']})
        self.assertIn(activity.id, {row.id for row in sources['activities']})

    def test_project_scope_blocks_cross_tenant_recap_creation_and_read(self):
        response = self._post_recap(user=self.other_user)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(AiGenerationRun.objects.exists())

        created = self._post_recap()
        request = self.factory.get(f"/api/ai/recaps/{created.data['id']}/")
        force_authenticate(request, user=self.other_user)
        denied = AiRecapDetailView.as_view()(request, run_id=created.data['id'])
        self.assertEqual(denied.status_code, 403)

    def test_window_bounds_and_rate_limit_fail_closed(self):
        now = timezone.now()
        response = self._post_recap(payload={
            'source_window_start': (now - timedelta(days=32)).isoformat(),
            'source_window_end': now.isoformat(),
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn('31 days', response.data['detail'])

        for _ in range(10):
            ok = self._post_recap()
            self.assertEqual(ok.status_code, 201)
        limited = self._post_recap()
        self.assertEqual(limited.status_code, 400)
        self.assertIn('rate limit', limited.data['detail'])

    def _make_queued_run(self):
        now = timezone.now()
        return AiGenerationRun.objects.create(
            org_id=self.project.org_id,
            project=self.project,
            sprint=self.sprint,
            type='sprint',
            status='queued',
            model='deterministic-pulse-recap-v1',
            prompt_template_version='pulse-recap-v1',
            source_window_start=now - timedelta(hours=2),
            source_window_end=now,
            created_by_id=self.user.id,
            created_by_email=self.user.email,
            client_id='unit-test',
        )

    def test_failed_generation_persists_durable_failed_state(self):
        """kei P1: a failure inside generate_recap_run must leave the row durably
        `failed` with an error message — the failed-state write survives the
        rollback of the generation transaction (separate-transaction persist)."""
        from tasks.services import ai_recaps

        run = self._make_queued_run()
        with patch('tasks.services.ai_recaps._render_output', side_effect=RuntimeError('boom-render')):
            with self.assertRaises(RuntimeError):
                ai_recaps.generate_recap_run(run.id)

        run.refresh_from_db()
        self.assertEqual(run.status, 'failed')
        self.assertIn('boom-render', run.error_message)

    def test_celery_task_path_leaves_run_failed(self):
        """kei P1: the Celery wrapper path must also leave the row durably failed
        after generation raises, rather than stranded queued/running."""
        from tasks import tasks as celery_tasks

        run = self._make_queued_run()
        with patch('tasks.services.ai_recaps._render_output', side_effect=RuntimeError('boom-task')):
            try:
                celery_tasks.generate_ai_recap.apply(args=[str(run.id)])
            except Exception:
                pass

        run.refresh_from_db()
        self.assertEqual(run.status, 'failed')
        self.assertIn('boom-task', run.error_message)

    def test_stale_recap_sweep_files_and_resolves_fail_loud_task(self):
        """kei P2: the async recap mechanism has a named fail-loud path — a stuck
        queued/running run trips the liveness sweep, which files a task onto the
        engineering queue and self-resolves once the stall clears."""
        from tasks import tasks as celery_tasks
        from tasks.models import Item

        run = self._make_queued_run()
        # Force the run to look stuck (updated_at is auto_now; set it via UPDATE).
        AiGenerationRun.objects.filter(id=run.id).update(
            updated_at=timezone.now() - timedelta(minutes=90))

        celery_tasks.sweep_stale_ai_recaps.apply()
        filed = Item.objects.filter(assignment_group='engineering',
                                    metadata__ai_recap_signal='ai-recap-stall')
        self.assertEqual(filed.count(), 1)
        filed_item = filed.first()
        self.assertNotIn(filed_item.status, ('resolved', 'closed', 'done', 'completed'))

        # A second sweep while still stuck must not file a duplicate.
        celery_tasks.sweep_stale_ai_recaps.apply()
        self.assertEqual(filed.count(), 1)

        # Clear the stall → sweep auto-resolves the task.
        AiGenerationRun.objects.filter(id=run.id).update(status='completed')
        celery_tasks.sweep_stale_ai_recaps.apply()
        filed_item.refresh_from_db()
        self.assertIn(filed_item.status, ('resolved', 'done', 'completed', 'closed'))

    def test_feedback_endpoint_records_human_correction(self):
        created = self._post_recap()
        request = self.factory.post(
            f"/api/ai/recaps/{created.data['id']}/feedback/",
            {'verdict': 'corrected', 'comment': 'Clarify this as review feedback.', 'bullet_index': 0},
            format='json',
        )
        force_authenticate(request, user=self.user)
        response = AiRecapFeedbackView.as_view()(request, run_id=created.data['id'])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['feedback'][0]['verdict'], 'corrected')
        self.assertIn('review feedback', response.data['feedback'][0]['comment'])
