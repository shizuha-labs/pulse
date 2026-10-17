"""PLAT-944: phantom-replay guard in AlertDeduplicationService.submit_alert.

Alertmanager re-fired its persisted June-20 alerts after a restart (identical
startsAt). The active-only dedup missed them (originals had resolved) so each
replay spawned a new alert + DM. The startsAt guard must dedup a same-startsAt
replay while still letting a genuine re-fire (new startsAt) create a new alert.
"""
from django.test import TestCase
from tasks.services.deduplication import AlertDeduplicationService as D
from tasks.models import Item


class PLAT944ReplayGuardTests(TestCase):
    def _alert(self, starts_at, fp='fp-1'):
        return {
            'item_type': 'alert.critical', 'title': 'KubeJobFailed', 'source': 'alertmanager',
            'source_id': fp, 'severity': 'critical', 'labels': {'alertname': 'KubeJobFailed'},
            'payload': {'starts_at': starts_at},
        }

    def test_phantom_replay_deduped_no_new_alert(self):
        item, is_new = D.submit_alert(self._alert('2026-06-20T19:21:44.696Z'))
        self.assertTrue(is_new)
        item.status = 'resolved'
        item.save(update_fields=['status'])
        # same fingerprint + same startsAt as a now-resolved alert => replay
        item2, is_new2 = D.submit_alert(self._alert('2026-06-20T19:21:44.696Z'))
        self.assertFalse(is_new2, 'phantom replay must not create a new alert')
        self.assertEqual(item2.id, item.id)
        self.assertEqual(Item.objects.filter(mode='alert', source='alertmanager', source_id='fp-1').count(), 1)

    def test_genuine_refire_creates_new_alert(self):
        item, is_new = D.submit_alert(self._alert('2026-06-20T19:21:44.696Z'))
        self.assertTrue(is_new)
        item.status = 'resolved'
        item.save(update_fields=['status'])
        # same fingerprint but NEW startsAt => genuine re-fire => new alert
        item2, is_new2 = D.submit_alert(self._alert('2026-06-27T10:00:00.000Z'))
        self.assertTrue(is_new2, 'genuine re-fire (new startsAt) must create a new alert')
        self.assertEqual(Item.objects.filter(mode='alert', source='alertmanager', source_id='fp-1').count(), 2)

    def test_active_dedup_still_collapses_repeat_firing(self):
        item, is_new = D.submit_alert(self._alert('2026-06-20T19:21:44.696Z'))
        self.assertTrue(is_new)
        # still firing + same startsAt => normal active dedup (occurrence bump), not new
        item2, is_new2 = D.submit_alert(self._alert('2026-06-20T19:21:44.696Z'))
        self.assertFalse(is_new2)
        self.assertEqual(item2.id, item.id)
