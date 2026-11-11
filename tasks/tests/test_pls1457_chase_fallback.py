"""PLS-1457: chase auto-fallback waits for two prior silent rungs.

A bot PR status stamp must not count as assignee activity.
"""
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from tasks.models import Comment, Item, ItemActivity, Project, TeamRoster
from tasks.views import _maybe_chase_auto_fallback


class ChaseAutoFallbackTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name='Chase', key='CHS', org_id=1)
        # The assignment fence (Item.save -> WorkflowService.assignment_org_mismatch)
        # requires the assignee to sit in the task org's exact team roster. Migration
        # 0055 already seeds a shizuha/engineering roster into every fresh test
        # database, so update_or_create is required here — a blind objects.create
        # collides with uniq_team_roster_org_name. Overwrite tiers/is_active so the
        # fence sees BOTH the original assignee and the fallback target.
        TeamRoster.objects.update_or_create(
            organization_slug='shizuha',
            team_name='engineering',
            defaults={
                'organization_id': 1,
                'tiers': [['old@shizuha.com', 'next@shizuha.com']],
                'junior_emails': [],
                'is_active': True,
            },
        )
        self.item = Item.objects.create(
            mode='task',
            title='silent assignee',
            project=self.project,
            item_key='CHS-1',
            status='in_progress',
            assignee_email='old@shizuha.com',
            assignment_group='engineering',
            created_by_id=1,
            metadata={
                'pull_requests': [{
                    'url': 'https://origin.shizuha.com/shizuha-labs/pulse/pulls/1',
                    'updated_at': (timezone.now() + timedelta(hours=1)).isoformat(),
                    'head_sha_at': (timezone.now() + timedelta(hours=1)).isoformat(),
                }],
            },
        )

    def _chase(self, when):
        row = Comment.objects.create(
            item=self.item,
            content='chase: still no movement',
            author_email='reviewer@shizuha.com',
            author_id=2,
        )
        Comment.objects.filter(pk=row.pk).update(created_at=when)
        row.refresh_from_db()
        return row

    @patch('tasks.services.workflow_service.WorkflowService._resolve_team_assignee')
    def test_one_prior_silent_rung_does_not_reassign(self, resolve):
        resolve.return_value = (9, 'next@shizuha.com')
        now = timezone.now()
        self._chase(now - timedelta(hours=3))
        current = self._chase(now)
        self.assertIsNone(_maybe_chase_auto_fallback(self.item, current))
        resolve.assert_not_called()
        self.item.refresh_from_db()
        self.assertEqual(self.item.assignee_email, 'old@shizuha.com')

    @patch('tasks.services.workflow_service.WorkflowService._resolve_team_assignee')
    def test_two_prior_silent_rungs_reassign_despite_bot_pr_stamp(self, resolve):
        resolve.return_value = (9, 'next@shizuha.com')
        now = timezone.now()
        # Bot-shaped PR activity: an ItemActivity whose actor is not the assignee.
        ItemActivity.objects.create(
            item=self.item,
            action='pr_status_changed',
            user_id=0,
            user_email='github-webhook@shizuha.local',
        )
        self._chase(now - timedelta(hours=5))
        self._chase(now - timedelta(hours=3))
        current = self._chase(now)
        self.assertEqual(
            _maybe_chase_auto_fallback(self.item, current),
            'next@shizuha.com',
        )
        self.item.refresh_from_db()
        self.assertEqual(self.item.assignee_email, 'next@shizuha.com')

    @patch('tasks.services.workflow_service.WorkflowService._resolve_team_assignee')
    def test_assignee_activity_resets_the_count(self, resolve):
        resolve.return_value = (9, 'next@shizuha.com')
        now = timezone.now()
        self._chase(now - timedelta(hours=5))
        acted = ItemActivity.objects.create(
            item=self.item,
            action='commented',
            user_id=3,
            user_email='old@shizuha.com',
        )
        ItemActivity.objects.filter(pk=acted.pk).update(
            created_at=now - timedelta(hours=4),
        )
        self._chase(now - timedelta(hours=3))
        current = self._chase(now)
        self.assertIsNone(_maybe_chase_auto_fallback(self.item, current))
        resolve.assert_not_called()
