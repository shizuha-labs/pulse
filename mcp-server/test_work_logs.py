"""
Regression tests for AT-84 MCP work-log payload mapping.

The MCP tool keeps a stable agent-facing signature (`time_spent_minutes`,
`work_date`), but the WorkLog API serializer accepts `duration_minutes` and
`worked_date`. Pin the outgoing POST payload and list formatting to the backend
contract so item-key work-log support actually works end-to-end.
"""
import os
import unittest
from unittest import mock

os.environ.setdefault("SHIZUHA_PULSE_URL", "http://localhost:8002/pulse/api")

import pulse_server  # noqa: E402


class WorkLogPayloadTests(unittest.TestCase):
    def test_log_work_maps_mcp_args_to_serializer_fields(self):
        with mock.patch.object(pulse_server, "_resolve_item_pk", return_value=84) as resolve, \
             mock.patch.object(pulse_server, "api_request", return_value={"id": 123}) as api:
            result = pulse_server.log_work(
                "AT-84",
                45,
                description="fix item-key work logs",
                work_date="2026-06-04",
            )

        self.assertIn("Logged 45 minutes", result)
        resolve.assert_called_once_with("AT-84")
        api.assert_called_once_with(
            "POST",
            "/work-logs/",
            data={
                "item": 84,
                "duration_minutes": 45,
                "description": "fix item-key work logs",
                "worked_date": "2026-06-04",
            },
        )


    def test_log_work_defaults_worked_date_to_today(self):
        with mock.patch.object(pulse_server, "_resolve_item_pk", return_value=84), \
             mock.patch.object(pulse_server, "api_request", return_value={"id": 124}) as api:
            pulse_server.log_work("AT-84", 10)

        _, _, kwargs = api.mock_calls[0]
        self.assertEqual(kwargs["data"]["worked_date"], pulse_server._dt.date.today().isoformat())
        self.assertEqual(kwargs["data"]["duration_minutes"], 10)
        self.assertNotIn("work_date", kwargs["data"])
        self.assertNotIn("time_spent_minutes", kwargs["data"])


    def test_log_work_trims_iso_datetime_to_date(self):
        with mock.patch.object(pulse_server, "_resolve_item_pk", return_value=84), \
             mock.patch.object(pulse_server, "api_request", return_value={"id": 125}) as api:
            pulse_server.log_work("AT-84", 15, work_date="2026-06-04T12:34:56+00:00")

        _, _, kwargs = api.mock_calls[0]
        self.assertEqual(kwargs["data"]["worked_date"], "2026-06-04")

    def test_list_work_logs_reads_serializer_field_names(self):
        with mock.patch.object(pulse_server, "_resolve_item_pk", return_value=84) as resolve, \
             mock.patch.object(
                 pulse_server,
                 "api_request",
                 return_value={
                     "results": [
                         {
                             "duration_minutes": 30,
                             "worked_date": "2026-06-04",
                             "user_email": "ryo@shizuha.com",
                             "description": "mapped fields",
                         }
                     ]
                 },
             ) as api:
            result = pulse_server.list_work_logs("AT-84", limit=5)

        resolve.assert_called_once_with("AT-84")
        api.assert_called_once_with("GET", "/work-logs/", params={"page_size": 5, "item": 84})
        self.assertIn("30min by ryo@shizuha.com on 2026-06-04", result)
        self.assertNotIn("- 0min", result)
        self.assertNotIn("N/A", result)


if __name__ == "__main__":
    unittest.main()
