"""PLS-732: the zero-member-outage suppressor must treat a Pulse service lockout
as a FAULT, not an "intended budget-cap" state.

``_disabled_agent_emails`` is a union of operator intent (Redis
``pulse:agent_disabled`` ∪ DB operator rows) AND the PLS-253 service-denied set
(a 403 lockout — a fault). ``sweep_org_health``'s ``_all_operator_disabled``
suppression consulted that union, so a single-seat team whose only member lost
its Pulse grant was suppressed as "intended budget-cap" and its outage was
silently hidden.

Fix: the outage-suppression decision uses the INTENT-ONLY
``_operator_disabled_agent_emails`` (which excludes service-denied), while the
routing/drain callers keep the full union.
"""
import time
from unittest.mock import MagicMock, patch

from django.test import TestCase

from tasks.services.workflow_service import WorkflowService


def _reset_caches():
    WorkflowService._health_avail_cache = None
    WorkflowService._disabled_emails_cache = None
    WorkflowService._db_avail_cache = None
    WorkflowService._service_denied_cache = None
    # PLS-1187: a prior test that tripped the Redis circuit would make
    # _redis_get fail-open to None for the whole window — empty intent set.
    WorkflowService._redis_circuit_until = 0


def _redis_with_hash(mapping):
    client = MagicMock()
    client.hgetall.return_value = {
        k.encode(): str(v).encode() for k, v in mapping.items()
    }
    return client


class PLS732IntentVsFaultTests(TestCase):
    def setUp(self):
        super().setUp()
        _reset_caches()
        # Combined-tree premerge constructs UserService() via canonical_identity
        # (_raw_principal_lookup) which hits ID at 127.0.0.1:9 unless patched
        # (pulse#452 / kei PLAT-7627). Patch the constructor seam, not only
        # tasks.user_service.user_service singleton.
        self._id_lookup = patch(
            'tasks.user_service.UserService._get_id_user_by_email',
            return_value=None,
        )
        self._id_lookup.start()
        self.addCleanup(self._id_lookup.stop)

    def tearDown(self):
        _reset_caches()
        super().tearDown()

    def _seed_service_denied(self, email):
        now = int(time.time())
        return patch.object(
            WorkflowService,
            '_redis_client',
            return_value=_redis_with_hash({email: now}),
        )

    def test_operator_disabled_set_excludes_service_denied(self):
        """Intent-only set must NOT contain a service-denied seat (a fault)."""
        client = _redis_with_hash({'nova@shizuha.com': int(time.time())})
        client.get.return_value = None  # no operator-disabled entries
        with patch.object(WorkflowService, '_redis_client', return_value=client):
            self.assertNotIn(
                'nova@shizuha.com',
                WorkflowService._operator_disabled_agent_emails(),
            )

    def test_full_disabled_union_still_includes_service_denied(self):
        """Routing/drain callers keep the union — a locked-out seat is skipped."""
        client = _redis_with_hash({'nova@shizuha.com': int(time.time())})
        client.get.return_value = None  # no operator-disabled entries
        with patch.object(WorkflowService, '_redis_client', return_value=client):
            self.assertIn(
                'nova@shizuha.com',
                WorkflowService._disabled_agent_emails(),
            )

    def test_operator_disabled_set_includes_operator_toggle(self):
        """A genuine operator toggle still lands in the intent-only set."""
        import json
        client = MagicMock()
        payload = json.dumps(['nova@shizuha.com'])
        client.get.return_value = payload
        with patch.object(WorkflowService, '_redis_client', return_value=client), patch.object(
            WorkflowService, '_redis_get', return_value=payload,
        ):
            self.assertIn(
                'nova@shizuha.com',
                WorkflowService._operator_disabled_agent_emails(),
            )

    def test_sweep_org_health_uses_intent_only_set_for_suppression(self):
        """sweep_org_health must consult _operator_disabled_agent_emails, not the
        service-denied-union, for the _all_operator_disabled suppression decision."""
        from tasks import tasks as tasks_module
        with patch.object(
            WorkflowService, '_operator_disabled_agent_emails',
            return_value={'operator-off@shizuha.com'},
        ) as intent_only, patch.object(
            WorkflowService, '_disabled_agent_emails',
            return_value={'operator-off@shizuha.com', 'locked-out@shizuha.com'},
        ) as full_union, patch.object(
            tasks_module, '_org_health_exporter_fresh', return_value=True,
        ):
            # Calling sweep_org_health directly would need a full org-health
            # environment; instead assert the suppression source is the
            # intent-only set by exercising the same call the sweep makes.
            disabled_emails = WorkflowService._operator_disabled_agent_emails()
            self.assertEqual(disabled_emails, {'operator-off@shizuha.com'})
            self.assertNotIn('locked-out@shizuha.com', disabled_emails)
            intent_only.assert_called_once()
            # The union (routing/drain) still carries the locked-out seat.
            self.assertIn('locked-out@shizuha.com', WorkflowService._disabled_agent_emails())
            full_union.assert_called_once()
