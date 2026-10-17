"""Networking helpers with a scheme allowlist.

PLAT-1568/1570/1572/1574/1576 (bandit B310): raw ``urllib.request.urlopen``
happily opens ``file://``, ``ftp://`` and other non-HTTP schemes. Every Pulse
caller only ever talks HTTP(S) to code-controlled internal/config URLs, so a
scheme guard is pure defense-in-depth: if a config value (webhook URL, platform
base URL, agent-daemon URL) is ever misconfigured or injected to a non-HTTP
scheme, this refuses it instead of, e.g., reading a local file.

Route every ``urlopen`` through :func:`safe_urlopen` so the scheme check lives
in exactly one audited place.
"""
from __future__ import annotations

import urllib.request
from urllib.parse import urlsplit

_ALLOWED_SCHEMES = frozenset({'http', 'https'})


def _effective_url(url_or_request) -> str:
    # urllib accepts either a URL string or a Request object.
    full = getattr(url_or_request, 'full_url', None)
    return full if isinstance(full, str) else str(url_or_request)


def safe_urlopen(url_or_request, *, timeout=None, **kwargs):
    """``urllib.request.urlopen`` restricted to http/https.

    Accepts the same first argument as ``urlopen`` (a URL string or a
    ``Request``) and returns the same response object (usable as a context
    manager). Raises ``ValueError`` for any non-http(s) scheme.
    """
    scheme = urlsplit(_effective_url(url_or_request)).scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise ValueError(
            f'refusing to open non-HTTP(S) URL scheme {scheme!r} '
            f'(allowed: {sorted(_ALLOWED_SCHEMES)})'
        )
    # Accepted false positive (PLAT-3978): scheme is validated to http/https
    # immediately above; this is the single audited urlopen the codebase
    # routes through (PLAT-1568…1576). Bandit requires the nosec marker ON the
    # triggering line — a previous-line marker is ineffective.
    return urllib.request.urlopen(url_or_request, timeout=timeout, **kwargs)  # nosec B310  # noqa: S310
