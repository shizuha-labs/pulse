"""The real fleet tool must not narrate HTTP202 as a completed transition."""
from unittest import TestCase, mock
import pulse_server as module


class DeferredTransitionFormattingTests(TestCase):
    def test_pending_operation_is_not_completion(self):
        result = {'pending': True, 'committed': False, 'operation_id': 'op-123',
                  'title': 'Gate repair', 'status': 'in_progress'}
        with mock.patch.object(module, 'api_request', return_value=result):
            text = module.execute_transition('7421', 'done')
        self.assertIn('not completed', text)
        self.assertIn("remains 'in_progress'", text)
        self.assertIn('op-123', text)
        self.assertNotIn('✓ Transitioned', text)

    def test_committed_small_transition_keeps_success(self):
        with mock.patch.object(module, 'api_request', return_value={'title': 'Small', 'status': 'done'}):
            text = module.execute_transition('42', 'done')
        self.assertIn("✓ Transitioned 'Small' to status 'done'", text)

    def test_complete_pending_preserves_pending_contract_and_exposes_read_tool(self):
        result = {'pending': True, 'committed': False, 'operation_id': 'op-123',
                  'item_key': 'PLAT-7421', 'title': 'Gate repair', 'status': 'in_progress'}
        with mock.patch.object(module, 'api_request', return_value=result) as api:
            text = module.complete_task('7421')
        api.assert_called_once_with('POST', '/items/7421/complete/')
        self.assertIn('not completed', text)
        self.assertNotIn('Completed:', text)
        self.assertIn("pulse_get_workflow_operation(item_id='PLAT-7421', operation_id='op-123')", text)

    def test_complete_small_committed_response_is_unchanged(self):
        with mock.patch.object(module, 'api_request', return_value={'item_key': 'PLAT-42', 'title': 'Small', 'status': 'done'}):
            self.assertEqual(module.complete_task('42'), 'Completed: [PLAT-42] Small')

    def _operation(self, state, **extra):
        return {'operation_id': 'op-123', 'item_key': 'PLAT-7421', 'status': 'in_progress',
                'operation_state': state, 'pending': state == 'pending',
                'committed': state == 'succeeded', **extra}

    def test_operation_pending_reads_only_the_authorized_endpoint(self):
        with mock.patch.object(module, 'api_request', return_value=[self._operation('pending')]) as api:
            text = module.get_workflow_operation('#7421', 'op-123')
        api.assert_called_once_with('GET', '/items/7421/workflow-operations/', params={'operation_id': 'op-123'})
        self.assertIn('pending; not completed', text)

    def test_operation_committed_receipt_does_not_override_newer_task_status(self):
        row = self._operation('succeeded', status='todo', result={'status': 'done', 'detail': 'Graph committed'})
        with mock.patch.object(module, 'api_request', return_value=[row]):
            text = module.get_workflow_operation('PLAT-7421', 'op-123')
        self.assertIn('succeeded; graph transaction committed', text)
        self.assertIn('Current task [PLAT-7421] status: todo', text)
        self.assertIn('"status": "done"', text)

    def test_operation_failure_and_stale_are_not_completion(self):
        for state in ['failed', 'stale']:
            with self.subTest(state=state), mock.patch.object(module, 'api_request', return_value=[
                    self._operation(state, result={'code': 'graph_changed', 'detail': 'Refused'})]):
                text = module.get_workflow_operation('PLAT-7421', 'op-123')
            self.assertIn(f'{state}; graph transaction did not commit', text)
            self.assertIn('graph_changed', text)

    def test_not_visible_empty_and_mismatched_receipts_fail_loudly(self):
        for response in [{'error': 'Not found', 'status_code': 404}, [],
                         [self._operation('pending', operation_id='someone-else')],
                         [self._operation('pending', committed=True)]]:
            with self.subTest(response=response), mock.patch.object(module, 'api_request', return_value=response):
                text = module.get_workflow_operation('PLAT-7421', 'op-123')
            self.assertIn('Error', text)
            self.assertNotIn('succeeded;', text)

    def test_operation_reader_is_discoverable_with_exact_ids(self):
        import asyncio
        tools = asyncio.run(module.mcp.list_tools())
        tool = next(tool for tool in tools if tool.name == 'pulse_get_workflow_operation')
        self.assertEqual(set(tool.inputSchema['required']), {'item_id', 'operation_id'})
