"""PLS-256 — stale-bot sweep + Definition-of-Ready intake gate.

Covers the acceptance criteria:
- a synthetic idle non-exempt task gets marked, then closed after grace;
- recurring / security-labeled / blocked items are exempt;
- high-value (priority/story-point) idle items are flagged, never auto-closed;
- a real comment/transition clears `stale` (reversibility);
- dry-run closes nothing;
- a no-outcome create is flagged `needs-detail` (or rejected in reject mode).
"""
from datetime import timedelta
from unittest import mock

from django.db import connection
from django.test import TestCase, override_settings
from django.utils import timezone

from tasks.models import Comment, Item, ItemActivity, ItemLink, Project
from tasks.serializers import ItemSerializer
from tasks import liveness
from tasks.tasks import _notify_stale_sweep_owner, sweep_stale_tasks


APPLY_ENV = {'PULSE_STALE_SWEEP_APPLY': 'true'}


def _run(**env):
    with mock.patch.dict('os.environ', env, clear=False):
        return sweep_stale_tasks.apply().get()


class StaleSweepTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name='Pulse', key='PLS', org_id=1)
        self._n = 0

    def _item(self, *, idle_days=None, **overrides):
        """Create a task item, optionally backdated to be `idle_days` old with no
        recent human activity."""
        self._n += 1
        defaults = dict(
            mode='task', item_type='task.generic', title=f'Task {self._n}',
            item_key=f'PLS-{self._n}', project=self.project, status='open',
            workflow_name='autonomous-dev', assignment_group='engineering',
            description='a reasonably detailed description of the work to do',
        )
        defaults.update(overrides)
        # A JSONField(default=dict) column is NOT NULL at the SQL level, so
        # Item.objects.create(labels=None) would emit SQL NULL and be rejected
        # by the constraint. Legacy null labels actually reach the sweep as a
        # JSON `null` value (distinct from SQL NULL, which NOT NULL does not
        # reject) left by pre-normalization writes. Reproduce that exact
        # reachable shape: create with the model default, then store JSON null.
        store_json_null_labels = 'labels' in defaults and defaults['labels'] is None
        if store_json_null_labels:
            defaults.pop('labels')
        item = Item.objects.create(**defaults)
        if store_json_null_labels:
            with connection.cursor() as cur:
                cur.execute(
                    "UPDATE tasks_task SET labels = 'null'::jsonb WHERE id = %s",
                    [item.id],
                )
            item.refresh_from_db()
        if idle_days is not None:
            old = timezone.now() - timedelta(days=idle_days)
            Item.objects.filter(id=item.id).update(created_at=old, updated_at=old)
            ItemActivity.objects.filter(item=item).update(created_at=old)
            item.refresh_from_db()
        return item

    # ---- marking ----------------------------------------------------------
    def test_idle_non_exempt_task_is_marked_stale(self):
        item = self._item(idle_days=25)
        result = _run(**APPLY_ENV)
        self.assertIn(item.item_key, result['marked'])
        item.refresh_from_db()
        self.assertEqual(item.labels.get('stale'), 'true')
        self.assertIn('stale_marked_at', item.metadata)
        self.assertTrue(Comment.objects.filter(item=item, content__contains='Marked `stale`').exists())

    def test_recent_task_not_marked(self):
        item = self._item(idle_days=3)
        result = _run(**APPLY_ENV)
        self.assertNotIn(item.item_key, result['marked'])
        item.refresh_from_db()
        self.assertNotIn('stale', item.labels)

    def test_dry_run_marks_nothing(self):
        item = self._item(idle_days=25)
        result = sweep_stale_tasks.apply().get()  # no APPLY env → dry-run
        self.assertFalse(result['applied'])
        self.assertIn(item.item_key, result['marked'])  # reported…
        item.refresh_from_db()
        self.assertNotIn('stale', item.labels)  # …but not actually applied

    def test_success_records_stale_sweep_liveness_and_metric_payload(self):
        item = self._item(idle_days=25)
        with mock.patch('tasks.liveness.record_heartbeat', return_value=1234.0) as heartbeat:
            result = sweep_stale_tasks.apply().get()
        heartbeat.assert_called_with(liveness.STALE_SWEEP)
        self.assertIn(item.item_key, result['marked'])
        self.assertEqual(result['item_errors'], 0)
        self.assertEqual(result['candidate_count'], 1)
        self.assertTrue(result['backlog_non_empty'])

    # ---- closing after grace ---------------------------------------------
    def test_stale_item_closed_after_grace(self):
        item = self._item(idle_days=40)
        marked_at = (timezone.now() - timedelta(days=10)).isoformat()
        Item.objects.filter(id=item.id).update(
            labels={'stale': 'true'}, metadata={'stale_marked_at': marked_at})
        result = _run(**APPLY_ENV)
        self.assertIn(item.item_key, result['closed'])
        item.refresh_from_db()
        self.assertEqual(item.status, 'closed')
        self.assertEqual(item.metadata.get('stale_closed_from_status'), 'open')

    def test_stale_item_within_grace_not_closed(self):
        item = self._item(idle_days=25)
        marked_at = (timezone.now() - timedelta(days=2)).isoformat()  # within 7-day grace
        Item.objects.filter(id=item.id).update(
            labels={'stale': 'true'}, metadata={'stale_marked_at': marked_at})
        result = _run(**APPLY_ENV)
        self.assertNotIn(item.item_key, result['closed'])
        item.refresh_from_db()
        self.assertEqual(item.status, 'open')

    def test_dry_run_closes_nothing(self):
        item = self._item(idle_days=40)
        marked_at = (timezone.now() - timedelta(days=10)).isoformat()
        Item.objects.filter(id=item.id).update(
            labels={'stale': 'true'}, metadata={'stale_marked_at': marked_at})
        result = sweep_stale_tasks.apply().get()  # dry-run
        self.assertIn(item.item_key, result['closed'])  # reported
        item.refresh_from_db()
        self.assertEqual(item.status, 'open')  # not actually closed

    # ---- reversibility ----------------------------------------------------
    def test_activity_after_mark_clears_stale(self):
        item = self._item(idle_days=40)
        marked_at = timezone.now() - timedelta(days=10)
        Item.objects.filter(id=item.id).update(
            labels={'stale': 'true'}, metadata={'stale_marked_at': marked_at.isoformat()})
        # A real (non-system) activity AFTER the mark.
        ItemActivity.objects.create(
            item=item, action='commented', user_id=7, user_email='owner@example.test')
        result = _run(**APPLY_ENV)
        self.assertIn(item.item_key, result['cleared'])
        self.assertNotIn(item.item_key, result['closed'])
        item.refresh_from_db()
        self.assertNotIn('stale', item.labels)

    def test_system_comment_does_not_reset_staleness(self):
        # The sweep's OWN system comment must not count as activity (else grace
        # never elapses). Mark it, then run again → it should still progress.
        item = self._item(idle_days=40)
        _run(**APPLY_ENV)  # marks it, creating a system comment
        item.refresh_from_db()
        self.assertEqual(item.labels.get('stale'), 'true')
        # Backdate the mark past grace; the system comment is the only activity.
        Item.objects.filter(id=item.id).update(
            metadata={'stale_marked_at': (timezone.now() - timedelta(days=10)).isoformat()})
        result = _run(**APPLY_ENV)
        self.assertIn(item.item_key, result['closed'])  # not "cleared"

    # ---- exemptions -------------------------------------------------------
    def test_recurring_item_exempt(self):
        item = self._item(idle_days=40, is_recurring=True, frequency='daily')
        result = _run(**APPLY_ENV)
        self.assertNotIn(item.item_key, result['marked'])
        self.assertGreaterEqual(result['exempted'], 1)

    def test_security_labeled_item_exempt(self):
        item = self._item(idle_days=40, labels={'security-finding': 'true'})
        result = _run(**APPLY_ENV)
        self.assertNotIn(item.item_key, result['marked'])

    def test_epic_item_exempt(self):
        item = self._item(idle_days=40, item_type='epic')
        result = _run(**APPLY_ENV)
        self.assertNotIn(item.item_key, result['marked'])

    def test_blocked_item_exempt(self):
        blocker = self._item(idle_days=1, status='open')
        blocked = self._item(idle_days=40)
        ItemLink.objects.create(
            source_item=blocked, target_item=blocker, link_type='is_blocked_by', created_by_id=1)
        result = _run(**APPLY_ENV)
        self.assertNotIn(blocked.item_key, result['marked'])

    # ---- high-value → flag, never close ----------------------------------
    def test_high_priority_idle_item_flagged_not_closed(self):
        item = self._item(idle_days=40, priority='urgent')
        result = _run(**APPLY_ENV)
        self.assertIn(item.item_key, result['flagged'])
        self.assertNotIn(item.item_key, result['closed'])
        item.refresh_from_db()
        self.assertEqual(item.labels.get('needs-custodian-review'), 'true')
        self.assertEqual(item.status, 'open')

    def test_high_story_point_idle_item_flagged(self):
        item = self._item(idle_days=40, story_points=8)
        result = _run(**APPLY_ENV)
        self.assertIn(item.item_key, result['flagged'])

    # ---- kill-switch + close cap -----------------------------------------
    def test_kill_switch_skips_sweep(self):
        self._item(idle_days=40)
        result = _run(PULSE_STALE_SWEEP_ENABLED='false', **APPLY_ENV)
        self.assertEqual(result.get('skipped'), 'kill-switch')

    def test_close_cap_bounds_closes_per_run(self):
        marked_at = (timezone.now() - timedelta(days=10)).isoformat()
        for _ in range(3):
            it = self._item(idle_days=40)
            Item.objects.filter(id=it.id).update(
                labels={'stale': 'true'}, metadata={'stale_marked_at': marked_at})
        with mock.patch('tasks.tasks._notify_stale_sweep_owner') as notify:
            result = _run(PULSE_STALE_SWEEP_MAX_CLOSES='2', **APPLY_ENV)
        self.assertEqual(len(result['closed']), 2)
        self.assertTrue(result['cap_hit'])
        notify.assert_called_once()
        self.assertEqual(notify.call_args.args[0]['state'], 'anomaly')
        self.assertIn('close cap hit', notify.call_args.args[0]['error'])


    def test_notify_stale_sweep_owner_posts_connect_dm(self):
        payload = {
            'state': 'anomaly', 'processed': 0, 'marked': 0, 'closed': 0,
            'flagged': 0, 'cleared': 0, 'exempted': 0, 'item_errors': 0,
            'cap_hit': False, 'backlog_non_empty': True, 'candidate_count': 3,
            'error': 'zero processed', 'invariant': 'only non-exempt idle items',
        }
        response = mock.Mock(status_code=201)
        with mock.patch('tasks.services.workflow_service.WorkflowService._resolve_team_lead_email',
                        return_value='sora@shizuha.com'),              mock.patch('tasks.user_service.user_service.get_user_by_email',
                        return_value={'id': 44}),              mock.patch('requests.post', return_value=response) as post,              mock.patch('tasks.services.notifications.UserNotificationService.notify_user') as notify:
            self.assertTrue(_notify_stale_sweep_owner(payload))
        post.assert_called_once()
        body = post.call_args.kwargs['json']
        self.assertEqual(body['recipient_user_id'], 44)
        self.assertEqual(body['sender_service'], 'shizuha-pulse')
        self.assertIn('PLS-262 stale-sweep health', body['content'])
        self.assertIn('zero processed', body['content'])
        notify.assert_called_once()
        self.assertEqual(notify.call_args.kwargs['event_type'], 'alert')
        self.assertEqual(notify.call_args.kwargs['severity'], 'error')

    def test_zero_processed_large_backlog_notifies_owner(self):
        for _ in range(3):
            self._item(idle_days=1)
        with mock.patch('tasks.tasks._notify_stale_sweep_owner') as notify:
            result = _run(PULSE_STALE_SWEEP_ZERO_PROCESSED_MIN_BACKLOG='3')
        self.assertEqual(result['candidate_count'], 3)
        notify.assert_called_once()
        payload = notify.call_args.args[0]
        self.assertEqual(payload['state'], 'anomaly')
        self.assertIn('zero processed', payload['error'])

    # ---- PLS-470: legacy list/string labels regression --------------------

    def test_legacy_list_labels_does_not_crash(self):
        """Legacy list-of-strings labels must not raise 'list' object has no attribute 'items'."""
        item = self._item(idle_days=25, labels=['frontend', 'bug'])
        result = _run(**APPLY_ENV)
        self.assertIn(item.item_key, result['marked'])
        self.assertEqual(result['item_errors'], 0)
        item.refresh_from_db()
        self.assertEqual(item.labels.get('stale'), 'true')
        self.assertEqual(item.labels.get('frontend'), 'true')
        self.assertEqual(item.labels.get('bug'), 'true')

    def test_legacy_string_labels_does_not_crash(self):
        """Legacy bare-string labels must not raise."""
        item = self._item(idle_days=25, labels='frontend')
        result = _run(**APPLY_ENV)
        self.assertIn(item.item_key, result['marked'])
        self.assertEqual(result['item_errors'], 0)
        item.refresh_from_db()
        self.assertEqual(item.labels.get('stale'), 'true')
        self.assertEqual(item.labels.get('frontend'), 'true')

    def test_legacy_list_labels_exempt_check_works(self):
        """Legacy list labels with exempt tokens are correctly exempted."""
        item = self._item(idle_days=40, labels=['security-finding'])
        result = _run(**APPLY_ENV)
        self.assertNotIn(item.item_key, result['marked'])
        self.assertGreaterEqual(result['exempted'], 1)
        self.assertEqual(result['item_errors'], 0)

    def test_legacy_list_labels_clear_stale(self):
        """Legacy list labels are preserved when clearing stale."""
        item = self._item(idle_days=40)
        Item.objects.filter(id=item.id).update(
            labels=['frontend', 'stale'],
            metadata={'stale_marked_at': (timezone.now() - timedelta(days=10)).isoformat()})
        ItemActivity.objects.create(
            item=item, action='commented', user_id=7, user_email='owner@example.test')
        result = _run(**APPLY_ENV)
        self.assertIn(item.item_key, result['cleared'])
        self.assertEqual(result['item_errors'], 0)
        item.refresh_from_db()
        self.assertNotIn('stale', item.labels)
        self.assertEqual(item.labels.get('frontend'), 'true')

    def test_legacy_list_labels_flag_custodian(self):
        """Legacy list labels work with high-value flag path."""
        item = self._item(idle_days=40, priority='urgent', labels=['frontend'])
        result = _run(**APPLY_ENV)
        self.assertIn(item.item_key, result['flagged'])
        self.assertEqual(result['item_errors'], 0)
        item.refresh_from_db()
        self.assertEqual(item.labels.get('needs-custodian-review'), 'true')
        self.assertEqual(item.labels.get('frontend'), 'true')

    def test_empty_dict_labels_works(self):
        """Empty dict labels must not crash."""
        item = self._item(idle_days=25, labels={})
        result = _run(**APPLY_ENV)
        self.assertIn(item.item_key, result['marked'])
        self.assertEqual(result['item_errors'], 0)

    def test_mixed_list_labels_fails_closed_with_anomaly(self):
        """Mixed list entries (strings + non-strings) must fail closed and count an anomaly."""
        item = self._item(idle_days=25, labels=['frontend', {'bad': True}])
        result = _run(**APPLY_ENV)
        self.assertNotIn(item.item_key, result['marked'])
        self.assertGreaterEqual(result['item_errors'], 1)
        item.refresh_from_db()
        self.assertNotIn('stale', item.labels)

    def test_null_labels_does_not_crash(self):
        """Null labels must not crash and should be treated as empty."""
        item = self._item(idle_days=25, labels=None)
        result = _run(**APPLY_ENV)
        self.assertIn(item.item_key, result['marked'])
        self.assertEqual(result['item_errors'], 0)
        item.refresh_from_db()
        self.assertEqual(item.labels.get('stale'), 'true')

    def test_malformed_int_labels_fails_closed_with_anomaly(self):
        """Completely unsupported label shapes (int) must fail closed and count an anomaly."""
        item = self._item(idle_days=25, labels=42)
        result = _run(**APPLY_ENV)
        self.assertNotIn(item.item_key, result['marked'])
        self.assertGreaterEqual(result['item_errors'], 1)
        item.refresh_from_db()
        # Fail-closed: the unsupported int shape is left untouched (never
        # mutated to a stale-tagged dict). `item.labels` is still the raw int,
        # so assert equality rather than membership ('stale' in 42 → TypeError).
        self.assertEqual(item.labels, 42)

    def test_mixed_list_labels_exempt_check_counts_anomaly(self):
        """Mixed list labels with exempt tokens must still fail closed (anomaly counted)."""
        item = self._item(idle_days=40, labels=['security-finding', 7])
        result = _run(**APPLY_ENV)
        self.assertNotIn(item.item_key, result['marked'])
        self.assertGreaterEqual(result['item_errors'], 1)

    def test_mixed_list_labels_clear_stale_counts_anomaly(self):
        """Mixed list labels on a stale-clear path must count an anomaly, not silently clear."""
        item = self._item(idle_days=40)
        Item.objects.filter(id=item.id).update(
            labels=['frontend', 42],
            metadata={'stale_marked_at': (timezone.now() - timedelta(days=10)).isoformat()})
        ItemActivity.objects.create(
            item=item, action='commented', user_id=7, user_email='owner@example.test')
        result = _run(**APPLY_ENV)
        self.assertNotIn(item.item_key, result['cleared'])
        self.assertGreaterEqual(result['item_errors'], 1)

    def test_mixed_list_labels_flag_custodian_counts_anomaly(self):
        """Mixed list labels on high-value path must count an anomaly, not silently flag."""
        item = self._item(idle_days=40, priority='urgent', labels=['frontend', {'bad': True}])
        result = _run(**APPLY_ENV)
        self.assertNotIn(item.item_key, result['flagged'])
        self.assertGreaterEqual(result['item_errors'], 1)


class DefinitionOfReadyGateTests(TestCase):
    def _validate(self, **data):
        data.setdefault('mode', 'task')
        data.setdefault('title', 'Do the thing')
        return ItemSerializer().validate(data)

    def test_no_outcome_create_flagged_needs_detail(self):
        out = self._validate(description='fix it', source='manual')
        self.assertEqual(out['labels'].get('needs-detail'), 'true')

    def test_detailed_create_not_flagged(self):
        out = self._validate(
            description='Investigate and fix the login timeout on the dashboard endpoint',
            source='manual')
        self.assertNotIn('needs-detail', out.get('labels', {}))

    @override_settings(PULSE_DOR_INTAKE_MODE='reject')
    def test_reject_mode_bounces_no_outcome_create(self):
        from rest_framework import serializers as drf
        with self.assertRaises(drf.ValidationError):
            self._validate(description='', source='manual')

    def test_auto_filer_source_is_exempt(self):
        out = self._validate(description='x', source='security-ci')
        self.assertNotIn('needs-detail', out.get('labels', {}))

    def test_recurring_task_exempt_from_gate(self):
        out = self._validate(description='x', source='manual', is_recurring=True, frequency='daily')
        self.assertNotIn('needs-detail', out.get('labels', {}))

    @override_settings(PULSE_DOR_INTAKE_MODE='off')
    def test_off_mode_disables_gate(self):
        out = self._validate(description='x', source='manual')
        self.assertNotIn('needs-detail', out.get('labels', {}))
