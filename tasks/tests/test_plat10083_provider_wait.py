"""PLAT-10083: provider_wait queue-lane annotation contract.

Covers the five acceptance criteria of the task plus the two review defects
(revi REQUEST CHANGES, 2026-09-28):

- P1-a regression: the fresh-terminal-ANDON guard reads ``Comment.content``
  (a ``body`` lookup raises FieldError on every auto-clear evaluation).
- P1-b regression: every successful set (create, idempotent refresh, re-arm)
  persists ``last_set_at``, so the guard trips only for stops whose set call
  never happened — never permanently blocks the stop → re-arm → recover →
  succeed sequence.
"""
from __future__ import annotations

from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from tasks.models import Comment, Item, Project, ProviderWaitMarker
from tasks.services.provider_wait import (
    PROVIDER_WAIT_SIGNATURES,
    ProviderWaitError,
    auto_clear_provider_wait,
    manager_clear_provider_wait,
    provider_wait_visible_qs,
    set_provider_wait,
)
from .base import AuthenticatedAPITestCase


class ProviderWaitServiceTest(AuthenticatedAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = Project.objects.create(
            name='Provider wait', key='PWT', org_id=1, access_level='open',
        )
        self.item = Item.objects.create(
            project=self.project, title='provider-wait fixture', status='pending',
        )

    def post_andon(self, text='🔴 ANDON [stop_signature: upstream_unavailable]'):
        return Comment.objects.create(
            item=self.item, author_id=0, author_email='runtime@shizuha.io',
            author_name='runtime', content=text,
        )

    def test_non_provider_signature_typed_rejected(self):
        for bad in ('context_overflow', 'auth_failed', 'provider_unclassified', '', None):
            with self.assertRaises(ProviderWaitError):
                set_provider_wait(self.item, signature_class=bad)
        self.assertFalse(ProviderWaitMarker.objects.filter(item=self.item).exists())
        self.assertEqual(provider_wait_visible_qs().count(), 0)

    def test_set_then_auto_clear_on_success(self):
        marker, created = set_provider_wait(
            self.item, signature_class='upstream_unavailable',
        )
        self.assertTrue(created)
        self.assertEqual(provider_wait_visible_qs().count(), 1)
        marker, cleared, reason = auto_clear_provider_wait(self.item)
        self.assertTrue(cleared)
        self.assertEqual(reason, '')
        self.assertEqual(marker.state, 'inactive')
        self.assertIsNotNone(marker.auto_cleared_at)
        self.assertEqual(provider_wait_visible_qs().count(), 0)

    def test_auto_clear_refused_while_fresh_terminal_andon(self):
        """P1-a regression: the guard must evaluate, not FieldError."""
        set_provider_wait(self.item, signature_class='quota_exhausted')
        self.post_andon()  # stop landed AFTER the marker's last set
        marker, cleared, reason = auto_clear_provider_wait(self.item)
        self.assertFalse(cleared)
        self.assertEqual(reason, 'fresh_terminal_andon')
        marker.refresh_from_db()
        self.assertEqual(marker.state, 'active')

    def test_stale_quoted_andon_does_not_refuse(self):
        set_provider_wait(self.item, signature_class='upstream_unavailable')
        stale = Comment.objects.create(
            item=self.item, author_id=0, author_email='r@shizuha.io',
            author_name='reviewer',
            content='note: the stamp [stop_signature: upstream_unavailable] '
                    'was discussed yesterday',
        )
        # created_at is auto_now_add — backdate via update to predate the set.
        Comment.objects.filter(pk=stale.pk).update(
            created_at=timezone.now() - timezone.timedelta(hours=1),
        )
        marker, cleared, reason = auto_clear_provider_wait(self.item)
        self.assertTrue(cleared)

    def test_refresh_persists_last_set_at(self):
        """P1-b regression: idempotent refresh must move last_set_at."""
        marker, created = set_provider_wait(
            self.item, signature_class='upstream_unavailable',
        )
        self.assertTrue(created)
        first_set_at = marker.last_set_at
        marker, changed = set_provider_wait(
            self.item, signature_class='upstream_unavailable',
        )
        self.assertFalse(changed)
        marker.refresh_from_db()
        self.assertGreater(marker.last_set_at, first_set_at)

    def test_stop_rearm_recover_succeed_sequence(self):
        """The core reversibility sequence must terminate in a clear."""
        set_provider_wait(self.item, signature_class='upstream_unavailable')
        self.post_andon('[stop_signature: quota_exhausted]')
        marker, changed = set_provider_wait(
            self.item, signature_class='quota_exhausted',
        )
        self.assertTrue(changed)
        self.assertEqual(marker.generation, 2)
        marker.refresh_from_db()
        guard_stamp = marker.last_set_at
        # The re-arm set covered this stop — the guard must NOT see it as fresher.
        marker2, cleared, reason = auto_clear_provider_wait(self.item)
        self.assertTrue(cleared, f'auto-clear refused ({reason}) in the '
                                 f'core sequence; guard stamp={guard_stamp}')

    def test_manager_clear_provenance(self):
        set_provider_wait(self.item, signature_class='quota_exhausted')
        marker, cleared = manager_clear_provider_wait(
            self.item, action='take_over', actor='kei@shizuha.io',
        )
        self.assertTrue(cleared)
        marker.refresh_from_db()
        self.assertEqual(marker.state, 'inactive')
        self.assertEqual(marker.manager_cleared_by, 'kei@shizuha.io')
        self.assertEqual(marker.manager_cleared_action, 'take_over')
        with self.assertRaises(ProviderWaitError):
            manager_clear_provider_wait(self.item, action='nuke', actor='x')
        # Valid action on an already-inactive marker is a typed no-op:
        # returns (None, False) — it must NOT raise.
        marker2, cleared2 = manager_clear_provider_wait(
            self.item, action='take_over', actor='x',
        )
        self.assertIsNone(marker2)
        self.assertFalse(cleared2)


class ProviderWaitEndpointTest(AuthenticatedAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = Project.objects.create(
            name='Provider wait api', key='PWA', org_id=1, access_level='open',
        )
        self.item = Item.objects.create(
            project=self.project, title='provider-wait api fixture', status='pending',
        )

    def test_non_provider_signature_400(self):
        resp = self.client.post(
            f'/api/items/{self.item.pk}/provider-wait/',
            {'signature_class': 'context_overflow'}, format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_set_auto_clear_and_refusal_roundtrip(self):
        resp = self.client.post(
            f'/api/items/{self.item.pk}/provider-wait/',
            {'signature_class': 'upstream_unavailable', 'stop_message_id': 'm1'},
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertTrue(resp.data['changed'])

        # successful-turn auto-clear clears
        resp = self.client.post(
            f'/api/items/{self.item.pk}/provider-wait/auto-clear/', {}, format='json',
        )
        self.assertTrue(resp.data['cleared'])

        # re-set, then a failed attempt (fresh ANDON) is a typed refusal
        self.client.post(
            f'/api/items/{self.item.pk}/provider-wait/',
            {'signature_class': 'quota_exhausted'}, format='json',
        )
        Comment.objects.create(
            item=self.item, author_id=0, author_email='runtime@shizuha.io',
            author_name='runtime', content='[stop_signature: quota_exhausted]',
        )
        resp = self.client.post(
            f'/api/items/{self.item.pk}/provider-wait/auto-clear/', {}, format='json',
        )
        self.assertFalse(resp.data['cleared'])
        self.assertEqual(resp.data['refused_reason'], 'fresh_terminal_andon')
        self.assertTrue(resp.data['live'])

        # manager-action clear with provenance
        resp = self.client.post(
            f'/api/items/{self.item.pk}/provider-wait/clear/',
            {'action': 'redispatch'}, format='json',
        )
        self.assertTrue(resp.data['cleared'])
        self.assertEqual(resp.data['manager_cleared_action'], 'redispatch')

    def test_state_endpoint_shape(self):
        resp = self.client.get(f'/api/items/{self.item.pk}/provider-wait/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertFalse(resp.data['live'])
        self.client.post(
            f'/api/items/{self.item.pk}/provider-wait/',
            {'signature_class': 'upstream_unavailable'}, format='json',
        )
        resp = self.client.get(f'/api/items/{self.item.pk}/provider-wait/')
        self.assertTrue(resp.data['live'])
        self.assertEqual(resp.data['signature_class'], 'upstream_unavailable')


class WriterWireContractIntegrationTests(AuthenticatedAPITestCase):
    """PLAT-10083 (rei review 910927 P1): the canonical surface must be
    wire-compatible with the DEPLOYED runtime writer — shizuha-beta
    ``src/gateway/provider-wait-writer.ts`` @ ``000cccca``.

    The writer has no item identity and no item-API auth: it reports terminal
    stops by seat identity against seat-scoped internal routes:

    - ``POST /api/internal/queue-lane/provider-wait/``  {email|username,
      signature, reason, set_by}
    - ``POST /api/internal/queue-lane/provider-wait:clear/``  (success-only)
    - ``GET  /api/internal/queue-lane/``  (manager enumeration)

    These tests invoke those EXACT URLs/payloads (no auth header — the writer
    supplies none; the internal trust model is network-secured per PLAT-6071)
    and walk the full lifecycle: set → manager-visible row → failed turn
    preserves → successful turn clears → replay benign.
    """

    SET_URL = '/api/internal/queue-lane/provider-wait/'
    CLEAR_URL = '/api/internal/queue-lane/provider-wait:clear/'
    LIST_URL = '/api/internal/queue-lane/'

    def _blocked_item_for(self, seat_email, title):
        from tasks.tests.base import create_blocked_item

        return create_blocked_item(
            title=title,
            mode='task',
            assignment_group='engineering',
            assignee_email=seat_email,
            created_by_email='zen@shizuha.com',
            created_by_id=0,
            workflow_name='autonomous-bug',
        )

    def _post_writer(self, url, payload):
        return self.client.post(
            url, payload, content_type='application/json',
            REMOTE_ADDR='10.42.1.20',
        )

    def test_full_writer_lifecycle_set_visible_preserve_clear_replay(self):
        from tasks.models import ProviderWaitMarker

        seat = 'writer-seat@agents.shizuha.io'
        item = self._blocked_item_for(seat, 'writer-lifecycle dependent')

        # 1. Writer reports a terminal stop (exact wire payload, no auth).
        response = self._post_writer(self.SET_URL, {
            'email': seat,
            'signature': 'upstream_unavailable',
            'reason': 'vLLM 503 stream at terminal stop',
            'set_by': 'provider-wait-writer',
        })
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body['seat'], seat)
        marked = [row for row in body['items'] if row['item_id'] == item.pk]
        self.assertEqual(len(marked), 1)
        self.assertTrue(marked[0]['changed'])
        self.assertEqual(marked[0]['signature_class'], 'upstream_unavailable')

        marker = ProviderWaitMarker.objects.get(item=item)
        self.assertEqual(marker.state, 'active')

        # 2. Manager enumeration shows the row, seat-addressed.
        listing = self.client.get(self.LIST_URL, REMOTE_ADDR='10.42.1.20')
        self.assertEqual(listing.status_code, 200)
        rows = [r for r in listing.json()['markers'] if r['item_id'] == item.pk]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['seat'], seat)
        self.assertEqual(rows[0]['signature_class'], 'upstream_unavailable')

        # 3. FAILED turn preserves: another terminal stop re-arms (new gen),
        #    and no clear call is made — the marker stays live.
        response = self._post_writer(self.SET_URL, {
            'email': seat,
            'signature': 'upstream_unavailable',
            'reason': 'second terminal stop',
            'set_by': 'provider-wait-writer',
        })
        self.assertEqual(response.status_code, 200)
        marker.refresh_from_db()
        self.assertEqual(marker.state, 'active')
        self.assertEqual(marker.generation, 2)
        self.assertEqual(marker.stop_message_id, 'second terminal stop')
        self.assertIsNone(marker.auto_cleared_at)

        # 4. SUCCESSFUL turn clears: the writer's success-only clear route.
        response = self._post_writer(self.CLEAR_URL, {'email': seat})
        self.assertEqual(response.status_code, 200, response.content)
        result = [r for r in response.json()['items']
                  if r['item_id'] == item.pk][0]
        self.assertTrue(result['cleared'])
        self.assertEqual(response.json()['active_remaining'], 0)
        marker.refresh_from_db()
        self.assertEqual(marker.state, 'inactive')
        self.assertIsNotNone(marker.auto_cleared_at)

        # 5. Replay is benign: clearing with no active marker is a typed
        #    no-op (200, no error), and re-setting re-arms a fresh generation.
        response = self._post_writer(self.CLEAR_URL, {'email': seat})
        self.assertEqual(response.status_code, 200)
        result = [r for r in response.json()['items']
                  if r['item_id'] == item.pk][0]
        self.assertFalse(result['cleared'])

        response = self._post_writer(self.SET_URL, {
            'email': seat,
            'signature': 'quota_exhausted',
            'reason': 'quota exhausted on retry',
            'set_by': 'provider-wait-writer',
        })
        self.assertEqual(response.status_code, 200)
        marker.refresh_from_db()
        self.assertEqual(marker.state, 'active')
        self.assertEqual(marker.signature_class, 'quota_exhausted')
        self.assertEqual(marker.generation, 3)

    def test_writer_username_resolution_and_non_provider_signature_rejected(self):
        from django.contrib.auth import get_user_model
        from tasks.models import ProviderWaitMarker

        get_user_model().objects.create_user(
            username='writer-seat2',
            email='writer-seat2@agents.shizuha.io',
            password='x',
        )
        item = self._blocked_item_for(
            'writer-seat2@agents.shizuha.io', 'username-resolution dependent')

        # Username form resolves to the seat's email and marks its items.
        response = self._post_writer(self.SET_URL, {
            'username': 'writer-seat2',
            'signature': 'quota_exhausted',
            'reason': 'quota',
            'set_by': 'provider-wait-writer',
        })
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['seat'],
                         'writer-seat2@agents.shizuha.io')
        self.assertTrue(ProviderWaitMarker.objects.filter(
            item=item, state='active').exists())

        # Non-provider signature is typed-rejected (never gains visibility).
        response = self._post_writer(self.SET_URL, {
            'email': 'writer-seat2@agents.shizuha.io',
            'signature': 'some_other_stop',
            'reason': 'not a provider class',
            'set_by': 'provider-wait-writer',
        })
        self.assertEqual(response.status_code, 400)
        marker = ProviderWaitMarker.objects.get(item=item)
        self.assertEqual(marker.signature_class, 'quota_exhausted')
        self.assertEqual(marker.state, 'active')

    def test_ingress_proxied_writer_request_is_404_masked(self):
        """The internal surface keeps the PLAT-6071 ingress guard: proxied
        traffic is 404-masked; only cluster-internal callers reach it."""
        seat = 'ingress-probe@agents.shizuha.io'
        self._blocked_item_for(seat, 'ingress guard dependent')
        response = self.client.post(
            self.SET_URL,
            {'email': seat, 'signature': 'upstream_unavailable',
             'reason': 'x', 'set_by': 'provider-wait-writer'},
            content_type='application/json',
            HTTP_X_FORWARDED_FOR='203.0.113.9',
        )
        self.assertEqual(response.status_code, 404)
