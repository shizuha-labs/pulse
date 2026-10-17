import logging
import sys
import unittest

from tasks.log_redaction import (
    WebhookSecretRedactingFormatter,
    WebhookSecretRedactionFilter,
    redact_webhook_secrets,
)
from tasks.logging_handlers import PulseAlertHandler


class WebhookLogRedactionTests(unittest.TestCase):
    def test_redacts_alertmanager_key_in_strings_and_logging_args(self):
        secret = "test-alertmanager-key-should-not-log"
        request_line = f"POST /pulse/api/webhooks/alertmanager/{secret}/ HTTP/1.1"

        self.assertEqual(
            redact_webhook_secrets(request_line),
            "POST /pulse/api/webhooks/alertmanager/<redacted>/ HTTP/1.1",
        )

        record = logging.LogRecord(
            name="django.request",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="Internal Server Error: %s",
            args=(f"/api/webhooks/alertmanager/{secret}/",),
            exc_info=None,
        )

        WebhookSecretRedactionFilter().filter(record)

        message = record.getMessage()
        self.assertNotIn(secret, message)
        self.assertIn("/api/webhooks/alertmanager/<redacted>/", message)


    def test_console_formatter_redacts_exception_tracebacks(self):
        secret = "formatter-secret-should-not-log"
        try:
            raise RuntimeError(f"boom /api/webhooks/alertmanager/{secret}/")
        except RuntimeError:
            record = logging.getLogger("django.request").makeRecord(
                "django.request",
                logging.ERROR,
                __file__,
                1,
                "Internal Server Error: %s",
                (f"/api/webhooks/alertmanager/{secret}/",),
                exc_info=sys.exc_info(),
            )

        WebhookSecretRedactionFilter().filter(record)
        formatter = WebhookSecretRedactingFormatter(
            format="{levelname} {asctime} {module} {message}",
            style="{",
        )
        formatted = formatter.format(record)

        self.assertNotIn(secret, formatted)
        self.assertIn("/api/webhooks/alertmanager/<redacted>/", formatted)

    def test_pulse_alert_handler_redacts_exception_descriptions(self):
        secret = "exception-secret-should-not-log"
        try:
            raise RuntimeError(f"boom /api/webhooks/alertmanager/{secret}/")
        except RuntimeError:
            record = logging.getLogger("django.request").makeRecord(
                "django.request",
                logging.ERROR,
                __file__,
                1,
                "Internal Server Error: %s",
                (f"/api/webhooks/alertmanager/{secret}/",),
                exc_info=sys.exc_info(),
            )

        WebhookSecretRedactionFilter().filter(record)
        handler = PulseAlertHandler()
        description = handler._format_description(record)

        self.assertNotIn(secret, record.getMessage())
        self.assertNotIn(secret, description)
        self.assertIn("/api/webhooks/alertmanager/<redacted>/", description)


if __name__ == "__main__":
    unittest.main()
