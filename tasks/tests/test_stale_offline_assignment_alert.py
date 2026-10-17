from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from tasks.models import Comment, Item, ItemLink, Project, TeamRoster
from tasks.tasks import sweep_stale_offline_assignments


class StaleOfflineAssignmentAlertTests(TestCase):
    def _register_nori(self, team_name):
        TeamRoster.objects.update_or_create(
            organization_id=1,
            team_name=team_name,
            defaults={
                'organization_slug': 'shizuha',
                'tiers': [['nori@shizuha.com']],
                'is_active': True,
            },
        )

    def test_sweep_uses_active_queue_predicate_for_implementation_linked_rfc(self):
        """PLS-445: queue-suppressed RFCs cannot produce stale-agent blame."""
        self._register_nori('architecture')
        project = Project.objects.create(name='Pulse', key='PLS445', org_id=1)
        source = Item.objects.create(
            mode='task',
            item_type='task.generic',
            title='RFC waiting for implementation',
            description='Implementation follow-up PLS445-2 owns the active work.',
            item_key='PLS445-1',
            project=project,
            status='rfc_implementation_linked',
            workflow_name='feature-request-rfc',
            assignment_group='architecture',
            assignee_email='nori@shizuha.com',
        )
        followup = Item.objects.create(
            mode='task',
            item_type='task.generic',
            title='Active implementation',
            item_key='PLS445-2',
            project=project,
            status='open',
            workflow_name='autonomous-dev',
        )
        # 37c0c9a removed the free-text `description__icontains=F(item_key)` branch
        # from filter_active_queue_items (~14s Postgres JIT per is_active query), so
        # the parking relationship must be a TYPED implementation link
        # (IMPLEMENTATION_LINK_TYPES / PLS-189 / PLS-445), not a generic relation.
        ItemLink.objects.create(
            source_item=source,
            target_item=followup,
            link_type='implemented_by',
            created_by_id=0,
            created_by_email='system@pulse',
        )
        Item.objects.filter(id=source.id).update(
            updated_at=timezone.now() - timedelta(hours=30)
        )

        with mock.patch(
            'tasks.views._scrape_agent_health',
            return_value={'nori': {'status': 'offline'}},
        ):
            suppressed = sweep_stale_offline_assignments.apply(
                kwargs={'hours': 24}
            ).get()

        self.assertEqual(suppressed['alerted'], 0)
        self.assertFalse(
            Comment.objects.filter(
                item=source, content__contains='stale-assignment alert'
            ).exists()
        )

        # Once the implementation artifact releases, the same RFC reappears in
        # the default queue and therefore becomes eligible for stale monitoring.
        followup.status = 'done'
        followup.save(update_fields=['status'])
        with mock.patch(
            'tasks.views._scrape_agent_health',
            return_value={'nori': {'status': 'offline'}},
        ):
            released = sweep_stale_offline_assignments.apply(
                kwargs={'hours': 24}
            ).get()

        self.assertEqual(released['alerted'], 1)
        self.assertTrue(
            Comment.objects.filter(
                item=source, content__contains='stale-assignment alert'
            ).exists()
        )

    def test_sweep_comments_source_without_creating_ghost_alert_item(self):
        """PLS-160: the sweep comments on the stale SOURCE task only — it must NOT create
        a separate keyless alert Item (those surfaced as N/A 'ghost' tasks and flooded
        CoS). One source-task comment, once-per-cycle idempotency marker, no extra Item."""
        self._register_nori('engineering')
        project = Project.objects.create(name='Cortex', key='CTX', org_id=1)
        source = Item.objects.create(
            mode='task',
            item_type='task.generic',
            title='Stuck work',
            item_key='CTX-1',
            project=project,
            status='open',
            workflow_name='autonomous-dev',
            assignment_group='engineering',
            assignee_email='nori@shizuha.com',
        )
        Item.objects.filter(id=source.id).update(updated_at=timezone.now() - timedelta(hours=30))
        item_count_before = Item.objects.count()

        with mock.patch('tasks.views._scrape_agent_health', return_value={'nori': {'status': 'offline'}}):
            result = sweep_stale_offline_assignments.apply(kwargs={'hours': 24}).get()

        self.assertEqual(result['alerted'], 1)
        # PLS-160: NO separate alert Item created (no ghosts, no CoS flood).
        self.assertEqual(Item.objects.count(), item_count_before)
        self.assertFalse(Item.objects.filter(metadata__source='stale_offline_assignment_sweep').exists())
        source.refresh_from_db()
        # Idempotency marker set; the removed alert_item_id is gone.
        self.assertEqual(source.metadata.get('stale_offline_assignment_alert'), 'nori:24h')
        self.assertIsNone(source.metadata.get('stale_offline_assignment_alert_item_id'))
        # Staleness is surfaced as a comment on the actual stuck task.
        self.assertTrue(Comment.objects.filter(item=source, content__contains='stale-assignment alert').exists())

    def test_sweep_idempotent_no_duplicate_comment_second_run(self):
        """PLS-160: a second sweep within the same cycle must not re-comment (idempotent)."""
        self._register_nori('engineering')
        project = Project.objects.create(name='Cortex', key='CTXI', org_id=1)
        source = Item.objects.create(
            mode='task', item_type='task.generic', title='Stuck', item_key='CTXI-1',
            project=project, status='open', workflow_name='autonomous-dev',
            assignment_group='engineering', assignee_email='nori@shizuha.com',
        )
        Item.objects.filter(id=source.id).update(updated_at=timezone.now() - timedelta(hours=30))
        with mock.patch('tasks.views._scrape_agent_health', return_value={'nori': {'status': 'offline'}}):
            r1 = sweep_stale_offline_assignments.apply(kwargs={'hours': 24}).get()
            r2 = sweep_stale_offline_assignments.apply(kwargs={'hours': 24}).get()
        self.assertEqual(r1['alerted'], 1)
        self.assertEqual(r2['alerted'], 0)  # skipped via idempotency marker
        self.assertEqual(Comment.objects.filter(item=source, content__contains='stale-assignment alert').count(), 1)

    def test_sweep_skips_backlog_source_tasks(self):
        """PLS-125: backlog tasks must not generate stale-assignment alerts (PLS-125)."""
        self._register_nori('engineering')
        project = Project.objects.create(name='Cortex', key='CTX2', org_id=1)
        source = Item.objects.create(
            mode='task',
            item_type='task.generic',
            title='Parked work',
            item_key='CTX2-1',
            project=project,
            status='backlog',
            workflow_name='autonomous-dev',
            assignment_group='engineering',
            assignee_email='nori@shizuha.com',
        )
        Item.objects.filter(id=source.id).update(updated_at=timezone.now() - timedelta(hours=30))

        with mock.patch('tasks.views._scrape_agent_health', return_value={'nori': {'status': 'offline'}}):
            result = sweep_stale_offline_assignments.apply(kwargs={'hours': 24}).get()

        self.assertEqual(result['alerted'], 0)
        source.refresh_from_db()
        self.assertIsNone(source.metadata.get('stale_offline_assignment_alert_item_id'))

    def test_sweep_judges_fire_time_state_not_selection_snapshot(self):
        """PLAT-8169 arm (b): a disposition landing between queryset selection and
        fire must suppress the alert. PLAT-7787's alert arrived after its Deploy
        Verified close because the sweep blamed the selection-time row. Here the
        settled task is closed while the sweep is mid-iteration (simulated via a
        health-dict side effect), so the in-memory row still looks stale — the
        fire-time re-fetch must see the close and suppress ONLY that row, while
        the genuinely stale sibling still alerts."""
        self._register_nori('engineering')
        project = Project.objects.create(name='Cortex', key='CTX3', org_id=1)
        stale = Item.objects.create(
            mode='task', item_type='task.generic', title='Genuinely stuck',
            item_key='CTX3-1', project=project, status='open',
            workflow_name='autonomous-dev', assignment_group='engineering',
            assignee_email='nori@shizuha.com',
        )
        settled = Item.objects.create(
            mode='task', item_type='task.generic', title='Closed mid-sweep',
            item_key='CTX3-2', project=project, status='open',
            workflow_name='autonomous-dev', assignment_group='engineering',
            assignee_email='nori@shizuha.com',
        )
        Item.objects.filter(id__in=[stale.id, settled.id]).update(
            updated_at=timezone.now() - timedelta(hours=30)
        )

        class MidSweepDisposition(dict):
            """Closes `settled` on the first agent lookup — the disposition lands
            after both rows were selected but before the sweep fires."""

            def __init__(self, *args, victim_pk, **kwargs):
                super().__init__(*args, **kwargs)
                self._victim_pk = victim_pk
                self._fired = False

            def get(self, key):
                if not self._fired:
                    Item.objects.filter(pk=self._victim_pk).update(
                        status='done', updated_at=timezone.now(),
                    )
                    self._fired = True
                return super().get(key)

        health = MidSweepDisposition(
            {'nori': {'status': 'offline'}}, victim_pk=settled.pk,
        )
        with mock.patch('tasks.views._scrape_agent_health', return_value=health):
            result = sweep_stale_offline_assignments.apply(kwargs={'hours': 24}).get()

        # The genuinely stale task still alerts; the mid-sweep-settled one does not.
        self.assertEqual(result['alerted'], 1)
        self.assertTrue(
            Comment.objects.filter(item=stale, content__contains='stale-assignment alert').exists()
        )
        self.assertFalse(
            Comment.objects.filter(item=settled, content__contains='stale-assignment alert').exists()
        )
        settled.refresh_from_db()
        self.assertIsNone(settled.metadata.get('stale_offline_assignment_alert'))
