"""HIVE-1153 (independent Security review P1 #2): every owner-writing
authorization site must pass the `item` so runtime-substrate capabilities are
decided by verified signed Hive V2 evidence under `enforce` — never the legacy
team/plain-JSON predicate that `_seat_satisfies_required_capability` returns when
`item is None`. Without the item, the authoritative `Item.save()` commit fence
could authorize a privileged assignment with no verified evidence.

Two guards:
1. A source-scan proving NO `_seat_satisfies_required_capability` call site omits
   `item=` (prevents the bypass from reappearing on any owner writer).
2. A helper-boundary regression proving that under `enforce` the signed path is
   consulted only when an item is present, and fails closed on a denying verdict.
"""
import ast
import unittest
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

from tasks.services import capability_evidence as ce
from tasks.services.workflow_service import WorkflowService

ROOT = Path(__file__).resolve().parents[2]

# Every module that authorizes an owner write via the seat-capability helper.
OWNER_WRITER_SOURCES = [
    'tasks/services/routing_authority.py',
    'tasks/tasks.py',
    'tasks/services/workflow_service.py',
]


def _capability_call_sites(path):
    tree = ast.parse(path.read_text())
    sites = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == '_seat_satisfies_required_capability'
        ):
            sites.append(node)
    return sites


class OwnerWriterPassesItemSourceScanTests(SimpleTestCase):
    def test_no_owner_writer_omits_item(self):
        offenders = []
        for rel in OWNER_WRITER_SOURCES:
            for call in _capability_call_sites(ROOT / rel):
                if 'item' not in {kw.arg for kw in call.keywords}:
                    offenders.append(f'{rel}:{call.lineno}')
        self.assertEqual(
            offenders,
            [],
            'these _seat_satisfies_required_capability call sites omit item= and '
            'would fall back to the legacy predicate under enforce (HIVE-1153 P1 #2): '
            + ', '.join(offenders),
        )


class SeatCapabilityEnforceFenceTests(SimpleTestCase):
    CAP = 'kubeconfig'  # a runtime-substrate capability

    def _run(self, item):
        deny = mock.Mock(authorizes=False)
        with mock.patch.object(ce, 'get_substrate_cap_mode', return_value=ce.MODE_ENFORCE), \
             mock.patch.object(WorkflowService, '_agent_effective_capabilities', return_value={self.CAP}), \
             mock.patch.object(WorkflowService, '_email_conferred_capabilities', return_value=set()), \
             mock.patch.object(WorkflowService, '_substrate_evidence_verdict', return_value=deny) as verdict:
            result = WorkflowService._seat_satisfies_required_capability(
                'mio@shizuha.com', {self.CAP}, item=item)
        return result, verdict

    def test_item_none_bypasses_signed_evidence(self):
        # The bug: with no item, enforce short-circuits to the legacy predicate,
        # so a seat with legacy caps is authorized even though signed evidence
        # denies — and the signed verdict is never consulted.
        result, verdict = self._run(item=None)
        self.assertTrue(result)
        verdict.assert_not_called()

    def test_item_present_consults_signed_evidence_and_fails_closed(self):
        # The fix: with the item, enforce consults signed evidence and fails
        # closed on a denying verdict even though the legacy predicate would pass.
        result, verdict = self._run(item=mock.Mock())
        self.assertFalse(result)
        verdict.assert_called_once()


if __name__ == '__main__':
    unittest.main()
