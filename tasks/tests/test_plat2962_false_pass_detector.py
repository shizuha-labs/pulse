"""PLAT-2962: QA false-pass detector."""
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone
from django_celery_beat.models import PeriodicTask

from tasks.models import Comment, Item, ItemActivity, ItemLink, Project
from tasks.services.false_pass_detector import (
    compute_false_pass_metrics,
    run_false_pass_detector,
    _mark_report_run_failed,
)
from tasks.tasks import setup_notification_schedules, sweep_false_pass_detector


class FalsePassDetectorTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.project = Project.objects.create(key='PLAT', org_id=1, name='Platform')

    def _item(
        self,
        key,
        title,
        *,
        status='done',
        created_at=None,
        labels=None,
        tags='',
        item_type='task.generic',
        workflow_name='autonomous-dev',
        source='',
        source_id='',
    ):
        item = Item.objects.create(
            project=self.project,
            item_key=key,
            title=title,
            status=status,
            workflow_name=workflow_name,
            labels=labels or {},
            tags=tags,
            item_type=item_type,
            source=source,
            source_id=source_id,
            mode='task',
        )
        if created_at is not None:
            Item.objects.filter(pk=item.pk).update(created_at=created_at)
            item.refresh_from_db()
        return item

    def _activity(self, item, *, old='in_review', new='done', at=None, action='status_changed'):
        act = ItemActivity.objects.create(
            item=item,
            action=action,
            field_name='Status',
            old_value=old,
            new_value=new,
            user_id=1,
            user_email='qa@shizuha.com',
        )
        if at is not None:
            ItemActivity.objects.filter(pk=act.pk).update(created_at=at)
            act.refresh_from_db()
        return act

    def _comment(self, item, content, *, at=None, author='zen@shizuha.com'):
        comment = Comment.objects.create(item=item, author_id=1, author_email=author, content=content)
        if at is not None:
            Comment.objects.filter(pk=comment.pk).update(created_at=at)
            comment.refresh_from_db()
        return comment

    def test_reopen_after_terminal_verification_counts_false_pass(self):
        origin = self._item('CTX-37', 'Accessible dashboards fixed', status='reopened')
        verified_at = self.now - timezone.timedelta(days=3)
        reopened_at = self.now - timezone.timedelta(days=2)
        self._activity(origin, old='in_review', new='done', at=verified_at)
        self._activity(origin, old='done', new='reopened', at=reopened_at, action='reopened')

        metrics = compute_false_pass_metrics(window_days=14, lookback_days=14, now=self.now)

        self.assertEqual(metrics['verification_count'], 1)
        self.assertEqual(metrics['false_pass_count'], 1)
        self.assertEqual(metrics['false_pass_rate_per_20'], 20.0)
        self.assertTrue(metrics['breach'])
        self.assertEqual(metrics['pairs'][0]['origin_key'], 'CTX-37')
        self.assertEqual(metrics['pairs'][0]['related_key'], 'CTX-37')
        self.assertIn('reopened_after_verification', metrics['pairs'][0]['reasons'])

    def test_latest_terminal_close_is_not_rewound_by_earlier_qa_comment(self):
        origin = self._item('BKS-120', 'Books remediation', status='done')
        self._activity(origin, old='in_review', new='done', at=self.now - timezone.timedelta(days=12))
        self._comment(origin, 'QA verified the first close.', at=self.now - timezone.timedelta(days=10))
        self._activity(
            origin,
            old='done',
            new='open',
            at=self.now - timezone.timedelta(days=9),
            action='reopened',
        )
        self._activity(origin, old='open', new='done', at=self.now - timezone.timedelta(days=8))

        metrics = compute_false_pass_metrics(window_days=14, lookback_days=14, now=self.now)

        self.assertEqual(metrics['verification_count'], 1)
        self.assertEqual(metrics['false_pass_count'], 0)
        self.assertEqual(metrics['pairs'], [])

    def test_explicit_reopen_to_workflow_specific_active_status_counts(self):
        origin = self._item('PLAT-120', 'Workflow-specific review', status='architect_review')
        self._activity(origin, old='in_review', new='done', at=self.now - timezone.timedelta(days=3))
        self._activity(
            origin,
            old='in_review',
            new='architect_review',
            at=self.now - timezone.timedelta(days=2),
            action='reopened',
        )

        metrics = compute_false_pass_metrics(window_days=14, lookback_days=14, now=self.now)

        self.assertEqual(metrics['false_pass_count'], 1)
        self.assertEqual(metrics['pairs'][0]['reasons'], ['reopened_after_verification'])

    def test_terminal_transition_to_workflow_specific_active_status_counts(self):
        origin = self._item('PLAT-121', 'Workflow-specific verification', status='architect_review')
        self._activity(origin, old='in_review', new='done', at=self.now - timezone.timedelta(days=3))
        self._activity(
            origin,
            old='done',
            new='architect_review',
            at=self.now - timezone.timedelta(days=2),
        )

        metrics = compute_false_pass_metrics(window_days=14, lookback_days=14, now=self.now)

        self.assertEqual(metrics['false_pass_count'], 1)
        self.assertEqual(metrics['pairs'][0]['reasons'], ['reopened_after_verification'])

    def test_only_explicit_duplicate_regression_link_is_listed(self):
        origin = self._item('KURA-6', 'Kura route verified', status='done')
        linked_bug = self._item('KURA-12', 'Regression: KURA-6 route still 502', status='open', item_type='bug.regression')
        ref_bug = self._item('KURA-13', 'Bug: KURA-6 still inaccessible', status='open', item_type='task.bug')
        verified_at = self.now - timezone.timedelta(days=5)
        self._comment(origin, 'QA passed live verification on prod.', at=verified_at)
        link = ItemLink.objects.create(
            source_item=origin,
            target_item=linked_bug,
            link_type='is_duplicated_by',
            created_by_id=1,
            created_by_email='qa@shizuha.com',
        )
        ItemLink.objects.filter(pk=link.pk).update(created_at=self.now - timezone.timedelta(days=4))
        Item.objects.filter(pk=ref_bug.pk).update(created_at=self.now - timezone.timedelta(days=3))

        metrics = compute_false_pass_metrics(window_days=14, lookback_days=14, now=self.now)

        self.assertEqual(metrics['verification_count'], 1)
        self.assertEqual(metrics['false_pass_count'], 1)
        related = {p['related_key']: p['reasons'] for p in metrics['pairs']}
        self.assertIn('KURA-12', related)
        self.assertIn('linked_is_duplicated_by', related['KURA-12'])
        self.assertNotIn('KURA-13', related)
        self.assertEqual(metrics['reason_counts'], {'linked_is_duplicated_by': 1})

    def test_bks_112_hardening_reference_is_not_false_pass(self):
        origin = self._item('BKS-112', 'Books authz-invariant CI gate', status='done')
        verified_at = self.now - timezone.timedelta(days=5)
        hardening = self._item(
            'BKS-127',
            '[BKS-112 hardening] runtime bypass fence',
            status='needs_triage',
            created_at=self.now - timezone.timedelta(days=4),
            labels={'security': True, 'hardening': True},
            tags='payments-books,security-sensitive',
            item_type='task.bug',
        )
        hardening.description = (
            'BKS-112 is sound and this is not a regression. '
            'This is a defense-in-depth hardening item.'
        )
        hardening.save(update_fields=['description'])
        self._activity(origin, old='in_review', new='done', at=verified_at)

        metrics = compute_false_pass_metrics(window_days=14, lookback_days=14, now=self.now)

        self.assertEqual(metrics['verification_count'], 1)
        self.assertEqual(metrics['false_pass_count'], 0)
        self.assertEqual(metrics['reason_counts'], {})

    def test_bks_114_active_status_progression_is_not_reopen(self):
        origin = self._item('BKS-114', 'Books Tally SSRF', status='in_progress')
        verified_at = self.now - timezone.timedelta(days=5)
        self._comment(origin, 'QA passed live verification of the safe probe.', at=verified_at)
        self._activity(
            origin,
            old='in_progress',
            new='in_review',
            at=self.now - timezone.timedelta(days=4),
        )
        self._activity(
            origin,
            old='in_review',
            new='blocked',
            at=self.now - timezone.timedelta(days=3),
        )

        metrics = compute_false_pass_metrics(window_days=14, lookback_days=14, now=self.now)

        self.assertEqual(metrics['verification_count'], 1)
        self.assertEqual(metrics['false_pass_count'], 0)
        self.assertEqual(metrics['pairs'], [])

    def test_cancelled_task_is_not_a_verification_origin(self):
        origin = self._item('BKS-80', 'Focused QA task', status='cancelled')
        self._comment(
            origin,
            'QA verified the observed behavior before cancellation.',
            at=self.now - timezone.timedelta(days=5),
        )
        self._activity(
            origin,
            old='open',
            new='cancelled',
            at=self.now - timezone.timedelta(days=4),
        )
        self._activity(
            origin,
            old='cancelled',
            new='open',
            at=self.now - timezone.timedelta(days=3),
            action='reopened',
        )

        metrics = compute_false_pass_metrics(window_days=14, lookback_days=14, now=self.now)

        self.assertEqual(metrics['verification_count'], 0)
        self.assertEqual(metrics['false_pass_count'], 0)

    def test_focused_qa_related_title_reference_is_not_false_pass(self):
        origin = self._item('BKS-1', 'Payment fix verified', status='done')
        verified_at = self.now - timezone.timedelta(days=6)
        focused_qa_followup = self._item(
            'BKS-11',
            'Regression: BKS-1 payment flow still broken',
            status='open',
            source='focused-qa-automation',
            workflow_name='reviewed-task',
            labels={'source': 'focused-qa'},
            tags='focused-qa,auth',
            item_type='task.focused_qa',
        )
        self._comment(origin, 'QA pass on prod', at=verified_at)
        Item.objects.filter(pk=focused_qa_followup.pk).update(created_at=self.now - timezone.timedelta(days=5))

        metrics = compute_false_pass_metrics(window_days=14, lookback_days=14, now=self.now)

        self.assertEqual(metrics['verification_count'], 1)
        self.assertEqual(metrics['false_pass_count'], 0)
        self.assertEqual(metrics['pairs'], [])

    def test_focused_qa_linked_followup_is_not_false_pass(self):
        origin = self._item('BKS-2', 'Routing fix verified', status='done')
        focused_qa = self._item(
            'BKS-21',
            '[Focused QA:auth] BKS-2 — token refresh guard',
            status='open',
            source='focused-qa-automation',
            source_id='live:auth:focus',
            tags='focused-qa,auth,live',
            item_type='task.focused_qa',
        )
        verified_at = self.now - timezone.timedelta(days=4)
        self._comment(origin, 'QA passed on staging', at=verified_at)
        link = ItemLink.objects.create(
            source_item=origin,
            target_item=focused_qa,
            link_type='blocks',
            created_by_id=1,
            created_by_email='qa@shizuha.com',
        )
        ItemLink.objects.filter(pk=link.pk).update(created_at=self.now - timezone.timedelta(days=3))

        metrics = compute_false_pass_metrics(window_days=14, lookback_days=14, now=self.now)

        self.assertEqual(metrics['verification_count'], 1)
        self.assertEqual(metrics['false_pass_count'], 0)
        self.assertEqual(metrics['pairs'], [])

    def test_plain_related_followup_after_verification_is_not_false_pass(self):
        origin = self._item('PLAT-1', 'Verified platform fix', status='done')
        followup = self._item('PLAT-2', 'Documentation follow-up', status='todo', item_type='task.docs')
        verified_at = self.now - timezone.timedelta(days=3)
        self._activity(origin, old='in_review', new='done', at=verified_at)
        link = ItemLink.objects.create(
            source_item=origin,
            target_item=followup,
            link_type='relates_to',
            created_by_id=1,
            created_by_email='qa@shizuha.com',
        )
        ItemLink.objects.filter(pk=link.pk).update(created_at=self.now - timezone.timedelta(days=2))

        metrics = compute_false_pass_metrics(window_days=14, lookback_days=14, now=self.now)

        self.assertEqual(metrics['verification_count'], 1)
        self.assertEqual(metrics['false_pass_count'], 0)
        self.assertFalse(metrics['breach'])
        self.assertEqual(metrics['pairs'], [])

    def test_negative_verification_comment_is_not_qa_pass(self):
        origin = self._item('PLAT-3', 'Still waiting for evidence', status='in_progress')
        comment_at = self.now - timezone.timedelta(days=3)
        reopened_at = self.now - timezone.timedelta(days=2)
        self._comment(origin, 'Not verified yet; waiting on evidence.', at=comment_at)
        self._activity(origin, old='verification', new='in_progress', at=reopened_at, action='status_changed')

        metrics = compute_false_pass_metrics(window_days=14, lookback_days=14, now=self.now)

        self.assertEqual(metrics['verification_count'], 0)
        self.assertEqual(metrics['false_pass_count'], 0)
        self.assertFalse(metrics['breach'])
        self.assertEqual(metrics['pairs'], [])

    @patch('requests.post')
    @patch('tasks.user_service.user_service.get_user_by_email')
    @patch('tasks.services.false_pass_detector.W._resolve_team_lead_email')
    def test_run_persists_report_links_audit_and_dms_on_breach(self, lead_email, get_user, post):
        lead_email.return_value = 'zen@shizuha.com'
        get_user.return_value = {'id': 42, 'email': 'zen@shizuha.com'}
        audit = self._item('PLAT-1149', 'Monthly QA audit', status='todo')
        origin = self._item('SCLI-184', 'CLI artifact verified', status='reopened')
        self._activity(origin, old='in_review', new='done', at=self.now - timezone.timedelta(days=2))
        self._activity(origin, old='done', new='reopened', at=self.now - timezone.timedelta(days=1), action='reopened')

        result = run_false_pass_detector(window_days=14, lookback_days=14, now=self.now)

        report = Item.objects.get(source='qa-false-pass-detector')
        self.assertEqual(result['report_item_id'], str(report.pk))
        self.assertEqual(report.metadata['false_pass_detector']['false_pass_count'], 1)
        self.assertTrue(report.outward_links.filter(target_item=audit, link_type='relates_to').exists())
        self.assertTrue(report.comments.filter(content__contains='SCLI-184').exists())
        self.assertTrue(result['qa_lead_notified'])
        self.assertEqual(post.call_count, 1)
        post.return_value.raise_for_status.assert_called_once()
        report.refresh_from_db()
        self.assertEqual(
            report.metadata['false_pass_notified_signature'],
            ['SCLI-184->SCLI-184:reopened_after_verification'],
        )
        self.assertTrue(report.metadata['false_pass_notification_delivered'])
        self.assertFalse(report.metadata['false_pass_notification_failed'])

        # Idempotent: a second unchanged breach updates metadata but does not DM again.
        result2 = run_false_pass_detector(window_days=14, lookback_days=14, now=self.now)
        self.assertFalse(result2['report_changed'])
        self.assertFalse(result2['qa_lead_notified'])
        self.assertEqual(post.call_count, 1)

    @patch('requests.post')
    @patch('tasks.user_service.user_service.get_user_by_email')
    @patch('tasks.services.false_pass_detector.W._resolve_team_lead_email')
    def test_failed_breach_notification_retries_until_delivered(self, lead_email, get_user, post):
        lead_email.return_value = 'zen@shizuha.com'
        get_user.return_value = {'id': 42, 'email': 'zen@shizuha.com'}
        failed_response = post.return_value
        failed_response.raise_for_status.side_effect = Exception('connect 500')
        origin = self._item('SCLI-185', 'CLI artifact verified', status='reopened')
        self._activity(origin, old='in_review', new='done', at=self.now - timezone.timedelta(days=2))
        self._activity(origin, old='done', new='reopened', at=self.now - timezone.timedelta(days=1), action='reopened')

        result = run_false_pass_detector(window_days=14, lookback_days=14, now=self.now)

        report = Item.objects.get(source='qa-false-pass-detector')
        self.assertFalse(result['qa_lead_notified'])
        self.assertEqual(post.call_count, 1)
        self.assertNotEqual(
            report.metadata.get('false_pass_notified_signature'),
            ['SCLI-185->SCLI-185:reopened_after_verification'],
        )
        self.assertFalse(report.metadata['false_pass_notification_delivered'])
        self.assertTrue(report.metadata['false_pass_notification_failed'])

        post.reset_mock()
        post.return_value.raise_for_status.side_effect = None
        result2 = run_false_pass_detector(window_days=14, lookback_days=14, now=self.now)

        report.refresh_from_db()
        self.assertFalse(result2['report_changed'])
        self.assertTrue(result2['qa_lead_notified'])
        self.assertEqual(post.call_count, 1)
        self.assertEqual(
            report.metadata['false_pass_notified_signature'],
            ['SCLI-185->SCLI-185:reopened_after_verification'],
        )
        self.assertTrue(report.metadata['false_pass_notification_delivered'])
        self.assertFalse(report.metadata['false_pass_notification_failed'])

    def test_celery_task_and_schedule_are_registered(self):
        origin = self._item('PLS-1', 'Pulse task', status='done')
        self._activity(origin, old='in_review', new='done', at=self.now - timezone.timedelta(days=1))

        task_result = sweep_false_pass_detector.apply(kwargs={'window_days': 14, 'lookback_days': 14, 'alert': False}).get()
        self.assertEqual(task_result['verification_count'], 1)
        self.assertFalse(task_result['qa_lead_notified'])

        PeriodicTask.objects.all().delete()
        setup_notification_schedules()
        periodic = PeriodicTask.objects.select_related('crontab').get(name='QA False-Pass Detector')
        self.assertEqual(periodic.task, 'tasks.tasks.sweep_false_pass_detector')
        self.assertEqual(periodic.crontab.minute, '53')
        self.assertEqual(periodic.crontab.hour, '6')

    @patch('requests.post')
    @patch('tasks.user_service.user_service.get_user_by_email')
    @patch('tasks.services.workflow_service.WorkflowService._resolve_team_lead_email')
    @patch('tasks.services.false_pass_detector.run_false_pass_detector')
    def test_sweep_failure_notifies_qa_lead_then_reraises(self, run_detector, lead_email, get_user, post):
        lead_email.return_value = 'zen@shizuha.com'
        get_user.return_value = {'id': 42, 'email': 'zen@shizuha.com'}
        run_detector.side_effect = RuntimeError('detector boom')

        with self.assertRaises(RuntimeError):
            sweep_false_pass_detector.run(window_days=14, lookback_days=14, alert=True)

        self.assertEqual(post.call_count, 1)
        body = post.call_args.kwargs['json']
        self.assertEqual(body['recipient_user_id'], 42)
        self.assertIn('QA False-Pass Detector Failure', body['content'])
        self.assertIn('sweep_false_pass_detector', body['content'])
        self.assertIn('RuntimeError: detector boom', body['content'])
        post.return_value.raise_for_status.assert_called_once()


# PLS-718: the 14d scan must not materialize the full comment set, must fit the
# statement budget, and must surface run provenance so a failed sweep is never
# mistaken for "no change".
class FalsePassDetectorResourceTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.project = Project.objects.create(key='PLAT', org_id=1, name='Platform')

    def _item(self, key, title, *, status='done', created_at=None):
        item = Item.objects.create(
            project=self.project,
            item_key=key,
            title=title,
            status=status,
            workflow_name='autonomous-dev',
            labels={},
            tags='',
            item_type='task.generic',
            source='',
            source_id='',
            mode='task',
        )
        if created_at is not None:
            Item.objects.filter(pk=item.pk).update(created_at=created_at)
            item.refresh_from_db()
        return item

    def _comment(self, item, content, *, at=None, author='zen@shizuha.com'):
        comment = Comment.objects.create(item=item, author_id=1, author_email=author, content=content)
        if at is not None:
            Comment.objects.filter(pk=comment.pk).update(created_at=at)
            comment.refresh_from_db()
        return comment

    def test_sql_prefilter_only_counts_true_qa_pass_comments(self):
        # The SQL icontains pre-filter must be a strict superset of the Python
        # regex: every Python _QA_PASS_RE match is fetched, and non-matching
        # comments (the bulk of the 14d set) never reach the Python check.
        origin = self._item('PLS-900', 'Verified routing fix', status='done')
        decoy = self._item('PLS-901', 'Unrelated work', status='in_progress')
        self._comment(origin, 'QA passed live verification on prod.', at=self.now - timezone.timedelta(days=3))
        self._comment(origin, 'verified by QA — good to close.', at=self.now - timezone.timedelta(days=2))
        # Negative: contains a fail phrase, so not a QA pass despite "verified".
        self._comment(origin, 'Not verified yet; waiting on evidence.', at=self.now - timezone.timedelta(days=2))
        # Decoy: no QA-pass keyword at all — must not be fetched by the prefilter.
        self._comment(decoy, 'Just a status note about the deploy.', at=self.now - timezone.timedelta(days=1))

        metrics = compute_false_pass_metrics(window_days=14, lookback_days=14, now=self.now)

        # Two genuine QA-pass comments on the same item collapse to one
        # verification event (the later one wins the timestamp).
        self.assertEqual(metrics['verification_count'], 1)
        self.assertEqual(metrics['false_pass_count'], 0)

    def test_run_persists_run_provenance_ok(self):
        origin = self._item('PLS-902', 'Verified fix', status='done')
        self._comment(origin, 'QA pass on staging.', at=self.now - timezone.timedelta(days=2))

        result = run_false_pass_detector(window_days=14, lookback_days=14, alert=False, now=self.now)

        report = Item.objects.get(source='qa-false-pass-detector')
        self.assertEqual(report.metadata['last_run_status'], 'ok')
        self.assertIsNone(report.metadata['last_run_error'])
        self.assertTrue(report.metadata['last_attempted_at'])
        self.assertIn('last run: ok', report.description)
        self.assertEqual(result['report_item_key'], f'#{report.pk}')

    def test_failed_sweep_stamps_report_as_failed_not_stale(self):
        # A failed sweep must make the report visibly stale: last_run_status
        # 'failed' + the error + attempt time, so a reader never mistakes "did
        # not run" for "no change".
        origin = self._item('PLS-903', 'Verified fix', status='done')
        self._comment(origin, 'QA pass on staging.', at=self.now - timezone.timedelta(days=2))
        run_false_pass_detector(window_days=14, lookback_days=14, alert=False, now=self.now)

        _mark_report_run_failed(RuntimeError('statement timeout'), window_days=14)

        report = Item.objects.get(source='qa-false-pass-detector')
        self.assertEqual(report.metadata['last_run_status'], 'failed')
        self.assertEqual(report.metadata['last_run_error'], 'RuntimeError: statement timeout')
        self.assertTrue(report.metadata['last_attempted_at'])
        self.assertIn('last run: FAILED (RuntimeError: statement timeout)', report.description)

    def test_sweep_failure_path_stamps_report_via_task(self):
        # End-to-end through the celery task: when run_false_pass_detector
        # raises, sweep_false_pass_detector must stamp the report failed (and
        # still notify + re-raise).
        origin = self._item('PLS-904', 'Verified fix', status='done')
        self._comment(origin, 'QA pass on staging.', at=self.now - timezone.timedelta(days=2))
        run_false_pass_detector(window_days=14, lookback_days=14, alert=False, now=self.now)

        with patch('tasks.services.false_pass_detector.run_false_pass_detector') as run_detector:
            run_detector.side_effect = RuntimeError('detector boom')
            with self.assertRaises(RuntimeError):
                sweep_false_pass_detector.run(window_days=14, lookback_days=14, alert=True)

        report = Item.objects.get(source='qa-false-pass-detector')
        self.assertEqual(report.metadata['last_run_status'], 'failed')
        self.assertEqual(report.metadata['last_run_error'], 'RuntimeError: detector boom')
        self.assertIn('last run: FAILED (RuntimeError: detector boom)', report.description)

    def test_large_comment_volume_streams_without_oom(self):
        # Seed a comment volume well above a handful of fixtures to exercise the
        # streaming + SQL-prefilter path: thousands of non-matching comments
        # must not change the detector's result and must complete.
        origin = self._item('PLS-905', 'Verified fix', status='done')
        decoy = self._item('PLS-906', 'High-traffic item', status='in_progress')
        qa_at = self.now - timezone.timedelta(days=2)
        self._comment(origin, 'QA passed live verification.', at=qa_at)

        bulk = []
        for i in range(4000):
            bulk.append(Comment(
                item=decoy,
                author_id=1,
                author_email='bot@shizuha.com',
                content=f'Routine status update number {i} with no verification language.',
            ))
        Comment.objects.bulk_create(bulk)
        # A few decoys that mention "verified" but are negative (fail phrase).
        bulk2 = []
        for i in range(20):
            bulk2.append(Comment(
                item=decoy,
                author_id=1,
                author_email='bot@shizuha.com',
                content=f'Not verified yet ({i}); waiting on evidence.',
            ))
        Comment.objects.bulk_create(bulk2)

        metrics = compute_false_pass_metrics(window_days=14, lookback_days=14, now=self.now)

        self.assertEqual(metrics['verification_count'], 1)
        self.assertEqual(metrics['false_pass_count'], 0)
        self.assertEqual(metrics['pairs'], [])
