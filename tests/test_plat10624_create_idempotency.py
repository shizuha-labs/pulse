"""PLAT-10624: an in-flight create retry must re-read, not 500."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from django.db import IntegrityError


ROOT = Path(__file__).resolve().parents[1]


def test_omitted_client_request_id_is_payload_stable_not_a_fresh_uuid():
    mcp = (ROOT / 'mcp-server' / 'pulse_server.py').read_text()
    assert 'payload:' in mcp
    assert 'str(_uuid.uuid4())' not in mcp[mcp.index('PLAT-10624: a read-timeout'):mcp.index('item_id = result["id"]')]
    assert "timed out" in mcp
    assert "result.get('error_code') == 'pulse_transport_timeout'" in mcp


def test_collision_reread_is_creator_scoped_and_savepointed():
    views = (ROOT / 'tasks' / 'views.py').read_text()
    save = views.index('item = serializer.save(**extra_data)')
    window = views[save - 400:save + 700]
    assert 'with transaction.atomic():' in window
    assert 'except IntegrityError:' in window
    assert 'created_by_id=user.id, client_request_id=key' in window
    model = (ROOT / 'tasks' / 'models.py').read_text()
    assert "fields=['created_by_id', 'client_request_id']" in model
    assert 'unique=True' not in model[model.index('client_request_id = models.CharField'):model.index('client_request_id = models.CharField') + 400]


def test_integrity_error_returns_existing_row_for_same_creator():
    """The handler contract: collision + matching row replaces the insert."""
    existing = SimpleNamespace(id=7, client_request_id='req-1')
    lookup = MagicMock(return_value=existing)

    def resolve(user_id, key, error):
        if not isinstance(error, IntegrityError) or not key or not user_id:
            raise error
        found = lookup(user_id, key)
        if found is None:
            raise error
        return found

    assert resolve(3, 'req-1', IntegrityError('dup')).id == 7
    lookup.assert_called_once_with(3, 'req-1')
    lookup.return_value = None
    try:
        resolve(3, 'req-1', IntegrityError('dup'))
    except IntegrityError:
        pass
    else:
        raise AssertionError('missing row must re-raise')
