"""PLS-623: reference-data fixture agreement + natural-key round-trip.

Guards the committed ``tasks/fixtures/reference_data.json`` against drift from
the migration-seeded corpus, and proves the natural-key managers resolve the
tuples the fixture uses. The runner-level restore (HermeticDiscoverRunner +
TransactionTestCase flush wrap) is the production path; these tests pin the
fixture content and the natural-key contract so a silent drift becomes a
caught failure rather than a second-run constraint error.
"""
from __future__ import annotations

import json
from pathlib import Path

from django.core.management import call_command
from django.test import TestCase, TransactionTestCase

from tasks.models import (
    BusinessCalendar,
    IssueType,
    IssueTypeScheme,
    IssueTypeSchemeMapping,
    IssueTypeScreenScheme,
    NotificationScheme,
    NotificationSchemeEvent,
    PermissionGrant,
    PermissionScheme,
    Resolution,
    Screen,
    ScreenScheme,
    Status,
    TeamRoster,
    Workflow,
    WorkflowScheme,
    WorkflowSchemeMapping,
    WorkflowStatus,
    WorkflowTransition,
)

# Expected counts from a fully-migrated DB (measured at fixture generation).
# Bump these deliberately when a seed migration adds reference rows, and
# regenerate the fixture in the same commit.
# Counts measured against branch leaf through 0164 (PLAT-5355 Triage to DevOps
# on autonomous-bug + prior 0149 corpus).
# Regenerate fixture + these numbers together whenever a seed migration
# lands.
# PLS-825 (0166): focused-qa workflow seed adds resolution +4 (passed/failed/
# superseded/correction_requested), workflow +1 (focused-qa), workflowstatus +6,
# workflowtransition +14.
# PLS-719: resolved_elsewhere status +1, workflowstatus +1, workflowtransition +2.
# PLS-714 (0173): missing escalation lanes seeded for every workflow present at
# migration time — feature-request-rfc / autonomous-dev / autonomous-bug /
# focused-qa get the full set, the older workflows get the lanes missing from
# their 0053-era seed (incl. per-active-status rows for statuses added later,
# e.g. simple's resolved_elsewhere) — workflowtransition +73 → 263 (main's
# Triage-to-DevOps lane from #221-era advance included in the base).
# PLAT-9681 (0185): fleet-wide terminal reopen — Reopen edge from every
# terminal status across the 8 seeded workflows (bespoke-covered terminals
# skipped, self-reopen targets skipped) — workflowtransition +20 → 283.
EXPECTED_COUNTS = {
    'status': 33,
    'resolution': 14,
    'workflow': 8,
    'workflowstatus': 100,
    'workflowtransition': 283,
    'workflowscheme': 1,
    'workflowschememapping': 3,
    'issuetype': 7,
    'issuetypescheme': 1,
    'issuetypeschememapping': 7,
    'screen': 3,
    'screenscheme': 1,
    'issuetypescreenscheme': 1,
    'permissionscheme': 1,
    'permissiongrant': 23,
    'notificationscheme': 1,
    'notificationschemeevent': 11,
    'businesscalendar': 1,
    'teamroster': 16,
}

FIXTURE_PATH = (
    Path(__file__).resolve().parent.parent / 'fixtures' / 'reference_data.json'
)


class ReferenceDataFixtureAgreementTests(TestCase):
    """Fixture content agrees with the migration-seeded corpus."""

    def test_committed_fixture_exists_and_has_expected_shape(self):
        self.assertTrue(FIXTURE_PATH.is_file(), f'missing fixture at {FIXTURE_PATH}')
        payload = json.loads(FIXTURE_PATH.read_text())
        self.assertIsInstance(payload, list)
        self.assertGreater(len(payload), 0)

        # Natural-primary: no pk fields (UUID PKs are per-database).
        for obj in payload:
            self.assertNotIn(
                'pk', obj,
                f'fixture object carries a pk — regenerate with --natural-primary: {obj.get("model")}',
            )

        by_model: dict[str, int] = {}
        for obj in payload:
            model = obj['model'].split('.')[-1]
            by_model[model] = by_model.get(model, 0) + 1
        self.assertEqual(by_model, EXPECTED_COUNTS)

    def _reference_counts(self):
        return {
            'status': Status.objects.count(),
            'resolution': Resolution.objects.count(),
            'workflow': Workflow.objects.count(),
            'workflowstatus': WorkflowStatus.objects.count(),
            'workflowtransition': WorkflowTransition.objects.count(),
            'workflowscheme': WorkflowScheme.objects.count(),
            'workflowschememapping': WorkflowSchemeMapping.objects.count(),
            'issuetype': IssueType.objects.count(),
            'issuetypescheme': IssueTypeScheme.objects.count(),
            'issuetypeschememapping': IssueTypeSchemeMapping.objects.count(),
            'screen': Screen.objects.count(),
            'screenscheme': ScreenScheme.objects.count(),
            'issuetypescreenscheme': IssueTypeScreenScheme.objects.count(),
            'permissionscheme': PermissionScheme.objects.count(),
            'permissiongrant': PermissionGrant.objects.count(),
            'notificationscheme': NotificationScheme.objects.count(),
            'notificationschemeevent': NotificationSchemeEvent.objects.count(),
            'businesscalendar': BusinessCalendar.objects.count(),
            'teamroster': TeamRoster.objects.count(),
        }

    def test_fixture_load_is_idempotent_against_migration_seed(self):
        """Loading the fixture into an already-seeded DB must not duplicate rows."""
        before = self._reference_counts()
        # Migration seed must already match the fixture (same source of truth).
        self.assertEqual(before, EXPECTED_COUNTS)

        call_command('loaddata', 'reference_data.json', verbosity=0)

        after = self._reference_counts()
        self.assertEqual(after, before, 'loaddata duplicated or dropped reference rows')

    def test_natural_key_lookups_resolve_seeded_rows(self):
        """Natural-key managers resolve the tuples the fixture emits."""
        self.assertEqual(Status.objects.get_by_natural_key('open').slug, 'open')
        self.assertEqual(Resolution.objects.get_by_natural_key('fixed').slug, 'fixed')
        self.assertEqual(Workflow.objects.get_by_natural_key('simple').slug, 'simple')

        ws = WorkflowStatus.objects.get_by_natural_key('simple', 'todo')
        self.assertEqual(ws.workflow.slug, 'simple')
        self.assertEqual(ws.status.slug, 'todo')

        t = WorkflowTransition.objects.get_by_natural_key(
            'simple', 'todo', 'Start Progress',
        )
        self.assertEqual(t.name, 'Start Progress')
        self.assertEqual(t.to_status, 'in_progress')

        # Round-trip: model.natural_key() matches get_by_natural_key args.
        self.assertEqual(Status.objects.get_by_natural_key(*Status.objects.get(slug='done').natural_key()).slug, 'done')
        self.assertEqual(
            WorkflowTransition.objects.get_by_natural_key(*t.natural_key()).pk,
            t.pk,
        )


class ReferenceDataFlushRestoreTests(TransactionTestCase):
    """TransactionTestCase flush must leave reference data intact (runner patch)."""

    def test_reference_data_survives_transaction_testcase_flush(self):
        # This test itself is a TransactionTestCase: its teardown flushes, then
        # the runner patch reloads the fixture. The assertion here runs BEFORE
        # teardown, so we only prove the fixture is loadable mid-test. The
        # cross-test survival is proven by the two-run retained-DB acceptance.
        call_command('loaddata', 'reference_data.json', verbosity=0)
        self.assertEqual(Workflow.objects.count(), EXPECTED_COUNTS['workflow'])
        self.assertEqual(Status.objects.count(), EXPECTED_COUNTS['status'])
        self.assertTrue(Workflow.objects.filter(slug='simple').exists())
