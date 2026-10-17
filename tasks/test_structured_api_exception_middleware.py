from django.test import RequestFactory, SimpleTestCase
from django.db import OperationalError

from tasks.middleware import StructuredApiExceptionMiddleware


class StructuredApiExceptionMiddlewareTests(SimpleTestCase):
    @staticmethod
    def _statement_timeout():
        class QueryCanceled(Exception):
            pass

        exception = OperationalError(
            'canceling statement due to statement timeout')
        exception.__cause__ = QueryCanceled(
            'canceling statement due to statement timeout')
        return exception

    def test_search_statement_timeout_is_non_retryable_query_cost_rejection(self):
        middleware = StructuredApiExceptionMiddleware(lambda req: None)
        for path in ('/api/items/', '/api/tasks/'):
            with self.subTest(path=path):
                request = RequestFactory().get(path, {'search': 'longhorn'})
                response = middleware.process_exception(
                    request, self._statement_timeout())

                self.assertEqual(response.status_code, 422)
                self.assertNotIn('Retry-After', response)
                self.assertJSONEqual(
                    response.content,
                    {
                        'error': 'query_cost_exceeded',
                        'code': 'query_cost_exceeded',
                        'detail': (
                            'Pulse task search exceeded its bounded database execution '
                            'budget. Narrow the query or use a keyed task read; retrying '
                            'the identical search will not succeed.'
                        ),
                        'retryable': False,
                    },
                )

    def test_other_operational_error_keeps_transient_server_contract(self):
        request = RequestFactory().get('/api/items/', {'search': 'longhorn'})
        middleware = StructuredApiExceptionMiddleware(lambda req: None)

        response = middleware.process_exception(
            request, OperationalError('connection reset'))

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response['Retry-After'], '30')

    def test_statement_timeout_outside_task_search_keeps_server_contract(self):
        request = RequestFactory().get('/api/projects/')
        middleware = StructuredApiExceptionMiddleware(lambda req: None)

        response = middleware.process_exception(
            request, self._statement_timeout())

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response['Retry-After'], '30')

    def test_api_exception_returns_structured_json_with_retry_after(self):
        request = RequestFactory().get('/api/items/')
        middleware = StructuredApiExceptionMiddleware(lambda req: None)

        response = middleware.process_exception(request, RuntimeError('boom'))

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response['Retry-After'], '30')
        self.assertJSONEqual(
            response.content,
            {
                'error': 'server_error',
                'detail': 'Pulse API encountered an internal error. Retry after the suggested backoff; if it persists, check /api/health/ and Pulse server logs.',
                'retry_after': 30,
            },
        )

    def test_non_api_exception_uses_default_django_handling(self):
        request = RequestFactory().get('/admin/')
        middleware = StructuredApiExceptionMiddleware(lambda req: None)

        self.assertIsNone(middleware.process_exception(request, RuntimeError('boom')))

    def test_api_exception_is_logged_with_stacktrace(self):
        # PLS-375: returning a response from process_exception suppresses Django's
        # default uncaught-exception logging, so the middleware MUST log the
        # swallowed exception itself (with traceback) or every API 500 is
        # un-diagnosable. Assert method + path + the exception are captured.
        request = RequestFactory().post('/pulse/api/items/AT-1/execute-transition/')
        middleware = StructuredApiExceptionMiddleware(lambda req: None)

        with self.assertLogs('tasks.middleware', level='ERROR') as cm:
            response = middleware.process_exception(request, RuntimeError('kaboom'))

        self.assertEqual(response.status_code, 500)
        joined = '\n'.join(cm.output)
        self.assertIn('server_error', joined)
        self.assertIn('POST', joined)
        self.assertIn('/pulse/api/items/AT-1/execute-transition/', joined)
        self.assertIn('kaboom', joined)
        # exc_info=exception => the traceback header is emitted in the record
        self.assertIn('RuntimeError', joined)
