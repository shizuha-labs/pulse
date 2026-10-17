"""Logging helpers for redacting secret-bearing webhook URL paths."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from typing import Any

# Alertmanager webhook keys are historically embedded in the request path:
#   /api/webhooks/alertmanager/<key>/
#   /pulse/api/webhooks/alertmanager/<key>/
# Access/error loggers include the path in clear text, so redact only the
# secret path segment while preserving enough route shape for debugging.
_ALERTMANAGER_WEBHOOK_KEY_RE = re.compile(
    r'(?P<prefix>/(?:[A-Za-z0-9_.-]+/)*api/webhooks/alertmanager/)[^/\s"\'?#]+'
)

REDACTED_WEBHOOK_KEY = '<redacted>'


def redact_webhook_secrets(value: Any) -> Any:
    """Return *value* with secret Alertmanager webhook path segments redacted.

    The function handles strings plus the tuple/list/dict containers commonly
    stored in ``logging.LogRecord.args``. Other objects are returned unchanged so
    the filter does not alter logging semantics.
    """
    if isinstance(value, str):
        return _ALERTMANAGER_WEBHOOK_KEY_RE.sub(
            lambda match: f"{match.group('prefix')}{REDACTED_WEBHOOK_KEY}",
            value,
        )
    if isinstance(value, tuple):
        return tuple(redact_webhook_secrets(item) for item in value)
    if isinstance(value, list):
        return [redact_webhook_secrets(item) for item in value]
    if isinstance(value, Mapping):
        return {key: redact_webhook_secrets(item) for key, item in value.items()}
    # Avoid treating plain strings as sequences; all other sequences are rare
    # for logging args, but redacting them keeps the helper defensive.
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return type(value)(redact_webhook_secrets(item) for item in value)
    return value


class WebhookSecretRedactionFilter(logging.Filter):
    """Sanitize secret-bearing webhook paths before handlers format records."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_webhook_secrets(record.msg)
        if record.args:
            record.args = redact_webhook_secrets(record.args)
        # Some Django log records carry a request object and/or explicit path
        # attributes. Redact the attributes that handlers may render, without
        # mutating the request object itself.
        for attr in ('path', 'path_info', 'request_path'):
            if hasattr(record, attr):
                setattr(record, attr, redact_webhook_secrets(getattr(record, attr)))
        return True


class WebhookSecretRedactingFormatter(logging.Formatter):
    """Formatter that redacts webhook path secrets, including traceback text."""

    def __init__(
        self,
        fmt: str | None = None,
        datefmt: str | None = None,
        style: str = '%',
        validate: bool = True,
        *,
        defaults: Mapping[str, Any] | None = None,
        format: str | None = None,
    ) -> None:
        # logging.config may pass either the normal Formatter `fmt` positional
        # or the dictConfig `format` key when this class is used as a custom
        # formatter. Support both.
        super().__init__(fmt=format if fmt is None else fmt, datefmt=datefmt, style=style, validate=validate, defaults=defaults)

    def format(self, record: logging.LogRecord) -> str:
        return redact_webhook_secrets(super().format(record))

    def formatException(self, ei: object) -> str:  # noqa: N802 - logging API name
        return redact_webhook_secrets(super().formatException(ei))
