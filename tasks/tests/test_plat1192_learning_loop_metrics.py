"""PLAT-1192: monthly findings→mechanism conversion metric (PLAT-1180 deliverable c).

Acceptance fixture from the task: one finding with a linked mechanism, one
explicit N/A finding, and one stale (>30d open) prevention-mechanism task.
"""

from django.test import TestCase
from django.utils import timezone
from django_celery_beat.models import PeriodicTask

from tasks.models import Comment, Item, ItemActivity, ItemLink, Project
from tasks.services.learning_loop import (
    compute_learning_loop_metrics,
    format_learning_loop_block,
    previous_month_bounds,
)
from tasks.tasks import setup_notification_schedules


class LearningLoopMetricTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.start = self.now - timezone.timedelta(days=15)
        self.end = self.now + timezone.timedelta(days=1)

    def _task(self, title, *, labels=None, created_at=None, status='pending',
              workflow_name='autonomous-dev', **kw):
        item = Item.objects.create(
            title=title, mode='task', status=status,
            workflow_name=workflow_name, labels=labels or [], **kw,
        )
        if created_at is not None:
            Item.objects.filter(id=item.id).update(created_at=created_at)
            item.refresh_from_db()
        return item

    def test_acceptance_fixture_counts(self):
        """One finding+linked mechanism, one N/A finding, one stale mechanism."""
        finding = self._task('Retro finding: deploys skip verification',
                             labels=['retro-finding'])
        mechanism = self._task('Add post-deploy verification validator',
                               labels=['prevention-mechanism'])
        ItemLink.objects.create(source_item=finding, target_item=mechanism,
                                created_by_id=1, created_by_email='shizuha@shizuha.com',
                                link_type='relates_to')

        na_finding = self._task('Retro finding: one-off provider outage',
                                labels=['retro-finding'])
        Comment.objects.create(
            item=na_finding, author_id=1, author_email='shizuha@shizuha.com', author_name='shizuha',
            content='mechanism: N/A — external one-off, no durable mechanism possible')

        stale = self._task(
            'Old mechanism never shipped', labels=['prevention-mechanism'],
            status='in_progress',
            created_at=self.now - timezone.timedelta(days=45))

        m = compute_learning_loop_metrics(self.start, self.end, now=self.now)

        self.assertEqual(m['findings_total'], 2)
        self.assertEqual(m['retro_findings'], 2)
        self.assertEqual(m['incident_closes'], 0)
        self.assertEqual(m['mechanisms_linked'], 1)
        self.assertEqual(m['na_count'], 1)
        # conversion = linked / (total − N/A) = 1 / (2 − 1)
        self.assertEqual(m['conversion_rate'], 1.0)
        self.assertEqual(len(m['stale_mechanisms']), 1)
        self.assertEqual(m['stale_mechanisms'][0]['item_key'],
                         stale.item_key or f'#{stale.id}')
        self.assertGreaterEqual(m['stale_mechanisms'][0]['age_days'], 45)

    def test_incident_close_counts_as_finding(self):
        incident = self._task('SEV-2: pulse 500s', status='resolved')
        Item.objects.filter(id=incident.id).update(workflow_name='incident')
        incident.refresh_from_db()
        ItemActivity.objects.create(
            item=incident, action='status_changed',
            old_value='documenting', new_value='resolved',
            user_id=1, user_email='agent@shizuha.com')
        mechanism = self._task('Alert on pulse 5xx rate',
                               labels=['prevention-mechanism'])
        # Inward link direction (mechanism blocks incident) must also count.
        ItemLink.objects.create(source_item=mechanism, target_item=incident,
                                created_by_id=1, created_by_email='shizuha@shizuha.com',
                                link_type='blocks')

        m = compute_learning_loop_metrics(self.start, self.end, now=self.now)

        self.assertEqual(m['findings_total'], 1)
        self.assertEqual(m['incident_closes'], 1)
        self.assertEqual(m['mechanisms_linked'], 1)
        self.assertEqual(m['conversion_rate'], 1.0)

    def test_native_completed_close_counts_exactly_once(self):
        """PR #243 review P1 (ren): a status change TO the native 'completed'
        status is logged by signals with action='completed', not
        'status_changed' — it must still count as an incident close, once."""
        incident = self._task('SEV-3: native completed close', status='completed')
        Item.objects.filter(id=incident.id).update(workflow_name='incident')
        ItemActivity.objects.create(
            item=incident, action='completed',
            old_value='in_progress', new_value='completed',
            user_id=1, user_email='agent@shizuha.com')

        m = compute_learning_loop_metrics(self.start, self.end, now=self.now)

        self.assertEqual(m['findings_total'], 1)
        self.assertEqual(m['incident_closes'], 1)

    def test_close_outside_period_and_unlabeled_links_do_not_count(self):
        incident = self._task('Old incident', status='resolved')
        Item.objects.filter(id=incident.id).update(workflow_name='incident')
        act = ItemActivity.objects.create(
            item=incident, action='status_changed',
            old_value='open', new_value='resolved',
            user_id=1, user_email='agent@shizuha.com')
        ItemActivity.objects.filter(id=act.id).update(
            created_at=self.start - timezone.timedelta(days=10))

        finding = self._task('Retro finding: flaky CI', labels=['retro-finding'])
        plain = self._task('Some unlabeled follow-up')
        ItemLink.objects.create(source_item=finding, target_item=plain,
                                created_by_id=1, created_by_email='shizuha@shizuha.com',
                                link_type='relates_to')

        m = compute_learning_loop_metrics(self.start, self.end, now=self.now)

        self.assertEqual(m['findings_total'], 1)  # only the retro finding
        self.assertEqual(m['incident_closes'], 0)
        self.assertEqual(m['mechanisms_linked'], 0)
        self.assertEqual(m['na_count'], 0)
        self.assertEqual(m['conversion_rate'], 0.0)

    def test_conversion_rate_none_when_all_findings_na(self):
        na_finding = self._task('Retro finding: cosmic ray',
                                labels=['retro-finding'])
        Comment.objects.create(
            item=na_finding, author_id=1, author_email='shizuha@shizuha.com', author_name='shizuha',
            content='mechanism: N/A — not reproducible, nothing to gate')

        m = compute_learning_loop_metrics(self.start, self.end, now=self.now)

        self.assertEqual(m['findings_total'], 1)
        self.assertEqual(m['na_count'], 1)
        self.assertIsNone(m['conversion_rate'])

    def test_optional_base_queryset_excludes_second_org_and_projectless_rows(self):
        org1 = Project.objects.create(org_id=1, key='ORG1', name='Org 1')
        org2 = Project.objects.create(org_id=2, key='ORG2', name='Org 2')
        finding1 = self._task(
            'Org 1 finding', labels=['retro-finding'], project=org1)
        mechanism1 = self._task(
            'Org 1 mechanism', labels=['prevention-mechanism'], project=org1)
        ItemLink.objects.create(
            source_item=finding1, target_item=mechanism1,
            created_by_id=1, created_by_email='shizuha@shizuha.com',
            link_type='relates_to')
        finding2 = self._task(
            'Org 2 finding', labels=['retro-finding'], project=org2)
        mechanism2 = self._task(
            'Org 2 mechanism', labels=['prevention-mechanism'], project=org2)
        ItemLink.objects.create(
            source_item=finding2, target_item=mechanism2,
            created_by_id=1, created_by_email='shizuha@shizuha.com',
            link_type='relates_to')
        self._task('Projectless finding', labels=['retro-finding'])

        metrics = compute_learning_loop_metrics(
            self.start, self.end, now=self.now,
            base_items=Item.objects.filter(project__org_id=1),
        )

        self.assertEqual(metrics['findings_total'], 1)
        self.assertEqual(metrics['mechanisms_linked'], 1)
        self.assertEqual(metrics['mechanism_keys'], [mechanism1.item_key or f'#{mechanism1.id}'])

    def test_format_block_renders_rate_and_staleness(self):
        self._task('Stale mech', labels=['prevention-mechanism'],
                   status='pending',
                   created_at=self.now - timezone.timedelta(days=40))
        m = compute_learning_loop_metrics(self.start, self.end, now=self.now)
        block = format_learning_loop_block(m, 'Jun 2026')

        self.assertIn('Learning-loop conversion — Jun 2026', block)
        self.assertIn('n/a — no gated findings', block)
        self.assertIn('stale mechanisms (open >30d): 1', block)

    def test_previous_month_bounds(self):
        now = timezone.now().replace(year=2026, month=7, day=2)
        start, end = previous_month_bounds(now)
        self.assertEqual((start.year, start.month, start.day), (2026, 6, 1))
        self.assertEqual((end.year, end.month, end.day), (2026, 7, 1))
        self.assertEqual((start.hour, start.minute), (0, 0))

    def test_monthly_digest_beat_registered(self):
        PeriodicTask.objects.all().delete()
        setup_notification_schedules()
        task = PeriodicTask.objects.select_related('crontab').get(
            name='Post Monthly Org-Health Digest')
        self.assertEqual(task.task, 'tasks.tasks.post_monthly_org_health_digest')
        self.assertEqual(task.crontab.minute, '0')
        self.assertEqual(task.crontab.hour, '5')
        self.assertEqual(task.crontab.day_of_month, '1')
