"""
PulseAlertHandler — Python logging handler that sends ERROR/CRITICAL
log records to the Shizuha Pulse generic webhook as alerts.

Designed to be copy-pasted to any Django service. Only depends on stdlib
`logging`, `threading`, `hashlib`, `traceback`, and `urllib`.

Configuration via environment variables:
  PULSE_WEBHOOK_URL  — e.g. http://shizuha-pulse:8002/api/webhooks/generic/
  PULSE_WEBHOOK_KEY  — the WebhookKey token created in Pulse admin
"""

import hashlib
import json
import logging
import os
import threading
import time
import traceback
import urllib.request
import urllib.error

from .log_redaction import redact_webhook_secrets
from .net_utils import safe_urlopen  # PLAT-1568: scheme-guarded urlopen (http/https only)


class PulseAlertHandler(logging.Handler):
    """Logging handler that posts ERROR+ records to Pulse as alerts."""

    # Rate-limit: max errors per fingerprint within the window (seconds)
    RATE_LIMIT_WINDOW = 60
    RATE_LIMIT_MAX = 10

    # Loggers to skip entirely — these produce noise, not actionable alerts.
    # Django's HTTP access logger (`django.server`) emits ERROR for any 5xx
    # response, which duplicates the actual exception already captured by
    # `django.request`. Both would get reported otherwise.
    SKIP_LOGGERS = {
        'django.server',   # HTTP access log — noise (duplicates django.request)
    }

    # Message substrings to ignore — for known expected errors that shouldn't alert.
    SKIP_MESSAGE_SUBSTRINGS = (
        'User matching query does not exist',      # simplejwt refresh with stale user
        'Invalid or inactive webhook key',          # expected for bad webhook keys
        'Cannot resolve keyword',                   # Django FieldError from user-JQL
    )

    def __init__(self, level=logging.ERROR):
        super().__init__(level)
        self.webhook_url = os.environ.get('PULSE_WEBHOOK_URL', '')
        self.webhook_key = os.environ.get('PULSE_WEBHOOK_KEY', '')
        self._rate_counts = {}  # fingerprint -> (count, window_start)
        self._lock = threading.Lock()

    def _should_skip(self, record):
        """Return True if this record should be ignored (noise filter)."""
        if record.name in self.SKIP_LOGGERS:
            return True
        try:
            msg = record.getMessage()
            for pat in self.SKIP_MESSAGE_SUBSTRINGS:
                if pat in msg:
                    return True
        except Exception:
            pass
        return False

    def _compute_fingerprint(self, record):
        """SHA256 of exception_type + top 5 normalized stack frames."""
        parts = []
        if record.exc_info and record.exc_info[1]:
            exc_type = type(record.exc_info[1]).__qualname__
            parts.append(exc_type)
            tb = traceback.extract_tb(record.exc_info[2])
            # Normalize: module:function for top 5 frames (from bottom)
            for frame in tb[-5:]:
                parts.append(f"{frame.filename}:{frame.name}")
        else:
            # No exception — fingerprint on module + message
            parts.append(record.module)
            parts.append(str(record.getMessage())[:200])

        raw = '|'.join(parts)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def _is_rate_limited(self, fingerprint):
        """Returns True if this fingerprint has exceeded the rate limit."""
        now = time.monotonic()
        with self._lock:
            count, window_start = self._rate_counts.get(fingerprint, (0, now))
            if now - window_start > self.RATE_LIMIT_WINDOW:
                # New window
                self._rate_counts[fingerprint] = (1, now)
                return False
            if count >= self.RATE_LIMIT_MAX:
                return True
            self._rate_counts[fingerprint] = (count + 1, window_start)
            return False

    def emit(self, record):
        if not self.webhook_url:
            return
        if self._should_skip(record):
            return

        try:
            fingerprint = self._compute_fingerprint(record)
            if self._is_rate_limited(fingerprint):
                return

            # Build payload matching Pulse generic webhook format
            payload = {
                'title': f"[{record.levelname}] {record.module}: {redact_webhook_secrets(record.getMessage())[:200]}",
                'description': self._format_description(record),
                'severity': 'critical' if record.levelno >= logging.CRITICAL else 'error',
                'source': f"{record.name}",
                'fingerprint': fingerprint,
            }

            # Fire in background thread to avoid blocking the request
            thread = threading.Thread(
                target=self._send,
                args=(payload,),
                daemon=True,
            )
            thread.start()
        except Exception:
            self.handleError(record)

    def _format_description(self, record):
        """Build a markdown description with exception details."""
        parts = [f"**Logger:** `{record.name}`"]
        parts.append(f"**File:** `{record.pathname}:{record.lineno}`")
        parts.append(f"**Function:** `{record.funcName}`")

        if record.exc_info and record.exc_info[1]:
            tb_str = redact_webhook_secrets(''.join(traceback.format_exception(*record.exc_info)))
            parts.append(f"\n```\n{tb_str[-2000:]}\n```")
        else:
            parts.append(f"\n**Message:** {redact_webhook_secrets(record.getMessage())[:1000]}")

        return '\n'.join(parts)

    def _send(self, payload):
        """POST payload to the Pulse webhook endpoint."""
        url = self.webhook_url
        if self.webhook_key:
            # Key is part of the URL path: /api/webhooks/generic/<key>/
            url = url.rstrip('/') + '/' + self.webhook_key + '/'

        data = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(
            url,
            data=data,
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        try:
            safe_urlopen(req, timeout=5)
        except (urllib.error.URLError, OSError):
            # Silently drop — we don't want error reporting to cause errors
            pass
