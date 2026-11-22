"""PLAT-10624: a unique-race create must return the winner, not None."""
from types import SimpleNamespace
from unittest.mock import MagicMock

from django.test import SimpleTestCase

from tasks.views import ItemViewSet


class CollisionReplayResponseTests(SimpleTestCase):
    def test_integrity_collision_returns_200_replay_body(self):
        view = ItemViewSet()
        existing = SimpleNamespace(id=7, client_request_id='req-1')
        view.get_serializer = MagicMock(return_value=SimpleNamespace(
            data={'id': 7, 'client_request_id': 'req-1'},
        ))
        response = view._replay_existing(existing)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['id'], 7)
        view.get_serializer.assert_called_once_with(existing)
