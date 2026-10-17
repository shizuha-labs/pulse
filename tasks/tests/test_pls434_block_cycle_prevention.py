"""
PLS-434 (operator 2026-07-11) — block-dependency rings are permanent deadlocks;
reject any block link that would close a cycle, at link-creation time.
"""
from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase, APIClient

from tasks.models import Item, ItemLink
from tasks.services.workflow_service import WorkflowService

User = get_user_model()


def _item(key):
    return Item.objects.create(mode='task', title=key, item_key=key)


class PLS434BlockCycleTests(APITestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username='u', email='u@sec.test', password='x', is_staff=True)
        self.a = _item('CYC-A')
        self.b = _item('CYC-B')
        self.c = _item('CYC-C')

    # ---- helper ----
    def test_direct_two_node_cycle_detected(self):
        # A blocks B ; adding B blocks A closes a 2-ring
        ItemLink.objects.create(source_item=self.a, target_item=self.b, link_type='blocks', created_by_id=1, created_by_email='u@sec.test')
        self.assertTrue(WorkflowService.would_create_block_cycle(self.b.id, self.a.id))

    def test_transitive_three_node_cycle_detected(self):
        # A→B→C ; adding C blocks A closes a 3-ring
        ItemLink.objects.create(source_item=self.a, target_item=self.b, link_type='blocks', created_by_id=1, created_by_email='u@sec.test')
        ItemLink.objects.create(source_item=self.b, target_item=self.c, link_type='blocks', created_by_id=1, created_by_email='u@sec.test')
        self.assertTrue(WorkflowService.would_create_block_cycle(self.c.id, self.a.id))

    def test_is_blocked_by_direction_cycle_detected(self):
        # A is_blocked_by B  ==  B blocks A. Adding A blocks B closes the ring.
        ItemLink.objects.create(source_item=self.a, target_item=self.b, link_type='is_blocked_by', created_by_id=1, created_by_email='u@sec.test')
        self.assertTrue(WorkflowService.would_create_block_cycle(self.a.id, self.b.id))

    def test_self_block_is_cycle(self):
        self.assertTrue(WorkflowService.would_create_block_cycle(self.a.id, self.a.id))

    def test_non_cycle_allowed(self):
        # A blocks B ; A blocks C is fine (no path C→A)
        ItemLink.objects.create(source_item=self.a, target_item=self.b, link_type='blocks', created_by_id=1, created_by_email='u@sec.test')
        self.assertFalse(WorkflowService.would_create_block_cycle(self.a.id, self.c.id))

    # ---- endpoint ----
    def test_endpoint_rejects_ring_forming_link(self):
        self.client.force_authenticate(self.user)
        ItemLink.objects.create(source_item=self.a, target_item=self.b, link_type='blocks', created_by_id=1, created_by_email='u@sec.test')
        # Now try: B blocks A  (POST on B, link_type=blocks, target=A) → would ring
        resp = self.client.post(
            f'/api/items/{self.b.id}/links/',
            {'link_type': 'blocks', 'target_item_key': 'CYC-A'}, format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('circular block ring', str(resp.data).lower())
        self.assertFalse(
            ItemLink.objects.filter(source_item=self.b, target_item=self.a, link_type='blocks', created_by_id=1, created_by_email='u@sec.test').exists())

    def test_endpoint_allows_valid_block_link(self):
        self.client.force_authenticate(self.user)
        resp = self.client.post(
            f'/api/items/{self.a.id}/links/',
            {'link_type': 'blocks', 'target_item_key': 'CYC-B'}, format='json')
        self.assertEqual(resp.status_code, 201)


class PLS582ReleasedEdgeTests(APITestCase):
    """PLS-582 — terminal historical blocker edges must not create false rings.

    Live shape (revi's HIVE-1153 ANDON): the only apparent cycle traversed a
    RELEASED historical dependency (terminal PLS-498 no longer actively blocked
    HIVE-1157, but its retained provenance edge was treated as reachability).
    Ring detection must use the same canonical release semantics as
    `_active_blockers_for` (`_status_releases_blocks`: terminal statuses +
    BLOCK_RELEASING_NONTERMINAL_SLUGS) — with NO second status list."""

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username='u582', email='u582@sec.test', password='x', is_staff=True)
        self.a = _item('REL-A')
        self.b = _item('REL-B')
        self.c = _item('REL-C')

    def _link(self, src, dst, link_type='blocks'):
        ItemLink.objects.create(
            source_item=src, target_item=dst, link_type=link_type,
            created_by_id=1, created_by_email='u582@sec.test')

    # ---- helper ----
    def test_released_mid_path_blocker_does_not_ring(self):
        # A→B(done)→C ; adding C blocks A is VALID: B's outgoing edge is
        # retained provenance, not reachability, once B released its blocks.
        self._link(self.a, self.b)
        self._link(self.b, self.c)
        self.b.status = 'done'
        self.b.save(update_fields=['status'])
        self.assertFalse(WorkflowService.would_create_block_cycle(self.c.id, self.a.id))

    def test_same_path_all_active_still_rings(self):
        # Identical topology with every blocker active must still reject.
        self._link(self.a, self.b)
        self._link(self.b, self.c)
        self.assertTrue(WorkflowService.would_create_block_cycle(self.c.id, self.a.id))

    def test_released_direct_edge_does_not_close_two_ring(self):
        # A(done) blocks B ; adding B blocks A is valid — the A→B edge is inert.
        self._link(self.a, self.b)
        self.a.status = 'done'
        self.a.save(update_fields=['status'])
        self.assertFalse(WorkflowService.would_create_block_cycle(self.b.id, self.a.id))

    def test_rfc_approved_nonterminal_slug_releases_edge(self):
        # BLOCK_RELEASING_NONTERMINAL_SLUGS participate via the SAME canonical
        # helper — an approved RFC's historical edge is inert too.
        self._link(self.a, self.b)
        self._link(self.b, self.c)
        Item.objects.filter(pk=self.b.pk).update(status='rfc_approved')
        self.assertFalse(WorkflowService.would_create_block_cycle(self.c.id, self.a.id))

    # ---- endpoint ----
    def test_endpoint_accepts_link_across_released_history(self):
        self.client.force_authenticate(self.user)
        self._link(self.a, self.b)
        self._link(self.b, self.c)
        self.b.status = 'done'
        self.b.save(update_fields=['status'])
        resp = self.client.post(
            f'/api/items/{self.c.id}/links/',
            {'link_type': 'blocks', 'target_item_key': 'REL-A'}, format='json')
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertTrue(ItemLink.objects.filter(
            source_item=self.c, target_item=self.a, link_type='blocks').exists())

    def test_endpoint_still_rejects_all_active_ring(self):
        self.client.force_authenticate(self.user)
        self._link(self.a, self.b)
        self._link(self.b, self.c)
        resp = self.client.post(
            f'/api/items/{self.c.id}/links/',
            {'link_type': 'blocks', 'target_item_key': 'REL-A'}, format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('circular block ring', str(resp.data).lower())
