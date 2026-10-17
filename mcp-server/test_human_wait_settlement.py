"""Fleet callers can settle evidenced fulfillment without invoking resume."""
import json
import hashlib
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("SHIZUHA_PULSE_URL", "http://localhost:8002/pulse/api")
os.environ.setdefault("PULSE_SUBSTRATE_CAP_MODE", "off")

import pulse_server


class HumanWaitSettlementToolTests(unittest.TestCase):
    def setUp(self):
        self.arguments = {
            "item_id": "PLAT-8852",
            "receipt": "960c5cd9-b2db-49b7-8c39-3813ed336a54",
            "wait_generation": 1,
            "transition_id": "51657d6c-483f-40ea-a5e8-6228aaba7d72",
            "summary": "Existing prerequisite verified; parent answer recorded.",
            "resolutions": [{"parent_id": 25141, "comment_id": 842865, "sha256": "b" * 64}],
        }

    def test_transports_receipt_and_evidence_to_settlement_without_resume(self):
        receipt = {"released": True, "status": "resolved_elsewhere", "wait_generation": 1}
        with patch.object(pulse_server, "api_request", return_value=receipt) as request:
            result = pulse_server.settle_human_wait(**self.arguments)
        self.assertEqual(json.loads(result), receipt)
        expected = {k: v for k, v in self.arguments.items() if k != "item_id"}
        expected["screen_data"] = {}
        request.assert_called_once_with("POST", "/items/PLAT-8852/settle-human-wait/", data=expected)

    def test_preserves_configured_workflow_screen_data(self):
        screen_data = {"acceptance_reference": "original-approved-receipt"}
        with patch.object(pulse_server, "api_request", return_value={"released": True}) as request:
            pulse_server.settle_human_wait(**self.arguments, screen_data=screen_data)
        self.assertEqual(request.call_args.kwargs["data"]["screen_data"], screen_data)

    def test_typed_refusal_is_not_reported_as_fulfilled(self):
        for code in ("human_wait_generation_stale", "human_wait_settlement_forbidden", "resolution_evidence_changed_or_missing"):
            with self.subTest(code=code), patch.object(
                pulse_server, "api_request", return_value={"error": code, "code": code},
            ) as request:
                result = pulse_server.settle_human_wait(**self.arguments)
            self.assertIn(code, result)
            self.assertIn("Error", result)
            self.assertNotIn('"released": true', result)
            self.assertEqual(request.call_count, 1)

    def test_task_read_exposes_the_human_receipt_and_original_state(self):
        current = {
            'branch': 'human_escalation_v1', 'receipt': self.arguments['receipt'],
            'wait_generation': 1, 'resume_status': 'todo', 'resume_workflow_status': '',
        }
        item = {'id': 35525, 'item_key': 'PLAT-8852', 'status': 'waiting_external',
                'external_wait': {'current': current}}
        with patch.object(pulse_server, 'api_request', return_value=item):
            result = pulse_server.get_task('PLAT-8852')
        self.assertIn(json.dumps(current, sort_keys=True), result)
        self.assertIn('pulse_settle_human_wait', result)
        self.assertIn('include_hashes=True', result)

    def test_parent_reference_hashes_exact_stored_content_only_when_requested(self):
        content = 'Accepted receipt with Unicode: ✓\n\n'
        comment = {'id': 842865, 'item': 25141, 'content': content,
                   'author_email': 'owner@example.com', 'created_at': '2026-09-22T06:00:00Z'}
        response = {'count': 1, 'results': [comment]}
        with patch.object(pulse_server, '_resolve_item_pk', return_value=25141), patch.object(
            pulse_server, 'api_request', return_value=response,
        ):
            ordinary = pulse_server.list_comments('PLAT-5798')
            evidenced = pulse_server.list_comments('PLAT-5798', include_hashes=True)
        self.assertNotIn('Settlement reference:', ordinary)
        ref = {'parent_id': 25141, 'comment_id': 842865,
               'sha256': hashlib.sha256(content.encode('utf-8')).hexdigest()}
        self.assertIn(json.dumps(ref, sort_keys=True), evidenced)
        self.assertNotEqual(ref['sha256'], hashlib.sha256(content.strip().encode()).hexdigest())


if __name__ == "__main__":
    unittest.main()
