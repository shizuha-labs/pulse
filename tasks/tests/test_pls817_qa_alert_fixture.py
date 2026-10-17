"""PLS-817: safe controlled QA alert fixture — create/ACK/silence/resolve/cleanup.

The F0.7 daily QA recipe needs a current actionable test alert that can be
acknowledged, temporarily silenced, and resolved with auditable timestamps.
The `create_qa_alert` command provides a clearly-labelled, non-paging fixture
that supports the real lifecycle. These tests lock the safety guarantees:
never pages, idempotent duplicate, terminal cleanup.
"""

from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from tasks.models import Item


class CreateQaAlertTests(TestCase):
    def _run(self, *args, **kwargs):
        out = StringIO()
        err = StringIO()
        call_command('create_qa_alert', *args, stdout=out, stderr=err, **kwargs)
        return out.getvalue(), err.getvalue()

    def test_creates_clearly_labelled_non_paging_alert(self):
        out, _ = self._run('--assignee-email', 'qa@shizuha.com')
        self.assertIn('Created QA fixture alert', out)
        self.assertIn('No notification dispatched', out)
        alert = Item.objects.get(mode='alert', source='qa-fixture')
        self.assertEqual(alert.status, 'firing')
        self.assertEqual(alert.assignee_email, 'qa@shizuha.com')
        self.assertTrue(alert.title.startswith('[QA FIXTURE]'))
        self.assertEqual(alert.labels.get('qa_fixture'), 'true')

    def test_rejects_title_without_qa_fixture_label(self):
        with self.assertRaises(SystemExit) as ctx:
            self._run('--title', 'NOT A FIXTURE')
        self.assertEqual(ctx.exception.code, 2)
        self.assertEqual(Item.objects.filter(mode='alert', source='qa-fixture').count(), 0)

    def test_idempotent_duplicate_dedups_to_same_alert(self):
        self._run('--assignee-email', 'qa@shizuha.com')
        out, _ = self._run('--assignee-email', 'qa@shizuha.com')
        self.assertIn('Deduplicated to existing', out)
        self.assertEqual(Item.objects.filter(mode='alert', source='qa-fixture').count(), 1)

    def test_resolve_marks_alert_resolved(self):
        self._run('--assignee-email', 'qa@shizuha.com')
        alert = Item.objects.get(mode='alert', source='qa-fixture')
        out, _ = self._run('--resolve', '--source-id', alert.source_id)
        self.assertIn('Resolved QA fixture alert', out)
        alert.refresh_from_db()
        self.assertEqual(alert.status, 'resolved')
        self.assertIsNotNone(alert.resolved_at)

    def test_cleanup_deletes_alert(self):
        self._run('--assignee-email', 'qa@shizuha.com')
        alert = Item.objects.get(mode='alert', source='qa-fixture')
        out, _ = self._run('--cleanup', '--source-id', alert.source_id)
        self.assertIn('Deleted QA fixture alert', out)
        self.assertEqual(Item.objects.filter(mode='alert', source='qa-fixture').count(), 0)

    def test_resolve_missing_alert_exits_nonzero(self):
        with self.assertRaises(SystemExit) as ctx:
            self._run('--resolve', '--source-id', 'qa-does-not-exist')
        self.assertEqual(ctx.exception.code, 1)


class QaAlertApiDenialTests(TestCase):
    """PLS-817 negative coverage: a non-admin cannot route a QA fixture to a
    project/team via the public submit endpoint (project-routed alerts require
    auth + project access)."""

    def setUp(self):
        from django.contrib.auth import get_user_model
        from tasks.models import Project

        User = get_user_model()
        self.user = User.objects.create_user(
            username='qa-nonadmin', email='nonadmin@example.com', password='x',
        )
        self.project = Project.objects.create(
            name='QA Fixture', key='QAFIX', org_id=1, access_level='private',
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_non_admin_project_routed_alert_is_denied(self):
        response = self.client.post('/api/alerts/', {
            'title': '[QA FIXTURE] denied',
            'source': 'qa-fixture',
            'project_id': self.project.id,
            'assignment_group': 'engineering',
        }, format='json')
        # Non-admin without project access is denied (404 project unavailable).
        self.assertIn(response.status_code, (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND))
        self.assertEqual(Item.objects.filter(mode='alert', source='qa-fixture').count(), 0)
