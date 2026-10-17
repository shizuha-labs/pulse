"""PLS-1409 / CON-466: env-configurable topology for first-party endpoints.

Pulse is deployable outside the first-party cluster only if every hardcoded
first-party host/URL (origin.shizuha.com, *.svc.cluster.local, tailscale
CGNAT, gx10-*) is configurable through environment variables. This module is
the single SSOT for those values:

- Defaults preserve current first-party behaviour — an unconfigured
  deployment on the shizuha cluster behaves exactly as before (no live-Pulse
  break; verified by the topology tests).
- Third-party deployments set the env vars (or clear them with documented
  placeholders) — no code path REQUIRES a first-party hostname.

Values are read at import time from the environment and can be reloaded in
tests via :func:`reload_topology` (thread-safe).
"""

from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass

_LOCK = threading.Lock()


def _env(name: str, default: str) -> str:
    """Read an env var. Missing uses the default. An explicit empty value stays empty."""
    value = os.environ.get(name)
    if value is None:
        return default
    return value


def _csv(name: str, default: str) -> tuple[str, ...]:
    raw = _env(name, default)
    return tuple(part.strip() for part in raw.split(",") if part.strip())


@dataclass(frozen=True)
class Topology:
    """First-party topology endpoints, all env-overridable (PLS-1409)."""

    # Public Forgejo/Origin base URL used for agent/browser-facing URLs.
    origin_public_url: str = "https://origin.shizuha.com"
    # In-cluster Forgejo HTTP service (CI writers historically record this).
    origin_internal_http_url: str = "http://forgejo-http.origin.svc.cluster.local"
    # CI run-log service base (run evidence links for DevOps seats).
    ci_logs_base_url: str = "http://ci-logs.origin.svc.cluster.local:8080"
    # Monitoring endpoints consumed by company-state (PLAT-5899/6004 Books).
    alertmanager_url: str = (
        "http://kube-prometheus-kube-prome-alertmanager.monitoring.svc.cluster.local:9093"
    )
    prometheus_url: str = (
        "http://kube-prometheus-kube-prome-prometheus.monitoring.svc.cluster.local:9090"
    )
    books_url: str = "http://shizuha-books.shizuha.svc.cluster.local:8021"
    # Extra public Origin host aliases accepted when parsing persisted URLs.
    origin_public_host_aliases: tuple[str, ...] = ()
    # First-party node prefix and Tailscale CGNAT. Empty means "not this deploy".
    gx10_host_prefix: str = "gx10"
    tailscale_cgnat: str = "100.64.0.0/10"

    @property
    def origin_public_host(self) -> str:
        """Hostname (no scheme) of the public Origin URL."""
        from urllib.parse import urlsplit

        return urlsplit(self.origin_public_url).hostname or ""

    @property
    def origin_public_hosts(self) -> frozenset[str]:
        """All public Origin hostnames accepted as canonical/legacy inputs."""
        return frozenset({self.origin_public_host, *self.origin_public_host_aliases})

    @property
    def origin_internal_hosts(self) -> frozenset[str]:
        """In-cluster Origin service hostnames (never published externally)."""
        from urllib.parse import urlsplit

        internal = urlsplit(self.origin_internal_http_url)
        host = internal.hostname or "forgejo-http.origin.svc.cluster.local"
        return frozenset({host, host.removesuffix(".cluster.local")})

    @property
    def origin_api_url(self) -> str:
        """Forgejo REST API base on the public Origin URL."""
        return self.origin_public_url.rstrip("/") + "/api/v1"

    @property
    def origin_host_pattern(self) -> str:
        """Regex alternation matching every accepted Origin hostname (grouped)."""
        hosts = sorted(self.origin_public_hosts | self.origin_internal_hosts, key=len, reverse=True)
        return "(?:" + "|".join(re.escape(h) for h in hosts) + ")"


_topology: Topology | None = None


def current_topology() -> Topology:
    """Return the process-wide topology snapshot (reading env on first use)."""
    global _topology
    if _topology is None:
        with _LOCK:
            if _topology is None:
                _topology = _read_env_topology()
    return _topology


def _read_env_topology() -> Topology:
    return Topology(
        origin_public_url=_env("PULSE_ORIGIN_PUBLIC_URL", "https://origin.shizuha.com"),
        origin_internal_http_url=_env(
            "PULSE_ORIGIN_INTERNAL_HTTP_URL", "http://forgejo-http.origin.svc.cluster.local"
        ),
        ci_logs_base_url=_env("PULSE_CI_LOGS_BASE_URL", "http://ci-logs.origin.svc.cluster.local:8080"),
        alertmanager_url=_env(
            "PULSE_ALERTMANAGER_URL",
            "http://kube-prometheus-kube-prome-alertmanager.monitoring.svc.cluster.local:9093",
        ),
        prometheus_url=_env(
            "PULSE_PROMETHEUS_URL",
            "http://kube-prometheus-kube-prome-prometheus.monitoring.svc.cluster.local:9090",
        ),
        books_url=_env("PULSE_BOOKS_URL", "http://shizuha-books.shizuha.svc.cluster.local:8021"),
        origin_public_host_aliases=_csv("PULSE_ORIGIN_PUBLIC_HOST_ALIASES", "origin.shizuha"),
        gx10_host_prefix=_env("PULSE_GX10_HOST_PREFIX", "gx10"),
        tailscale_cgnat=_env("PULSE_TAILSCALE_CGNAT", "100.64.0.0/10"),
    )


def reload_topology() -> Topology:
    """Re-read the environment into a fresh Topology (tests, config changes)."""
    global _topology
    with _LOCK:
        _topology = _read_env_topology()
        return _topology


def ci_logs_url(repo: str, run_number: int | str, params: dict[str, str] | None = None) -> str:
    """Build the CI run-log URL for a repo/run from the configured base."""
    from urllib.parse import urlencode

    query = {"repo": repo, "run": run_number, **(params or {})}
    return current_topology().ci_logs_base_url.rstrip("/") + "/logs?" + urlencode(query)
