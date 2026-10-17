"""Orphan review-lane sweep (operator directive 2026-09-15).

Pipeline-created items minted directly into in_review with no assignee have no
assignment path: the queue-drain skips review-lane statuses and the
critical-unassigned override only covers urgent/high. The sweep bounces
unassigned review-lane items past a 3-day grace window back to a hands-on
status so the queue-drain owns them again.

Regression tests:
  1. Unassigned in_review past grace — bounced to a hands-on status + comment.
  2. Recently created (inside grace) — left alone.
  3. Assigned in_review — left alone (legitimately owned review work).
  4. Terminal/resolved — never touched.
"""
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from tasks.models import Comment, Item, Project


User = get_user_model()


def _make_project():
    return Project.objects.create(name='OrphanSweep', key='ORPH', org_id=1)


def _make_item(project, *, title, status='in_review', assignee=None, age_days=10):
    # Item.save() coerces statuses invalid for the project's default workflow
    # (simple coerces in_review -> in_progress), so create hands-on and stamp
    # the review-lane status via queryset update (raw DB state, as pipelines do).
    it = Item.objects.create(
        mode='task',
        item_type='task',
        title=title,
        project=project,
        assignment_group='qa',
        status='todo',
        workflow_status='To Do',
        source='test-orphan-sweep',
    )
    Item.objects.filter(pk=it.pk).update(
        status=status,
        workflow_status=status.replace('_', ' ').title(),
        assignee_email=assignee,
        updated_at=timezone.now() - timedelta(days=age_days),
    )
    it.refresh_from_db()
    return it


class OrphanReviewLaneSweepTests(TestCase):
    def setUp(self):
        # Own org so the org-assignment guard checks OUR roster, not org 1's.
        import uuid
        org_id = 950000 + (int(uuid.uuid4().hex[:6], 16) % 40000)
        from tasks.models import TeamRoster
        TeamRoster.objects.create(
            team_name='qa', organization_slug=f'orph{org_id}',
            organization_id=org_id, tiers=[['fumi@shizuha.com']],
            is_active=True)
        self.project = Project.objects.create(
            org_id=org_id, name=f'Orphan Sweep {org_id}', key=f'OS{org_id % 1000:03d}')

    def _run(self):
        from tasks.tasks import sweep_orphan_review_lane
        return sweep_orphan_review_lane.apply(args=[]).get()

    def test_unassigned_in_review_past_grace_is_bounced(self):
        it = _make_item(self.project, title='[Regression watchlist] svc → live')
        from tasks.services.workflow_service import WorkflowService
        from datetime import timedelta as _td
        grace = timezone.now() - _td(days=3)
        dbg = Item.objects.filter(mode='task', status__in=WorkflowService._REVIEW_PIPELINE_STATUSES,
            assignee_email__isnull=True, updated_at__lt=grace)
        print('DBG-CANDIDATES:', dbg.count(), list(dbg.values_list('pk','status','assignee_email','updated_at')))
        print('DBG-ITEM:', it.pk, it.status, it.assignee_email, it.updated_at, it.resolution_id, it.is_archived)
        result = self._run()
        print('DBG-RESULT:', result)
        keys = {b['key'] for b in result.get('bounced', [])}
        self.assertIn(str(it.pk), keys)
        it.refresh_from_db()
        self.assertIn(it.status, ('todo', 'open', 'in_progress'))
        self.assertTrue(Comment.objects.filter(item=it, content__icontains='Orphan review-lane sweep').exists())

    def test_recent_in_review_is_left_alone(self):
        it = _make_item(self.project, title='[Regression watchlist] fresh svc', age_days=0.1)
        result = self._run()
        keys = {b['key'] for b in result.get('bounced', [])}
        self.assertNotIn(str(it.pk), keys)
        it.refresh_from_db()
        self.assertEqual(it.status, 'in_review')

    def test_assigned_in_review_is_left_alone(self):
        # The org-assignment DB guard requires the assignee to be on the team
        # roster — seed a minimal qa roster for the assigned fixture.
        it = _make_item(self.project, title='owned review work',
                        assignee='fumi@shizuha.com')
        result = self._run()
        keys = {b['key'] for b in result.get('bounced', [])}
        self.assertNotIn(str(it.pk), keys)
        it.refresh_from_db()
        self.assertEqual(it.status, 'in_review')

    def test_terminal_items_never_touched(self):
        it = _make_item(self.project, title='done review orphan', status='done')
        result = self._run()
        keys = {b['key'] for b in result.get('bounced', [])}
        self.assertNotIn(str(it.pk), keys)
        it.refresh_from_db()
        self.assertEqual(it.status, 'done')

    def test_new_system_actor_has_unusable_password(self):
        _make_item(self.project, title='orphan requiring a system actor')
        User.objects.filter(is_staff=True).update(is_staff=False, is_superuser=False)
        User.objects.filter(is_superuser=True).update(is_superuser=False)
        User.objects.filter(username='orphan-sweep').delete()
        with mock.patch.dict('os.environ', {'PULSE_OPERATOR_EMAIL': ''}):
            self._run()
        actor = User.objects.get(username='orphan-sweep')
        self.assertFalse(actor.has_usable_password())
