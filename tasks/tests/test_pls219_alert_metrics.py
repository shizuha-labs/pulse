import json
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from tasks.models import Item, ItemActivity
from tasks.services.stage_metrics import team_throughput
from tasks.signals import TRACKED_FIELDS


class PLS219AlertMetricsTests(TestCase):
    def test_team_throughput_excludes_alert_mode_rows(self):
        task = Item.objects.create(mode='task', title='Real task', status='open', assignment_group='engineering')
        alert = Item.objects.create(mode='alert', title='Stale alert', status='firing', assignment_group='devops')
        field = TRACKED_FIELDS.get('status', 'Status')
        now = timezone.now()
        ItemActivity.objects.create(item=task, field_name=field, old_value='open', new_value='in_progress', created_at=now, user_id=1)
        ItemActivity.objects.create(item=alert, field_name=field, old_value='firing', new_value='resolved', created_at=now, user_id=1)

        rows = team_throughput(days=1, now=now)

        self.assertIn(('engineering', 'open'), {(r['team'], r['status']) for r in rows})
        self.assertNotIn(('devops', 'firing'), {(r['team'], r['status']) for r in rows})

    @patch('tasks.management.commands.reconcile_stale_alerts.safe_urlopen')
    def test_reconcile_stale_alerts_resolves_only_fingerprints_not_live(self, mock_urlopen):
        class Response:
            def __enter__(self): return self
            def __exit__(self, exc_type, exc, tb): return False
            def read(self):
                return json.dumps([
                    {'status': {'state': 'active'}, 'fingerprint': 'live-fp'},
                ]).encode()

        mock_urlopen.return_value = Response()
        stale = Item.objects.create(mode='alert', title='old', status='firing', source='alertmanager', source_id='stale-fp')
        live = Item.objects.create(mode='alert', title='live', status='firing', source='alertmanager', source_id='live-fp')
        other_source = Item.objects.create(mode='alert', title='other', status='firing', source='other', source_id='stale-fp')

        call_command('reconcile_stale_alerts', '--alertmanager-url', 'http://alertmanager.test', '--source', 'alertmanager')

        stale.refresh_from_db(); live.refresh_from_db(); other_source.refresh_from_db()
        self.assertEqual(stale.status, 'resolved')
        self.assertEqual(stale.resolved_by, 'reconcile:pls-219')
        self.assertEqual(live.status, 'firing')
        self.assertEqual(other_source.status, 'firing')
