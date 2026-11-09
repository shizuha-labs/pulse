"""Comment-noise guardrails (2026-11-09 tasks_comment audit).

Covers both write paths:
* model layer: automation authors cannot re-append an identical body to the
  same item inside the dedupe window; state-marker bodies, replies and
  evidence-type mechanical events are never deduped; long automation bodies
  are truncated;
* public API: max length (400), idempotent replay of an identical body, and
  per-item / per-author hourly rate limits (429) with operator exemption.
"""
import os
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from tasks.models import Comment, Item, ItemActivity, Project, TeamRoster
from tasks.services import comment_admission

User = get_user_model()


def _hermetic_org(org_id):
    return {'id': org_id, 'slug': 'shizuha'}


def _hermetic_classify(email, **_kwargs):
    return ('canonical', (email or '').strip().lower())


class _HermeticIdentityMixin:
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        for target, kwargs in (
            ('tasks.services.admin_service.admin_service.get_organization',
             {'side_effect': _hermetic_org}),
            ('tasks.services.canonical_identity.classify_roster_email',
             {'side_effect': _hermetic_classify}),
        ):
            p = patch(target, **kwargs)
            p.start()
            cls.addClassCleanup(p.stop)


class AutomationCommentDedupeTests(_HermeticIdentityMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.project = Project.objects.create(
            name='Noise', key='NOISE', org_id=1, access_level='open')
        cls.item = Item.objects.create(
            project=cls.project, item_key='NOISE-1', mode='task',
            title='fixture', status='todo')
        cls.other = Item.objects.create(
            project=cls.project, item_key='NOISE-2', mode='task',
            title='fixture 2', status='todo')

    def _sys(self, content, item=None, author='system@pulse', **extra):
        return Comment.objects.create(
            item=item or self.item, author_id=0, author_email=author,
            content=content, **extra)

    def test_identical_automation_comment_is_not_reinserted(self):
        body = 'Cleared unavailable concrete owner to the **qa** group queue.'
        first = self._sys(body)
        before_activity = ItemActivity.objects.filter(item=self.item).count()
        second = self._sys(body)
        self.assertEqual(second.pk, first.pk)
        self.assertTrue(getattr(second, 'comment_admission_suppressed', False))
        self.assertEqual(
            Comment.objects.filter(item=self.item, content=body).count(), 1)
        # No post_save fan-out for the suppressed repeat.
        self.assertEqual(
            ItemActivity.objects.filter(item=self.item).count(), before_activity)

    def test_different_body_or_item_or_author_still_inserts(self):
        a = self._sys('Returned to the **devops** team queue.')
        b = self._sys('Returned to the **merge** team queue.')
        c = self._sys('Returned to the **devops** team queue.', item=self.other)
        d = self._sys('Returned to the **devops** team queue.', author='workflow@pulse')
        self.assertEqual(len({a.pk, b.pk, c.pk, d.pk}), 4)

    def test_repeat_outside_window_inserts(self):
        body = 'Removed stale prior-stage owner.'
        first = self._sys(body)
        Comment.objects.filter(pk=first.pk).update(
            created_at=timezone.now() - timedelta(minutes=241))
        second = self._sys(body)
        self.assertNotEqual(second.pk, first.pk)

    def test_window_env_zero_disables(self):
        body = 'Queued in the **engineering** capacity queue.'
        with patch.dict(os.environ, {'PULSE_AUTOMATION_COMMENT_DEDUPE_MINUTES': '0'}):
            first = self._sys(body)
            second = self._sys(body)
        self.assertNotEqual(first.pk, second.pk)

    def test_state_marker_bodies_are_never_deduped(self):
        for body in (
            'Auto-failover to **a@x** — primary **b@x** is unavailable.',
            'Pulled from the **devops** capacity queue to **a@x** (rebalance sweep).',
            '🚨 **Critical unassigned override**: force-assigned to **a@x**.',
        ):
            first = self._sys(body)
            second = self._sys(body)
            self.assertNotEqual(first.pk, second.pk, body)

    def test_human_and_agent_authors_untouched_at_model_layer(self):
        first = Comment.objects.create(
            item=self.item, author_id=7, author_email='agent@agents.shizuha.io',
            content='same words')
        second = Comment.objects.create(
            item=self.item, author_id=7, author_email='agent@agents.shizuha.io',
            content='same words')
        self.assertNotEqual(first.pk, second.pk)

    def test_integration_author_is_deduped(self):
        body = 'Still FAILING: BUILDS HEALTH FAILED at [portal].'
        first = self._sys(body, author='builds-health@shizuha.com')
        second = self._sys(body, author='builds-health@shizuha.com')
        self.assertEqual(first.pk, second.pk)

    def test_replies_are_not_deduped(self):
        parent = self._sys('parent note unique')
        r1 = self._sys('ack', parent=parent)
        r2 = self._sys('ack', parent=parent)
        self.assertNotEqual(r1.pk, r2.pk)

    def test_evidence_mechanical_rows_are_not_deduped(self):
        common = dict(origin_kind=Comment.ORIGIN_WORKFLOW_LIFECYCLE,
                      origin_event='receipt.live_probe_passed', template_version=1)
        a = self._sys('receipt body', author='workflow@pulse',
                      origin_event_id='evt-a', **common)
        b = self._sys('receipt body', author='workflow@pulse',
                      origin_event_id='evt-b', **common)
        self.assertNotEqual(a.pk, b.pk)

    def test_restatement_mechanical_rows_are_deduped(self):
        common = dict(origin_kind=Comment.ORIGIN_WORKFLOW_LIFECYCLE,
                      origin_event='status_lane.normalized', template_version=1)
        a = self._sys('Normalized status-lane ownership for `blocked`.',
                      author='workflow@pulse', origin_event_id='evt-1', **common)
        b = self._sys('Normalized status-lane ownership for `blocked`.',
                      author='workflow@pulse', origin_event_id='evt-2', **common)
        self.assertEqual(a.pk, b.pk)
        self.assertFalse(Comment.objects.filter(origin_event_id='evt-2').exists())

    def test_long_automation_body_is_truncated(self):
        with patch.dict(os.environ, {'PULSE_AUTOMATION_COMMENT_MAX_CHARS': '100'}):
            c = self._sys('x' * 500)
        c.refresh_from_db()
        self.assertTrue(c.content.startswith('x' * 100))
        self.assertIn('truncated 400 characters', c.content)
        self.assertLess(len(c.content), 250)

    def test_existing_update_path_unchanged(self):
        c = Comment.objects.create(
            item=self.item, author_id=7, author_email='h@x.io', content='v1')
        c.content = 'v2'
        c.save()
        c.refresh_from_db()
        self.assertEqual(c.content, 'v2')
        self.assertTrue(c.is_edited)


class UserCommentApiGuardrailTests(APITestCase):
    def setUp(self):
        self.client = APIClient()
        self.member = User.objects.create_user(
            username='qa', email='qa@shizuha.com', password='x', is_staff=False,
        )
        self.project = Project.objects.create(
            name='Shizuha Origin', key='ORIG', org_id=1, access_level='private',
            is_active=True,
        )
        TeamRoster.objects.update_or_create(
            team_name='engineering',
            defaults={'tiers': [['qa@shizuha.com']], 'is_active': True},
        )
        self.item = Item.objects.create(
            mode='task', title='Noise guard', project=self.project,
            assignment_group='engineering',
        )
        self.item2 = Item.objects.create(
            mode='task', title='Noise guard 2', project=self.project,
            assignment_group='engineering',
        )
        self.client.force_authenticate(self.member)

    def _post(self, content, item=None):
        return self.client.post(
            '/api/comments/', {'item': (item or self.item).id, 'content': content},
            format='json',
        )

    def test_normal_comment_still_created(self):
        r = self._post('substantive progress note')
        self.assertEqual(r.status_code, 201, r.content)
        self.assertTrue(Comment.objects.filter(id=r.json()['id']).exists())

    def test_identical_repost_is_replayed_not_duplicated(self):
        r1 = self._post('Still waiting on review.')
        r2 = self._post('Still waiting on review.')
        self.assertEqual(r1.status_code, 201, r1.content)
        self.assertEqual(r2.status_code, 201, r2.content)
        self.assertEqual(r1.json()['id'], r2.json()['id'])
        self.assertEqual(
            Comment.objects.filter(item=self.item, content='Still waiting on review.').count(), 1)

    def test_identical_repost_after_window_creates_new_row(self):
        r1 = self._post('daily status')
        Comment.objects.filter(id=r1.json()['id']).update(
            created_at=timezone.now() - timedelta(seconds=601))
        r2 = self._post('daily status')
        self.assertNotEqual(r1.json()['id'], r2.json()['id'])

    def test_over_long_comment_rejected_with_attachment_hint(self):
        with patch.dict(os.environ, {'PULSE_COMMENT_MAX_CHARS': '50'}):
            r = self._post('y' * 51)
        self.assertEqual(r.status_code, 400, r.content)
        self.assertIn('attach', r.content.decode().lower())

    def test_per_item_hourly_limit(self):
        with patch.dict(os.environ, {'PULSE_COMMENT_ITEM_HOURLY_LIMIT': '3'}):
            for i in range(3):
                self.assertEqual(self._post(f'note {i}').status_code, 201)
            r = self._post('note 4')
            self.assertEqual(r.status_code, 429, r.content)
            # Other items are unaffected by the per-item limit.
            self.assertEqual(self._post('note on other', item=self.item2).status_code, 201)

    def test_per_author_hourly_limit(self):
        with patch.dict(os.environ, {'PULSE_COMMENT_AUTHOR_HOURLY_LIMIT': '2'}):
            self.assertEqual(self._post('a').status_code, 201)
            self.assertEqual(self._post('b', item=self.item2).status_code, 201)
            self.assertEqual(self._post('c').status_code, 429)

    def test_rate_exempt_email(self):
        with patch.dict(os.environ, {
            'PULSE_COMMENT_ITEM_HOURLY_LIMIT': '1',
            'PULSE_COMMENT_RATE_EXEMPT_EMAILS': 'qa@shizuha.com',
        }):
            self.assertEqual(self._post('one').status_code, 201)
            self.assertEqual(self._post('two').status_code, 201)


class AdmissionHelperTests(TestCase):
    def test_is_automation_author(self):
        self.assertTrue(comment_admission.is_automation_author('system@pulse'))
        self.assertTrue(comment_admission.is_automation_author('Workflow@Pulse'))
        self.assertTrue(comment_admission.is_automation_author('qa-automation@shizuha.com'))
        self.assertFalse(comment_admission.is_automation_author('ichi@agents.shizuha.io'))
        self.assertFalse(comment_admission.is_automation_author(''))

    def test_clamp_short_body_unchanged(self):
        self.assertEqual(comment_admission.clamp_automation_content('abc'), 'abc')
