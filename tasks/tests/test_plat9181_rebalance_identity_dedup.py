"""PLAT-9181/9182: duplicate-aware rebalance pull.

The rebalance capacity-pull must skip items whose finding identity
(repo+package+CVE) already has an active or terminal record — the 09-18
incident re-pulled already-triaged duplicates (PLAT-9143/9144) onto a
security seat, burning two redundant disposition cycles. Fail-open for
unparseable/ambiguous items: the guard saves disposition cycles, it never
blocks a legitimate pull.

These are logic-level tests with the ORM mocked: the seat harness runs
sqlite while the production query targets Postgres (the CI gate's
job-local Postgres validates the real query path). The identity-extraction
helpers are pure Python and tested directly.
"""

from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from tasks.tasks import (
    _finding_identity_from_item,
    _finding_identity_has_record,
    _item_label_strings,
)

IDENT = ('shizuha-labs/drive', 'openssl', 'CVE-2026-1234')


def _item(pk='a', labels=None, tags='', status='todo', project_id='p1'):
    return SimpleNamespace(
        pk=pk, labels=labels if labels is not None else {},
        tags=tags, status=status, project_id=project_id,
    )


class _FakeQuery:
    """Mimics the chained queryset the dedup guard builds."""

    def __init__(self, candidates):
        self._candidates = candidates

    def exclude(self, pk):
        return self

    def filter(self, *a, **k):
        return self

    def annotate(self, *a, **k):
        return self

    def only(self, *a, **k):
        return self

    def __iter__(self):
        return iter(self._candidates)


class _FakeObjects:
    def __init__(self, candidates, org_id='org-1'):
        self._candidates = candidates
        self._org_id = org_id

    def _base(self, item):
        # The guard resolves org via Item._project_organization_id.
        return self

    def filter(self, *a, **k):
        return _FakeQuery(self._candidates)


class FindingIdentityExtractionTests(SimpleTestCase):
    def test_extracts_identity_from_task_label_array(self):
        item = _item(labels=['repo:shizuha-labs/drive', 'pkg:openssl', 'cve:CVE-2026-1234'])
        self.assertEqual(_finding_identity_from_item(item), IDENT)

    def test_extracts_identity_from_alert_label_dict(self):
        item = _item(labels={'cve': 'CVE-2026-1234', 'pkg': 'openssl'})
        self.assertEqual(_finding_identity_from_item(item), ('', 'openssl', 'CVE-2026-1234'))

    def test_extracts_identity_from_tags(self):
        item = _item(tags='cve:CVE-2026-1234,pkg:openssl,repo:shizuha-labs/drive')
        self.assertEqual(_finding_identity_from_item(item), IDENT)

    def test_no_cve_means_no_finding_identity(self):
        item = _item(labels=['pkg:openssl', 'repo:shizuha-labs/drive'])
        self.assertIsNone(_finding_identity_from_item(item))

    def test_plain_item_yields_none_and_empty_strings(self):
        item = _item()
        self.assertIsNone(_finding_identity_from_item(item))
        self.assertEqual(_item_label_strings(item), [])

    def test_dual_shape_dict_with_list_values(self):
        item = _item(labels={'labels': ['cve:CVE-2026-1234']})
        self.assertEqual(_finding_identity_from_item(item), ('', '', 'CVE-2026-1234'))


class _FakeItem:
    """Stands in for tasks.models.Item inside the guard: org resolution via
    the project, and an objects manager whose exclude() yields the fakes."""

    _candidates = []

    @classmethod
    def _project_organization_id(cls, project_id):
        return 'org-1' if project_id else None


class FindingIdentityRecordTests(SimpleTestCase):
    def _run(self, candidate, others):
        """Run the guard with the ORM chain mocked to yield `others`."""

        class _Manager:
            def exclude(self, pk):
                return _FakeQuery(others)

            def filter(self, *a, **k):
                return _FakeQuery(others)

        fake_item = type('FakeItemBound', (_FakeItem,), {'objects': _Manager()})
        with patch('tasks.models.Item', fake_item):
            return _finding_identity_has_record(candidate, IDENT)

    def test_active_duplicate_record_skips(self):
        other = _item(pk='r1', labels=['cve:CVE-2026-1234'], status='in_progress')
        self.assertTrue(self._run(_item(pk='c1', labels=['cve:CVE-2026-1234']), [other]))

    def test_terminal_duplicate_record_skips(self):
        other = _item(pk='r2', labels=['cve:CVE-2026-1234'], status='done')
        self.assertTrue(self._run(_item(pk='c1', labels=['cve:CVE-2026-1234']), [other]))

    def test_backlog_record_does_not_skip(self):
        # Backlog = owned-but-not-active roadmap, not a triage record.
        other = _item(pk='r3', labels=['cve:CVE-2026-1234'], status='backlog')
        self.assertFalse(self._run(_item(pk='c1', labels=['cve:CVE-2026-1234']), [other]))

    def test_unique_finding_pulls(self):
        other = _item(pk='r4', labels=['cve:CVE-2026-9999'], status='done')
        self.assertFalse(self._run(_item(pk='c1', labels=['cve:CVE-2026-1234']), [other]))

    def test_non_finding_item_pulls(self):
        other = _item(pk='r5', labels={}, status='in_progress')
        self.assertFalse(self._run(_item(pk='c1', labels=['cve:CVE-2026-1234']), [other]))

    def test_repo_mismatch_does_not_skip(self):
        other = _item(pk='r6', labels=['repo:shizuha-labs/other', 'cve:CVE-2026-1234'], status='done')
        self.assertFalse(self._run(_item(pk='c1', labels=['cve:CVE-2026-1234']), [other]))

    def test_cve_only_record_skips(self):
        # A record carrying only the CVE (tags, no repo/pkg) still dedups —
        # the CVE is the discriminating token.
        other = _item(pk='r7', tags='cve:CVE-2026-1234', status='done')
        self.assertTrue(self._run(_item(pk='c1', labels=['cve:CVE-2026-1234']), [other]))

    def test_lookup_failure_fails_open(self):
        # A guard lookup error must never break the rebalance pull.

        class _ExplodingItem:
            @classmethod
            def _project_organization_id(cls, project_id):
                raise RuntimeError('db down')

        with patch('tasks.models.Item', _ExplodingItem):
            self.assertFalse(_finding_identity_has_record(
                _item(pk='c1', labels=['cve:CVE-2026-1234']), IDENT,
            ))
