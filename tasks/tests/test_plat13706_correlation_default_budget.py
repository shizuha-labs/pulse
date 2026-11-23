"""PLAT-13706 recurrence: correlation without an explicit budget must stay bounded.

pulse#1016 (8s ``future.result`` cap) bounds ONE compare wait, not the
candidate loop. The ingest path reaches ``_find_correlated_deploy_event``
without ``budget_end`` (focused-QA spawn →
``_focused_qa_origin_deploy_evidence_carried``), on a gunicorn SYNC worker.
Unbounded candidates × per-compare waits starve the arbiter heartbeat and the
worker is aborted mid-request (500 empty body, no event row). The loop must
default to the same wall-clock budget the gate evaluation establishes;
explicit ``budget_end`` callers (the gate) are unchanged.
"""
import os
import time
from unittest import mock

from django.test import SimpleTestCase

from tasks.services.workflow_service import WorkflowService

MERGED_SHA = 'a' * 40
DEPLOYED_SHA = 'd' * 40


class _Event:
    """Minimal DeploymentEvent duck-type for the correlation loop."""

    def __init__(self):
        self.pk = 101
        self.state = 'verified_by_qa'
        self.smoke_result = 'passed'
        self.commit_sha = DEPLOYED_SHA
        self.repo = 'shizuha-labs/pulse'
        self.source_task_keys = ['PLAT-13706']
        self.idempotency_key = 'k'


class _FakeQS:
    def __init__(self, events):
        self._events = events

    def filter(self, *args, **kwargs):
        # The widened pass carries project__org_id in its filter kwargs; the
        # key-named pass does not. Widened candidates stay empty here.
        if 'project__org_id' in kwargs:
            return _FakeQS([])
        return self

    def order_by(self, *args):
        return self

    def __getitem__(self, item):
        return list(self._events)

    def __iter__(self):
        return iter(self._events)


class _Manager:
    def __init__(self, events):
        self._events = events

    def filter(self, *args, **kwargs):
        return _FakeQS(self._events)


class CorrelationDefaultBudgetTests(SimpleTestCase):
    def _item(self):
        item = mock.Mock()
        item.item_key = 'PLAT-13706'
        item.project.org_id = 7
        return item

    def _heads(self):
        return [{'repo': 'shizuha-labs/pulse', 'sha': MERGED_SHA}]

    def test_no_budget_end_defaults_to_wall_clock_budget(self):
        # A descendant-SHA candidate forces the ancestry leg. When the caller
        # passes no budget_end, the loop must hand the proof a bounded
        # budget (now + PULSE_COMPARE_WALL_CLOCK_SECONDS) — never None.
        captured = {}

        def fake_proof(owner, repo, base_sha, deployed_sha, budget_end=None):
            captured['budget_end'] = budget_end
            return False

        item = self._item()
        with mock.patch(
            'tasks.models.DeploymentEvent.objects',
            _Manager([_Event()]),
        ), mock.patch.dict(os.environ, {'PULSE_COMPARE_WALL_CLOCK_SECONDS': '20'}), \
                mock.patch.object(
                    WorkflowService,
                    '_prove_deployed_descends_from_merged_head',
                    staticmethod(fake_proof),
                ):
            start = time.monotonic()
            found = WorkflowService._find_correlated_deploy_event(
                item, self._heads(),
            )
        self.assertIsNone(found)
        self.assertIsNotNone(captured.get('budget_end'))
        # Budget is bounded: now + 20s, with slack for scheduling.
        self.assertLessEqual(
            captured['budget_end'] - start, 20.0 + 1.0,
        )
        self.assertGreater(captured['budget_end'] - start, 19.0)

    def test_explicit_budget_end_caller_is_unchanged(self):
        # The gate passes its own budget_end; the default must not override it.
        captured = {}

        def fake_proof(owner, repo, base_sha, deployed_sha, budget_end=None):
            captured['budget_end'] = budget_end
            return False

        explicit = time.monotonic() + 1234.0
        item = self._item()
        with mock.patch(
            'tasks.models.DeploymentEvent.objects',
            _Manager([_Event()]),
        ), mock.patch.object(
            WorkflowService,
            '_prove_deployed_descends_from_merged_head',
            staticmethod(fake_proof),
        ):
            WorkflowService._find_correlated_deploy_event(
                item, self._heads(), budget_end=explicit,
            )
        self.assertEqual(captured.get('budget_end'), explicit)

    def test_unbudgeted_trickling_origin_fails_closed_fast(self):
        # End-to-end negative regression for the ingest-path shape: a
        # trickling Origin (slow-drip compare) must not hold the correlation
        # loop past the default wall-clock budget. The result is a fail-closed
        # None — never a worker death.
        import requests

        def trickle(url, headers=None, timeout=None):
            time.sleep(4)
            return type('R', (), {'status_code': 200, 'json': lambda s: {
                'commits': [], 'files': [], 'total_commits': 0,
            }, 'raise_for_status': lambda s: None})()

        item = self._item()
        with mock.patch(
            'tasks.models.DeploymentEvent.objects',
            _Manager([_Event()]),
        ), mock.patch.dict(os.environ, {
            'PULSE_COMPARE_WALL_CLOCK_SECONDS': '1',
            'ORIGIN_URL': 'https://origin.shizuha.com',
            'FORGEJO_TOKEN': 'test-token',
        }), mock.patch.object(requests, 'get', side_effect=trickle):
            start = time.monotonic()
            found = WorkflowService._find_correlated_deploy_event(
                item, self._heads(),
            )
            elapsed = time.monotonic() - start
        self.assertIsNone(found)
        self.assertLess(elapsed, 3.0)
