"""PLAT-11174: hermetic tests for the periodic teamless-ITEM reconcile.

Covered invariants:
  1. NON_DEMAND_STATUSES excludes terminal cycles and the owned-but-not-active
     roadmap — only ACTIVE queue-eligible rows are scanned.
  2. Derivable rows get their group stamped with provenance ledgered in
     metadata (what/when/why) — nothing blind-stamped.
  3. Unresolvable rows are PARKED and LEDGERED, never invented.
  4. The summary carries the ``pulse_teamless_items`` gauge counts.

The resolver is patched at the module level, so no DB or settings are needed
(plain unittest, subprocess-friendly).
"""
import unittest
from unittest.mock import MagicMock, patch

from pathlib import Path

from tasks import teamless_reconcile as tr
from tasks.models import Item


class _FakeQS(list):
    """Minimal iterator-protocol stand-in for a queryset."""
    def iterator(self):
        return iter(self)


class TeamlessReconcileUnitTests(unittest.TestCase):
    def _item(self, key='PLAT-10025', priority='high'):
        item = Item(priority=priority, title='stranded urgent work')
        item.item_key = key
        return item

    def test_boot_registers_teamless_reconcile_schedule(self):
        apps = Path(__file__).resolve().parents[1].joinpath('apps.py').read_text()
        self.assertIn('setup_teamless_reconcile_schedule()', apps)

    def test_non_demand_statuses_exclude_roadmap_and_terminal(self):
        for status in ('backlog', 'completed', 'cancelled', 'deferred', 'closed'):
            self.assertIn(status, tr.NON_DEMAND_STATUSES)
        for status in ('todo', 'in_progress', 'in_review', 'open'):
            self.assertNotIn(status, tr.NON_DEMAND_STATUSES)

    def test_backfills_derivable_group_with_provenance(self):
        item = self._item()
        item._state.adding = False
        item.save = MagicMock()

        with patch.object(tr, 'teamless_active_qs', return_value=_FakeQS([item])), \
             patch('tasks.services.workflow_service.WorkflowService', MagicMock(
                 _derive_owner_team=MagicMock(return_value='engineering'))):
            summary = tr.reconcile_teamless_items(dry_run=False)

        self.assertEqual(summary['backfilled'], 1)
        self.assertEqual(summary['parked'], 0)
        self.assertEqual(summary['gauge'], {'high': 1})
        self.assertIn('assignment_group_reconcile', item.metadata)
        self.assertEqual(item.metadata['assignment_group_reconcile']['source'], 'engineering')
        self.assertEqual(item.assignment_group, 'engineering')
        item.save.assert_called_once()

    def test_parks_unresolvable_without_inventing(self):
        item = self._item('MAIL-193', priority='urgent')
        item._state.adding = False
        item.save = MagicMock()

        with patch.object(tr, 'teamless_active_qs', return_value=_FakeQS([item])), \
             patch('tasks.services.workflow_service.WorkflowService', MagicMock(
                 _derive_owner_team=MagicMock(return_value=None))):
            summary = tr.reconcile_teamless_items(dry_run=False)

        self.assertEqual(summary['parked'], 1)
        self.assertEqual(summary['backfilled'], 0)
        self.assertIsNone(item.assignment_group)
        self.assertEqual(item.metadata, {})   # nothing ledgered as resolved
        item.save.assert_not_called()

    def test_dry_run_scans_without_writing(self):
        item = self._item('HIVE-2819')
        item._state.adding = False
        item.save = MagicMock()

        with patch.object(tr, 'teamless_active_qs', return_value=_FakeQS([item])), \
             patch('tasks.services.workflow_service.WorkflowService', MagicMock(
                 _derive_owner_team=MagicMock(return_value='review'))):
            summary = tr.reconcile_teamless_items(dry_run=True)

        self.assertEqual(summary['backfilled'], 1)
        self.assertEqual(summary['dry_run'], True)
        self.assertIsNone(item.assignment_group)
        item.save.assert_not_called()

    def test_resolver_unavailable_scans_only(self):
        # Patching the lazy import target to raise exercises the degraded lane.
        with patch.object(tr, 'teamless_active_qs', return_value=_FakeQS([self._item()])), \
             patch('builtins.__import__', side_effect=ImportError('resolver down')):
            summary = tr.reconcile_teamless_items(dry_run=False)

        self.assertEqual(summary['scanned'], 1)
        self.assertEqual(summary['parked'], 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
