"""Company-state snapshot aggregation for PLAT-1190 / PLAT-1197.

Read-only compute-on-request service for
``/api/internal/company-state/snapshot?schema_version=1&org_slug=...``.
The contract is intentionally honest about partial source failures: every
section returns an envelope with state/errors/data, and missing inputs are
reported as ``null``/``not_configured`` instead of silent zeroes.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from collections import Counter, OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

import requests
from django.core.cache import cache
from django.db.models import Count, Min, Q
from django.utils import timezone

from ..label_utils import item_labels_match_q
from ..models import AgentAvailability, Comment, Item, ItemLink, Project, Status
from .admin_service import admin_service
from taskmanager.topology import current_topology as _current_topology


SNAPSHOT_SCHEMA_V1 = "company_state.snapshot.v1"
V1_SECTION_NAMES = (
    "pulse_flow", "queues", "alerts_incidents", "product_backlog",
    "cortex", "hive_fleet", "spend_proxies", "learning_loop",
    "executive_directives",
)
DEFAULT_FRESHNESS_SLO_SECONDS = 3600
SECTION_CACHE_SECONDS = 60
OPTIONAL_UNAVAILABLE_STALE_AFTER = 900
EXTERNAL_SOURCE_STALE_AFTER = 300
DEFAULT_SOURCE_TIMEOUT_SECONDS = 2.0
# PLS-1409 / CON-466: monitoring + Books endpoints are env-configurable
# (PULSE_ALERTMANAGER_URL / PULSE_PROMETHEUS_URL / PULSE_BOOKS_URL) with
# first-party cluster defaults preserved for the live deployment.
DEFAULT_ALERTMANAGER_URL = _current_topology().alertmanager_url
DEFAULT_PROMETHEUS_URL = _current_topology().prometheus_url
# PLAT-5899 / PLAT-6004: Books is the sole authority for currency-denominated
# company accounting aggregates. Pulse delegates the current caller to the
# provider-owned read-only finance-summary endpoint and never widens its own
# company-state scope into Books authority.
DEFAULT_BOOKS_URL = _current_topology().books_url
BOOKS_FINANCE_SUMMARY_SCHEMA = "books.finance_summary.v1"
BOOKS_FINANCE_SUMMARY_PATH = "/api/finance-summary/"
# Canonical fixed-point money wire format: signed, 18/4 precision/scale, no
# exponent/NaN/Infinity, no leading '+', no negative zero.
BOOKS_MONEY_RE = re.compile(r"^-?(?:0|[1-9][0-9]{0,13})\.[0-9]{4}$")
BOOKS_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
BOOKS_LKG_MAX_ENTRIES = 64
# stale_deadline_mono = receipt_mono + min(900s, max(0, token_exp - receipt_wallclock))
BOOKS_LKG_STALE_MAX_SECONDS = 900
# Provider generated_at skew/age bounds for seeding LKG (HLD §5.3).
BOOKS_GENERATED_AT_MAX_FUTURE_SECONDS = 60
BOOKS_GENERATED_AT_MAX_AGE_SECONDS = 120
BOOKS_AMOUNT_FIELDS = (
    "accounting_income",
    "accounting_expenses",
    "accounting_net",
    "cash_and_bank",
    "receivables",
    "payables",
)
PRIORITY_RANK = {"urgent": 0, "high": 1, "normal": 2, "medium": 2, "low": 3}
_AS_OF_NOW = object()
_METRIC_LINE_RE = re.compile(
    r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*)\})?\s+'
    r'([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?|NaN|[+-]Inf)'
    r'(?:\s+\d+)?$'
)
_LABEL_RE = re.compile(r'(\w+)="((?:\\.|[^"\\])*)"')
_ITEM_KEY_RE = re.compile(r'\b[A-Z][A-Z0-9]+-\d+\b')
_MINUTE_REF_RE = re.compile(
    r'(?:Canonical minute:.*?page|canonical EVOL minute)\s+`'
    r'(?P<page>[0-9a-f-]{36})`(?:,|\s*\()?\s*version\s+(?P<version>\d+)',
    re.IGNORECASE | re.DOTALL,
)
CORTEX_FRESHNESS_FAMILIES = (
    "cortex_backend_healthy",
    "cortex_model_backends_expected",
    "cortex_model_backends_healthy",
    "cortex_vllm_route_fallback_total",
    "gb10_gpu_utilization_percent",
    "vllm:generation_tokens_total",
    "vllm:prompt_tokens_total",
    "vllm:request_success_total",
)
HIVE_HEALTH_METRICS = (
    "shizuha_agent_provider_healthy",
    "shizuha_agent_auth_failed",
    "shizuha_agent_bridge_auth_unavailable",
    "shizuha_agent_mcp_errors",
    "shizuha_agent_wedge_errors",
    "shizuha_agent_empty_turn_wedge",
    "shizuha_agent_pulse_wedge",
    "shizuha_agent_capacity_unavailable",
)
HIVE_LOAD_BEARING_METRICS = {
    "shizuha_exporter_last_gather_timestamp_seconds",
    "shizuha_agent_enabled",
    "shizuha_agent_up",
    *HIVE_HEALTH_METRICS,
}
LEGACY_TERMINAL_STATUSES = {
    "completed", "cancelled", "canceled", "closed", "done", "resolved",
    "rejected", "duplicate", "wont_fix", "merged", "failed", "expired",
    "deferred",
}


class SourceAdapterError(RuntimeError):
    """A bounded external source could not produce a trustworthy sample."""


class BooksLkgStore:
    """Process-local bounded last-known-good store for Books finance reads.

    PLAT-5899 §5.2: Pulse has NO shared per-org finance response cache and
    never returns cached finance instead of calling Books first. This store is
    process-local, bounded, and principal-partitioned: every key carries the
    numeric org, canonical subject, and a normalized signed-claim fingerprint,
    so an authorized caller's stale aggregate can never be replayed to a
    different subject/org. Bearers are never stored. A process restart clears
    the store (stale decisions then become unavailable, never silently fresh).
    """

    def __init__(self, max_entries: int = BOOKS_LKG_MAX_ENTRIES):
        self._entries: "OrderedDict[tuple, dict]" = OrderedDict()
        self._max_entries = max_entries
        self._lock = threading.Lock()

    def get(self, key: tuple) -> dict | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            self._entries.move_to_end(key)
            return dict(entry)

    def put(self, key: tuple, entry: dict) -> None:
        with self._lock:
            self._entries[key] = dict(entry)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    def purge_org_period(self, org_id: int, period: tuple) -> None:
        """Drop every entry for an org/period (config/404 invalidation)."""
        with self._lock:
            for key in [k for k in self._entries if k[0] == org_id and k[4] == period]:
                self._entries.pop(key, None)

    def purge_subject(self, org_id: int, subject: str) -> None:
        """Drop every entry for an org/subject (401/403 invalidation)."""
        with self._lock:
            for key in [k for k in self._entries if k[0] == org_id and k[1] == subject]:
                self._entries.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


# Single process-local store shared by every snapshot request in this process.
BOOKS_LKG = BooksLkgStore()


@dataclass
class SnapshotOrg:
    id: int | None
    slug: str
    name: str | None
    degraded: bool = False
    error: str | None = None


class CompanyStateSnapshotService:
    """Build the v1 company-state snapshot from existing Pulse-owned facts."""

    def __init__(
        self,
        *,
        org_slug: str = "shizuha",
        now=None,
        bearer_token: str | None = None,
        subject: str | None = None,
        claims: dict[str, Any] | None = None,
    ):
        self.org_slug = (org_slug or "shizuha").strip()
        self.now = now or timezone.now()
        self.org = self._resolve_org(self.org_slug)
        self._done_slugs = None
        self._inactive_slugs = None
        # PLAT-5899: the current caller's raw Bearer is forwarded to Books on
        # every finance read. Never cached, never logged, never substituted.
        self.bearer_token = bearer_token
        self.subject = subject
        self.claims = claims or {}

    def build_snapshot(self) -> dict[str, Any]:
        generated_at = self.now
        if self.org.id is None:
            return self._unresolved_org_snapshot(generated_at)
        sections = {
            "pulse_flow": self._cached_section("pulse_flow", self._pulse_flow_section),
            "queues": self._cached_section("queues", self._queues_section),
            "alerts_incidents": self._cached_section("alerts_incidents", self._alerts_incidents_section),
            "product_backlog": self._cached_section("product_backlog", self._product_backlog_section),
            "cortex": self._cached_section("cortex", self._cortex_section),
            "hive_fleet": self._cached_section("hive_fleet", self._hive_fleet_section),
            # PLAT-5899: spend_proxies is deliberately NOT in _cached_section.
            # Books is the final authorization authority on every reachable
            # request; the only finance cache is the process-local,
            # principal-partitioned failure-only LKG inside the adapter.
            "spend_proxies": self._spend_proxies_section(),
            "learning_loop": self._cached_section("learning_loop", self._learning_loop_section),
            "executive_directives": self._cached_section(
                "executive_directives", self._executive_directives_section
            ),
        }
        if self.org.degraded:
            sections["pulse_flow"]["errors"].append({
                "source": "admin",
                "message": self.org.error or "organization metadata unavailable",
            })

        sections = {
            name: self._normalize_freshness(name, section)
            for name, section in sections.items()
        }

        source_status = self._source_status(sections)
        missing_inputs = self._missing_inputs(sections)
        return {
            "schema_version": SNAPSHOT_SCHEMA_V1,
            "generated_at": generated_at.isoformat(),
            "org": {"id": self.org.id, "slug": self.org.slug, "name": self.org.name},
            "freshness_slo_seconds": DEFAULT_FRESHNESS_SLO_SECONDS,
            "overall_state": self._overall_state(sections),
            "sections": sections,
            "source_status": source_status,
            "missing_inputs": missing_inputs,
        }

    def _unresolved_org_snapshot(self, generated_at) -> dict[str, Any]:
        """Fail closed when the requested org cannot be positively resolved.

        ``SnapshotOrg(id=None)`` must never reach the Pulse aggregations:
        without an org id the item/project filters would silently widen to
        every org's data, a cross-org leak for this internal leadership
        endpoint. Return an unavailable snapshot with no Pulse data instead;
        the view maps ``overall_state == "unavailable"`` to HTTP 503.
        """
        message = self.org.error or f"organization '{self.org.slug}' could not be resolved"
        sections = {
            name: self._section(
                state="unavailable",
                sources=[{"system": "pulse", "ref": name}],
                errors=[{
                    "source": "admin",
                    "message": message,
                    "required_action": (
                        "Request a known org_slug; unresolved organizations are "
                        "refused to prevent cross-org aggregation."
                    ),
                    "owner_group": "architecture",
                    "related_task": "PLAT-1190",
                }],
                data=None,
                as_of=None,
            )
            for name in V1_SECTION_NAMES
        }
        return {
            "schema_version": SNAPSHOT_SCHEMA_V1,
            "generated_at": generated_at.isoformat(),
            "org": {"id": None, "slug": self.org.slug, "name": self.org.name},
            "freshness_slo_seconds": DEFAULT_FRESHNESS_SLO_SECONDS,
            "overall_state": "unavailable",
            "sections": sections,
            "source_status": self._source_status(sections),
            "missing_inputs": self._missing_inputs(sections),
        }

    def _resolve_org(self, slug: str) -> SnapshotOrg:
        try:
            org = admin_service.get_organization_by_slug(slug)
            if isinstance(org, dict):
                return SnapshotOrg(
                    id=org.get("id"),
                    slug=org.get("slug") or slug,
                    name=org.get("name") or org.get("display_name"),
                )
        except Exception as exc:  # pragma: no cover - defensive; tested through degraded fallback
            return SnapshotOrg(id=None, slug=slug, name=None, degraded=True, error=str(exc))
        # shizuha is the deployment default; keep the shell useful when admin is down.
        if slug == "shizuha":
            return SnapshotOrg(id=1, slug=slug, name="Shizuha Global Pvt. Ltd", degraded=True,
                               error="admin organization lookup unavailable")
        return SnapshotOrg(id=None, slug=slug, name=None, degraded=True,
                           error="admin organization lookup unavailable")

    def _cached_section(self, name: str, builder: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        key = f"company_state:v1:{self.org.slug}:{name}"
        cached = cache.get(key)
        if cached:
            return cached
        try:
            section = builder()
        except Exception as exc:
            section = self._section(
                state="unavailable",
                sources=[{"system": "pulse", "ref": name}],
                errors=[{"source": name, "message": str(exc)}],
                data=None,
                as_of=None,
            )
        cache.set(key, section, SECTION_CACHE_SECONDS)
        return section

    def _section(
        self,
        *,
        state: str,
        sources: list[dict[str, str]],
        data: Any,
        errors: list[dict[str, str]] | None = None,
        stale_after_seconds: int = DEFAULT_FRESHNESS_SLO_SECONDS,
        as_of=_AS_OF_NOW,
    ) -> dict[str, Any]:
        if as_of is _AS_OF_NOW:
            as_of_value = self.now.isoformat() if state != "not_configured" else None
        elif as_of is None:
            as_of_value = None
        elif hasattr(as_of, "isoformat"):
            as_of_value = as_of.isoformat()
        else:
            as_of_value = str(as_of)
        return {
            "state": state,
            "as_of": as_of_value,
            "stale_after_seconds": stale_after_seconds,
            "sources": sources,
            "errors": errors or [],
            "data": data,
        }

    def _not_configured_section(
        self,
        name: str,
        *,
        sources: list[dict[str, str]],
        required_action: str,
        owner_group: str,
        related_task: str,
    ) -> dict[str, Any]:
        return self._section(
            state="not_configured",
            sources=sources,
            data=None,
            errors=[{
                "source": name,
                "message": "source adapter not configured",
                "required_action": required_action,
                "owner_group": owner_group,
                "related_task": related_task,
            }],
            stale_after_seconds=OPTIONAL_UNAVAILABLE_STALE_AFTER,
            as_of=None,
        )

    def _global_telemetry_allowed(self) -> bool:
        """Global cluster metrics are visible only to the infrastructure org."""
        infra_slug = os.environ.get("COMPANY_STATE_INFRA_ORG_SLUG", "shizuha").strip()
        return bool(infra_slug and self.org.slug == infra_slug)

    def _global_source_not_configured(self, name: str, system: str) -> dict[str, Any]:
        return self._not_configured_section(
            name,
            sources=[{"system": system, "ref": "global source withheld for this org"}],
            required_action=(
                f"Provide a verified {self.org.slug}-scoped {system} filter before exposing "
                "cluster telemetry to this organization."
            ),
            owner_group="architecture",
            related_task="PLAT-4139",
        )

    @staticmethod
    def _source_timeout_seconds() -> float:
        try:
            value = float(os.environ.get(
                "COMPANY_STATE_SOURCE_TIMEOUT_SECONDS",
                str(DEFAULT_SOURCE_TIMEOUT_SECONDS),
            ))
        except (TypeError, ValueError):
            value = DEFAULT_SOURCE_TIMEOUT_SECONDS
        return min(max(value, 0.05), 10.0)

    @staticmethod
    def _http_base(env_name: str, default: str) -> str:
        value = os.environ.get(env_name, default).strip().rstrip("/")
        if not value.startswith(("http://", "https://")):
            raise SourceAdapterError(f"{env_name} must be an http(s) URL")
        return value

    @staticmethod
    def _response_json(response, *, source: str):
        try:
            response.raise_for_status()
            return response.json()
        except requests.Timeout:
            raise
        except Exception as exc:
            raise SourceAdapterError(f"{source} returned malformed/non-2xx response: {exc}") from exc

    def _normalize_freshness(self, name: str, section: dict[str, Any]) -> dict[str, Any]:
        """Turn old source samples into explicit stale/degraded section state."""
        as_of = section.get("as_of")
        if not as_of or section.get("state") in {"not_configured", "unavailable"}:
            return section
        try:
            sample_time = datetime.fromisoformat(str(as_of).replace("Z", "+00:00"))
            if sample_time.tzinfo is None:
                sample_time = sample_time.replace(tzinfo=dt_timezone.utc)
            age = max(0.0, (self.now - sample_time).total_seconds())
            stale_after = float(section.get("stale_after_seconds") or 0)
        except (TypeError, ValueError):
            return section
        if stale_after <= 0 or age <= stale_after:
            return section

        normalized = {**section, "errors": list(section.get("errors") or [])}
        required = name in {"pulse_flow", "queues", "alerts_incidents"}
        if normalized.get("state") == "ok":
            normalized["state"] = "stale" if required else "degraded"
        normalized["errors"].append({
            "source": name,
            "message": (
                f"source sample is stale ({int(age)}s old; limit {int(stale_after)}s)"
            ),
        })
        return normalized

    @staticmethod
    def _parse_metric_labels(raw: str | None) -> dict[str, str]:
        labels: dict[str, str] = {}
        for key, escaped in _LABEL_RE.findall(raw or ""):
            try:
                labels[key] = json.loads(f'"{escaped}"')
            except json.JSONDecodeError:
                labels[key] = escaped
        return labels

    @classmethod
    def _parse_prometheus_text(cls, text: str) -> dict[str, list[tuple[dict[str, str], float]]]:
        samples: dict[str, list[tuple[dict[str, str], float]]] = {}
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            metric_name = line.split(None, 1)[0].split("{", 1)[0]
            match = _METRIC_LINE_RE.match(line)
            if not match:
                if metric_name in HIVE_LOAD_BEARING_METRICS:
                    raise SourceAdapterError(
                        f"fleet exporter returned malformed {metric_name} sample"
                    )
                continue
            name, raw_labels, raw_value = match.groups()
            try:
                value = float(raw_value)
            except ValueError:
                if name in HIVE_LOAD_BEARING_METRICS:
                    raise SourceAdapterError(
                        f"fleet exporter returned malformed {name} value"
                    )
                continue
            if value != value or value in (float("inf"), float("-inf")):
                if name in HIVE_LOAD_BEARING_METRICS:
                    raise SourceAdapterError(
                        f"fleet exporter returned non-finite {name} value"
                    )
                continue
            samples.setdefault(name, []).append((cls._parse_metric_labels(raw_labels), value))
        return samples

    @staticmethod
    def _adapter_error(
        *, source: str, message: str, required_action: str,
        owner_group: str, related_task: str,
    ) -> dict[str, str]:
        return {
            "source": source,
            "message": message,
            "required_action": required_action,
            "owner_group": owner_group,
            "related_task": related_task,
        }

    # ------------------------------------------------------------------
    # PLAT-5899 / PLAT-6004: org-scoped Books finance adapter (spend_proxies)
    # ------------------------------------------------------------------
    def _spend_proxies_section(self) -> dict[str, Any]:
        """Books finance adapter for ``sections.spend_proxies``.

        Books is the final authorization and accounting authority. Pulse
        forwards the current caller's Bearer plus the exact numeric org id on
        every request, validates the ``books.finance_summary.v1`` contract, and
        maps every outcome to the reviewed honest state table. The only finance
        cache is the process-local, principal-partitioned failure-only LKG.
        """
        sources = [{
            "system": "books",
            "ref": "GET /api/finance-summary/ -> books.finance_summary.v1",
        }]
        if self.org.id is None:
            return self._section(
                state="unavailable",
                sources=sources,
                errors=[self._adapter_error(
                    source="books",
                    message="organization could not be positively resolved; refusing finance read",
                    required_action="Resolve a known org_slug before requesting finance data.",
                    owner_group="architecture",
                    related_task="PLAT-1190",
                )],
                data=None,
                stale_after_seconds=OPTIONAL_UNAVAILABLE_STALE_AFTER,
                as_of=None,
            )
        if not self.bearer_token:
            return self._section(
                state="unavailable",
                sources=sources,
                errors=[self._adapter_error(
                    source="books",
                    message="no current caller Bearer to forward to Books; finance read refused",
                    required_action=(
                        "Authenticate with a staff/superuser, org-lead, or "
                        "pulse:company-state.read token before requesting finance data."
                    ),
                    owner_group="engineering",
                    related_task="PLAT-5899",
                )],
                data=None,
                stale_after_seconds=OPTIONAL_UNAVAILABLE_STALE_AFTER,
                as_of=None,
            )
        try:
            return self._books_finance_section(sources)
        except Exception as exc:  # pragma: no cover - defensive
            return self._section(
                state="unavailable",
                sources=sources,
                errors=[self._adapter_error(
                    source="books",
                    message=f"Books finance adapter failed: {exc}",
                    required_action="Restore the Books finance-summary read path.",
                    owner_group="devops",
                    related_task="PLAT-5899",
                )],
                data=None,
                stale_after_seconds=OPTIONAL_UNAVAILABLE_STALE_AFTER,
                as_of=None,
            )

    def _books_base_url(self) -> str:
        return self._http_base("BOOKS_URL", DEFAULT_BOOKS_URL)

    def _books_period_tuple(self) -> tuple:
        # MTD is the only period Pulse requests today; the tuple keeps the LKG
        # key future-proof for custom periods without widening scope.
        return ("mtd",)

    def _books_claim_fingerprint(self) -> str:
        """Normalized signed-claim fingerprint for LKG partitioning.

        Covers issuer, sorted audience, subject, token org-membership/role and
        scopes relevant to Books, and token family/version where present. It is
        a digest of claims, never the raw token or the Bearer.
        """
        claims = self.claims or {}
        aud = claims.get("aud") or []
        if isinstance(aud, str):
            aud = [aud]
        memberships = claims.get("org_memberships") or claims.get(
            "organization_memberships") or {}
        if not isinstance(memberships, dict):
            memberships = {}
        scopes = claims.get("scope") or claims.get("scopes") or claims.get("permissions") or []
        if isinstance(scopes, str):
            scopes = scopes.split()
        normalized = {
            "iss": claims.get("iss"),
            "aud": sorted(str(item) for item in aud),
            "sub": self.subject,
            "org_memberships": {
                str(key): str(value) for key, value in sorted(memberships.items())
            },
            "scopes": sorted(str(item) for item in scopes),
            "token_family": claims.get("token_family") or claims.get("token_type"),
            "token_version": claims.get("token_version") or claims.get("ver"),
        }
        digest = hashlib.sha256(
            json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return digest

    def _books_lkg_key(self) -> tuple | None:
        if self.org.id is None or not self.subject:
            return None
        return (
            self.org.id,
            str(self.subject),
            self._books_claim_fingerprint(),
            BOOKS_FINANCE_SUMMARY_SCHEMA,
            self._books_period_tuple(),
        )

    @staticmethod
    def _books_money_valid(value: Any) -> bool:
        if not isinstance(value, str) or not BOOKS_MONEY_RE.fullmatch(value):
            return False
        try:
            parsed = Decimal(value)
        except InvalidOperation:
            return False
        if not parsed.is_finite():
            return False
        # Zero serializes as 0.0000, never negative zero (HLD §3.4).
        if parsed == 0 and value.startswith("-"):
            return False
        exponent = parsed.as_tuple().exponent
        # canonical 18/4: at most 14 integer digits and exactly scale 4
        if exponent != -4:
            return False
        digits = parsed.as_tuple().digits
        return len(digits) <= 18

    @staticmethod
    def _books_iso_timestamp(value: Any) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return None
        return parsed

    def _validate_books_finance_payload(
        self, payload: Any, receipt_wallclock: float,
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Validate the books.finance_summary.v1 contract.

        Returns ``(validated_finance, None)`` on success or
        ``(None, error_message)`` on an invalid contract. Invalid contracts
        never serve or seed LKG (HLD §5.2). ``receipt_wallclock`` is the local
        wall clock captured at receipt; provider ``generated_at`` must be
        timezone-aware UTC, <=60s future, and <=120s older than the receipt
        (HLD §5.3) — violations are ``invalid_source_contract``.
        """
        if not isinstance(payload, dict):
            return None, "Books finance payload is not a JSON object"
        if payload.get("schema_version") != BOOKS_FINANCE_SUMMARY_SCHEMA:
            return None, (
                f"unexpected Books schema {payload.get('schema_version')!r}; "
                f"expected {BOOKS_FINANCE_SUMMARY_SCHEMA}"
            )
        if payload.get("organization_id") != self.org.id:
            return None, (
                f"Books organization_id {payload.get('organization_id')!r} != "
                f"resolved org id {self.org.id}"
            )
        timezone_name = payload.get("accounting_timezone")
        if not isinstance(timezone_name, str) or not timezone_name.strip():
            return None, "Books accounting_timezone is missing or invalid"
        period = payload.get("period")
        if not isinstance(period, dict):
            return None, "Books period is missing or invalid"
        kind = period.get("kind")
        from_date = period.get("from_date")
        to_date = period.get("to_date")
        if kind not in {"mtd", "custom"}:
            return None, f"Books period kind {kind!r} is not allowed"
        for label, value in (("from_date", from_date), ("to_date", to_date)):
            if not isinstance(value, str):
                return None, f"Books period {label} is missing"
            try:
                datetime.strptime(value, "%Y-%m-%d")
            except ValueError:
                return None, f"Books period {label} {value!r} is not an ISO date"
        if str(from_date) > str(to_date):
            return None, "Books period from_date exceeds to_date"
        currency = payload.get("currency")
        if not isinstance(currency, str) or not BOOKS_CURRENCY_RE.fullmatch(currency):
            return None, f"Books currency {currency!r} is not a valid ISO-4217 code"
        generated_at = self._books_iso_timestamp(payload.get("generated_at"))
        if generated_at is None:
            return None, "Books generated_at is missing or not an aware timestamp"
        future_skew = (generated_at - datetime.now(dt_timezone.utc)).total_seconds()
        age = receipt_wallclock - generated_at.timestamp()
        if future_skew > BOOKS_GENERATED_AT_MAX_FUTURE_SECONDS:
            return None, (
                f"Books generated_at is {future_skew:.0f}s in the future; "
                f"exceeds the {BOOKS_GENERATED_AT_MAX_FUTURE_SECONDS}s bound"
            )
        if age > BOOKS_GENERATED_AT_MAX_AGE_SECONDS:
            return None, (
                f"Books generated_at is {age:.0f}s old; exceeds the "
                f"{BOOKS_GENERATED_AT_MAX_AGE_SECONDS}s bound"
            )
        amounts = payload.get("amounts")
        if not isinstance(amounts, dict):
            return None, "Books amounts is missing or invalid"
        missing = [field for field in BOOKS_AMOUNT_FIELDS if field not in amounts]
        if missing:
            return None, f"Books amounts missing field(s): {', '.join(missing)}"
        parsed_amounts: dict[str, Decimal] = {}
        for field in BOOKS_AMOUNT_FIELDS:
            value = amounts.get(field)
            if not self._books_money_valid(value):
                return None, f"Books amount {field} {value!r} is not canonical fixed-point money"
            parsed_amounts[field] = Decimal(value)
        # Signed natural-balance net invariant: net == income - expenses exactly.
        if parsed_amounts["accounting_net"] != (
            parsed_amounts["accounting_income"] - parsed_amounts["accounting_expenses"]
        ):
            return None, "Books accounting_net does not equal accounting_income - accounting_expenses"
        return {
            "schema_version": BOOKS_FINANCE_SUMMARY_SCHEMA,
            "organization_id": payload.get("organization_id"),
            "company_id": payload.get("company_id"),
            "generated_at": generated_at.isoformat().replace("+00:00", "Z"),
            "accounting_timezone": timezone_name,
            "period": {
                "kind": kind,
                "from_date": from_date,
                "to_date": to_date,
                "bounds": period.get("bounds") or "inclusive",
            },
            "currency": currency,
            "amounts": {field: str(parsed_amounts[field]) for field in BOOKS_AMOUNT_FIELDS},
            "last_financial_activity_at": payload.get("last_financial_activity_at"),
            "semantics": payload.get("semantics") if isinstance(payload.get("semantics"), dict) else {},
        }, None

    def _books_stale_deadline(self, receipt_mono: float) -> float:
        """stale_deadline_mono = receipt_mono + min(900s, max(0, token_exp - wallclock))."""
        claims = self.claims or {}
        exp = claims.get("exp")
        try:
            token_remaining = max(0, int(exp) - int(time.time()))
        except (TypeError, ValueError):
            token_remaining = BOOKS_LKG_STALE_MAX_SECONDS
        return receipt_mono + min(BOOKS_LKG_STALE_MAX_SECONDS, token_remaining)

    def _books_finance_section(self, sources: list[dict[str, str]]) -> dict[str, Any]:
        lkg_key = self._books_lkg_key()
        receipt_mono = time.monotonic()
        receipt_wallclock = time.time()
        try:
            base = self._books_base_url()
            response = requests.get(
                f"{base}{BOOKS_FINANCE_SUMMARY_PATH}",
                params={"period": "mtd"},
                headers={
                    "Authorization": f"Bearer {self.bearer_token}",
                    "X-Organization-ID": str(self.org.id),
                },
                timeout=self._source_timeout_seconds(),
            )
        except requests.RequestException as exc:
            # Transient transport failure: serve eligible exact-principal LKG
            # as stale, otherwise unavailable. Never seed LKG from a failure.
            if lkg_key is not None:
                entry = BOOKS_LKG.get(lkg_key)
                if entry is not None and receipt_mono <= entry["stale_deadline_mono"]:
                    return self._section(
                        state="stale",
                        sources=sources,
                        errors=[self._adapter_error(
                            source="books",
                            message=f"Books finance read failed transiently: {exc}",
                            required_action="Restore the Books finance-summary read path.",
                            owner_group="devops",
                            related_task="PLAT-5899",
                        )],
                        data=self._books_finance_data(entry["finance"]),
                        stale_after_seconds=OPTIONAL_UNAVAILABLE_STALE_AFTER,
                        as_of=entry["finance"]["generated_at"],
                    )
            return self._section(
                state="unavailable",
                sources=sources,
                errors=[self._adapter_error(
                    source="books",
                    message=f"Books finance read failed: {exc}",
                    required_action="Restore the Books finance-summary read path.",
                    owner_group="devops",
                    related_task="PLAT-5899",
                )],
                data=None,
                stale_after_seconds=OPTIONAL_UNAVAILABLE_STALE_AFTER,
                as_of=None,
            )

        status_code = response.status_code
        # 401/403: Books denied this principal. Purge/refuse every LKG entry
        # for this {org, subject} and never serve stale (HLD §5.2).
        if status_code in (401, 403):
            if lkg_key is not None:
                BOOKS_LKG.purge_subject(self.org.id, lkg_key[1])
            return self._section(
                state="unavailable",
                sources=sources,
                errors=[self._adapter_error(
                    source="books",
                    message="Books denied the forwarded caller authorization (401/403)",
                    required_action=(
                        "The current caller is not authorized by Books for this "
                        "organization; use a token Books accepts for the exact org."
                    ),
                    owner_group="engineering",
                    related_task="PLAT-5899",
                )],
                data=None,
                stale_after_seconds=OPTIONAL_UNAVAILABLE_STALE_AFTER,
                as_of=None,
            )

        try:
            payload = response.json()
        except ValueError:
            payload = None

        if status_code == 404:
            code = payload.get("code") if isinstance(payload, dict) else None
            if lkg_key is not None:
                BOOKS_LKG.purge_org_period(self.org.id, lkg_key[4])
            return self._section(
                state="not_configured",
                sources=sources,
                errors=[self._adapter_error(
                    source="books",
                    message=(
                        f"Books company not configured for org {self.org.id} "
                        f"({code or 'company_not_configured'})"
                    ),
                    required_action=(
                        "Configure the Books company/accounting for this "
                        "organization before finance data can be exposed."
                    ),
                    owner_group="accounting",
                    related_task="PLAT-5899",
                )],
                data=self._books_not_configured_data(),
                stale_after_seconds=OPTIONAL_UNAVAILABLE_STALE_AFTER,
                as_of=None,
            )
        if status_code == 409:
            code = payload.get("code") if isinstance(payload, dict) else None
            if lkg_key is not None:
                BOOKS_LKG.purge_org_period(self.org.id, lkg_key[4])
            return self._section(
                state="not_configured",
                sources=sources,
                errors=[self._adapter_error(
                    source="books",
                    message=(
                        f"Books accounting configuration required for org "
                        f"{self.org.id} ({code or 'accounting_configuration_required'})"
                    ),
                    required_action=(
                        "Set the Books company accounting timezone/currency/fiscal "
                        "period; no guessed timezone or currency is used."
                    ),
                    owner_group="accounting",
                    related_task="PLAT-5899",
                )],
                data=self._books_not_configured_data(),
                stale_after_seconds=OPTIONAL_UNAVAILABLE_STALE_AFTER,
                as_of=None,
            )
        if status_code == 400:
            return self._section(
                state="degraded",
                sources=sources,
                errors=[self._adapter_error(
                    source="books",
                    message="Books rejected the finance request (invalid period/request)",
                    required_action="Correct the Books finance request contract.",
                    owner_group="engineering",
                    related_task="PLAT-5899",
                )],
                data=None,
                stale_after_seconds=OPTIONAL_UNAVAILABLE_STALE_AFTER,
                as_of=None,
            )
        if status_code != 200:
            # 5xx or unexpected: transient unavailable; serve eligible LKG stale.
            if lkg_key is not None:
                entry = BOOKS_LKG.get(lkg_key)
                if entry is not None and receipt_mono <= entry["stale_deadline_mono"]:
                    return self._section(
                        state="stale",
                        sources=sources,
                        errors=[self._adapter_error(
                            source="books",
                            message=f"Books finance read returned HTTP {status_code}",
                            required_action="Restore the Books finance-summary read path.",
                            owner_group="devops",
                            related_task="PLAT-5899",
                        )],
                        data=self._books_finance_data(entry["finance"]),
                        stale_after_seconds=OPTIONAL_UNAVAILABLE_STALE_AFTER,
                        as_of=entry["finance"]["generated_at"],
                    )
            return self._section(
                state="unavailable",
                sources=sources,
                errors=[self._adapter_error(
                    source="books",
                    message=f"Books finance read returned HTTP {status_code}",
                    required_action="Restore the Books finance-summary read path.",
                    owner_group="devops",
                    related_task="PLAT-5899",
                )],
                data=None,
                stale_after_seconds=OPTIONAL_UNAVAILABLE_STALE_AFTER,
                as_of=None,
            )

        # 200: validate before exposing or seeding LKG. Contract validation
        # includes the provider timestamp skew/age bounds (HLD §5.3), so a
        # fresh valid 200 is the only thing that seeds LKG.
        finance, error = self._validate_books_finance_payload(payload, receipt_wallclock)
        if finance is None:
            # Invalid contract: never serve or seed stale (HLD §5.2).
            return self._section(
                state="degraded",
                sources=sources,
                errors=[self._adapter_error(
                    source="books",
                    message=f"Books finance contract invalid: {error}",
                    required_action="Fix the Books finance-summary contract mismatch.",
                    owner_group="engineering",
                    related_task="PLAT-5899",
                )],
                data=None,
                stale_after_seconds=OPTIONAL_UNAVAILABLE_STALE_AFTER,
                as_of=None,
            )

        if lkg_key is not None:
            BOOKS_LKG.put(lkg_key, {
                "finance": finance,
                "receipt_mono": receipt_mono,
                "stale_deadline_mono": self._books_stale_deadline(receipt_mono),
            })
        return self._section(
            state="ok",
            sources=sources,
            data=self._books_finance_data(finance),
            stale_after_seconds=EXTERNAL_SOURCE_STALE_AFTER,
            as_of=finance["generated_at"],
        )

    @staticmethod
    def _books_finance_data(finance: dict[str, Any]) -> dict[str, Any]:
        return {
            "finance": finance,
            "venture_attribution": {
                "state": "not_configured",
                "income_by_venture": None,
                "expenses_by_venture": None,
            },
        }

    @staticmethod
    def _books_not_configured_data() -> dict[str, Any]:
        return {
            "finance": None,
            "source_availability": "source_dark",
            "venture_attribution": {
                "state": "not_configured",
                "income_by_venture": None,
                "expenses_by_venture": None,
            },
        }

    def _done_statuses(self) -> set[str]:
        if self._done_slugs is None:
            self._done_slugs = (
                set(Status.objects.filter(category="done").values_list("slug", flat=True))
                | LEGACY_TERMINAL_STATUSES
            )
        return self._done_slugs

    def _inactive_statuses(self) -> set[str]:
        if self._inactive_slugs is None:
            scheduled = set(Status.objects.filter(category="scheduled").values_list("slug", flat=True))
            self._inactive_slugs = self._done_statuses() | scheduled
        return self._inactive_slugs

    def _items(self):
        if self.org.id is None:
            # Fail closed: build_snapshot() never reaches the aggregations for
            # an unresolved org, but keep the filter itself leak-proof too.
            return Item.objects.none()
        # Item has no independent org column. Projectless rows therefore have
        # no verifiable organization and must be excluded rather than admitted
        # into every resolved org snapshot.
        return Item.objects.filter(project__org_id=self.org.id)

    def _tasks(self):
        return self._items().filter(mode="task")

    def _active_tasks(self):
        return self._tasks().exclude(status__in=self._inactive_statuses())

    def _completion_filter(self, since):
        return (
            Q(resolution_date__gte=since)
            | Q(resolution_date__isnull=True, completed_at__gte=since)
        )

    def _pulse_flow_section(self) -> dict[str, Any]:
        from . import stage_metrics

        d1 = self.now - timedelta(days=1)
        d7 = self.now - timedelta(days=7)
        done = self._done_statuses()
        tasks = self._tasks()
        completed = tasks.filter(status__in=done)
        data = {
            "created": {
                "last_24h": tasks.filter(created_at__gte=d1).count(),
                "last_7d": tasks.filter(created_at__gte=d7).count(),
            },
            "completed": {
                "last_24h": completed.filter(self._completion_filter(d1)).count(),
                "last_7d": completed.filter(self._completion_filter(d7)).count(),
            },
            "net_flow": {
                "last_24h": tasks.filter(created_at__gte=d1).count()
                - completed.filter(self._completion_filter(d1)).count(),
                "last_7d": tasks.filter(created_at__gte=d7).count()
                - completed.filter(self._completion_filter(d7)).count(),
            },
            # Org-scoped: team_throughput defaults to Item.objects.all(), which
            # would leak cross-org stage dwell into a resolved-org snapshot.
            "stage_dwell_by_team_status": stage_metrics.team_throughput(days=7, items=self._tasks()),
        }
        return self._section(
            state="ok",
            sources=[{"system": "pulse", "ref": "Item/ItemActivity/stage_metrics"}],
            data=data,
        )

    def _queues_section(self) -> dict[str, Any]:
        active = self._active_tasks()
        rows = list(
            active.values("assignment_group", "project__key", "status", "priority")
            .annotate(count=Count("id"))
            .order_by("assignment_group", "project__key", "status", "priority")
        )
        oldest_by_team = list(
            active.exclude(assignment_group__isnull=True).exclude(assignment_group="")
            .values("assignment_group")
            .annotate(oldest_created_at=Min("created_at"), active_count=Count("id"))
            .order_by("assignment_group")
        )
        oldest_by_project = list(
            active.exclude(project__isnull=True)
            .values("project__key", "project__name")
            .annotate(oldest_created_at=Min("created_at"), active_count=Count("id"))
            .order_by("project__key")
        )
        data = {
            "active_total": active.count(),
            "by_team_project_status_priority": rows,
            "oldest_active_by_team": [
                {
                    **row,
                    "oldest_created_at": row["oldest_created_at"].isoformat()
                    if row.get("oldest_created_at") else None,
                }
                for row in oldest_by_team
            ],
            "oldest_active_by_project": [
                {
                    **row,
                    "oldest_created_at": row["oldest_created_at"].isoformat()
                    if row.get("oldest_created_at") else None,
                }
                for row in oldest_by_project
            ],
            "blocked_total": active.filter(status="blocked").count(),
            "review_lane_depth": active.filter(status__in=[
                "in_review", "awaiting_review", "awaiting_merge", "verification",
            ]).count(),
        }
        return self._section(
            state="ok",
            sources=[{"system": "pulse", "ref": "Item queue/status aggregates"}],
            data=data,
        )

    def _alerts_incidents_section(self) -> dict[str, Any]:
        alerts = self._items().filter(mode="alert", status__in=["firing", "acknowledged"])
        incidents = self._tasks().filter(workflow_name__iexact="incident")
        recent_cutoff = self.now - timedelta(days=7)
        pulse_data = {
            "pulse_alert_tasks_by_severity": list(
                alerts.values("severity").annotate(count=Count("id")).order_by("severity")
            ),
            "pulse_alert_tasks_by_owner_group": list(
                alerts.values("assignment_group").annotate(count=Count("id")).order_by("assignment_group")
            ),
            "open_pulse_incidents": incidents.exclude(status__in=self._done_statuses()).count(),
            "recent_resolved_incidents": incidents.filter(
                status__in=self._done_statuses()
            ).filter(self._completion_filter(recent_cutoff)).count(),
        }
        sources = [
            {"system": "pulse", "ref": "Item(mode=alert), incident workflow tasks"},
            {"system": "alertmanager", "ref": "/api/v2/alerts?active=true&silenced=false&inhibited=false"},
        ]
        if not self._global_telemetry_allowed():
            section = self._global_source_not_configured("alerts_incidents", "alertmanager")
            section["sources"] = sources
            section["data"] = {
                **pulse_data,
                "active_alerts_total": None,
                "active_alerts_by_severity": None,
                "active_alerts_by_owner_group": None,
                "active_alerts_by_alertname": None,
                "alertmanager": None,
            }
            return section

        try:
            base = self._http_base("ALERTMANAGER_URL", DEFAULT_ALERTMANAGER_URL)
            response = requests.get(
                f"{base}/api/v2/alerts",
                params={"active": "true", "silenced": "false", "inhibited": "false"},
                timeout=self._source_timeout_seconds(),
            )
            payload = self._response_json(response, source="alertmanager")
            if not isinstance(payload, list):
                raise SourceAdapterError("alertmanager payload is not a JSON array")
            severity = Counter()
            owner_group = Counter()
            alertname = Counter()
            for alert in payload:
                if not isinstance(alert, dict) or not isinstance(alert.get("labels", {}), dict):
                    raise SourceAdapterError("alertmanager array contains a malformed alert")
                labels = alert.get("labels") or {}
                severity[str(labels.get("severity") or "unknown")] += 1
                owner_group[str(labels.get("owner_group") or "unowned")] += 1
                alertname[str(labels.get("alertname") or "unknown")] += 1
            data = {
                **pulse_data,
                "active_alerts_total": len(payload),
                "active_alerts_by_severity": [
                    {"severity": key, "count": value} for key, value in sorted(severity.items())
                ],
                "active_alerts_by_owner_group": [
                    {"owner_group": key, "count": value}
                    for key, value in sorted(owner_group.items())
                ],
                "active_alerts_by_alertname": [
                    {"alertname": key, "count": value} for key, value in sorted(alertname.items())
                ],
                "alertmanager": {"reachable": True, "active_count": len(payload)},
            }
            return self._section(
                state="ok",
                sources=sources,
                data=data,
                stale_after_seconds=EXTERNAL_SOURCE_STALE_AFTER,
                # Alertmanager's API does not expose a snapshot timestamp;
                # receipt time is the approved live sample time.
                as_of=self.now,
            )
        except (requests.RequestException, SourceAdapterError, ValueError) as exc:
            return self._section(
                state="degraded",
                sources=sources,
                errors=[self._adapter_error(
                    source="alertmanager",
                    message=f"bounded Alertmanager read failed: {exc}",
                    required_action="Restore the Alertmanager API path/deadline for PLAT-4775.",
                    owner_group="devops",
                    related_task="PLAT-4775",
                )],
                data={
                    **pulse_data,
                    "active_alerts_total": None,
                    "active_alerts_by_severity": None,
                    "active_alerts_by_owner_group": None,
                    "active_alerts_by_alertname": None,
                    "alertmanager": None,
                },
                stale_after_seconds=EXTERNAL_SOURCE_STALE_AFTER,
                as_of=None,
            )

    def _prometheus_query(self, base: str, query: str, deadline: float):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise requests.Timeout("Prometheus source deadline exhausted")
        response = requests.get(
            f"{base}/api/v1/query",
            params={"query": query},
            timeout=max(0.05, remaining),
        )
        payload = self._response_json(response, source="prometheus")
        if not isinstance(payload, dict) or payload.get("status") != "success":
            raise SourceAdapterError("Prometheus payload has no success status")
        data = payload.get("data")
        result = data.get("result") if isinstance(data, dict) else None
        if not isinstance(result, list):
            raise SourceAdapterError("Prometheus result is not a vector")
        return result

    @staticmethod
    def _prometheus_value(result: list[dict[str, Any]]) -> tuple[float | None, datetime | None]:
        if not result:
            return None, None
        raw = result[0].get("value") if isinstance(result[0], dict) else None
        if not isinstance(raw, list) or len(raw) != 2:
            raise SourceAdapterError("Prometheus sample has no [timestamp, value]")
        try:
            value = float(raw[1])
            sample_time = datetime.fromtimestamp(float(raw[0]), tz=dt_timezone.utc)
        except (TypeError, ValueError, OverflowError) as exc:
            raise SourceAdapterError("Prometheus sample is malformed") from exc
        if value != value or value in (float("inf"), float("-inf")):
            raise SourceAdapterError("Prometheus sample is non-finite")
        return value, sample_time

    @staticmethod
    def _number(value: float | None):
        if value is None:
            return None
        rounded = round(value)
        return int(rounded) if abs(value - rounded) < 1e-9 else value

    def _cortex_section(self) -> dict[str, Any]:
        sources = [{"system": "prometheus", "ref": "Cortex/vLLM live metric queries"}]
        if not self._global_telemetry_allowed():
            section = self._global_source_not_configured("cortex", "prometheus")
            section["sources"] = sources
            return section

        queries = {
            "tokens_last_24h": (
                "sum(increase(vllm:prompt_tokens_total[24h])) + "
                "sum(increase(vllm:generation_tokens_total[24h]))"
            ),
            "requests_last_24h": "sum(increase(vllm:request_success_total[24h]))",
            "healthy_backends": "sum(cortex_backend_healthy)",
            "degraded_backends": "sum(1 - clamp_max(cortex_backend_healthy, 1))",
            "models_without_healthy_backend": (
                "sum(clamp_min(cortex_model_backends_expected - "
                "cortex_model_backends_healthy, 0))"
            ),
            "route_fallbacks_last_24h": "sum(increase(cortex_vllm_route_fallback_total[24h]))",
            "gpu_utilization_max_percent": "max(gb10_gpu_utilization_percent)",
        }
        model_query = (
            "topk(20, sum by (model_name,model,served_model_name) "
            "(increase(vllm:generation_tokens_total[24h]) + "
            "increase(vllm:prompt_tokens_total[24h])))"
        )
        field_families = {
            "tokens_last_24h": (
                "vllm:prompt_tokens_total", "vllm:generation_tokens_total",
            ),
            "requests_last_24h": ("vllm:request_success_total",),
            "healthy_backends": ("cortex_backend_healthy",),
            "degraded_backends": ("cortex_backend_healthy",),
            "models_without_healthy_backend": (
                "cortex_model_backends_expected", "cortex_model_backends_healthy",
            ),
            "route_fallbacks_last_24h": ("cortex_vllm_route_fallback_total",),
            "gpu_utilization_max_percent": ("gb10_gpu_utilization_percent",),
            "model_usage_last_24h": (
                "vllm:prompt_tokens_total", "vllm:generation_tokens_total",
            ),
        }
        data: dict[str, Any] = {}
        errors: list[dict[str, str]] = []
        sample_times: list[datetime] = []
        try:
            base = self._http_base("PROMETHEUS_URL", DEFAULT_PROMETHEUS_URL)
        except SourceAdapterError as exc:
            return self._section(
                state="unavailable", sources=sources, data=None, as_of=None,
                stale_after_seconds=EXTERNAL_SOURCE_STALE_AFTER,
                errors=[self._adapter_error(
                    source="prometheus", message=str(exc),
                    required_action="Restore the bounded Prometheus source configuration.",
                    owner_group="devops", related_task="PLAT-4775",
                )],
            )

        deadline = time.monotonic() + self._source_timeout_seconds()
        freshness_query = " or ".join(
            'label_replace(min(timestamp({family})), "family", "{family}", '
            '"__name__", ".*")'.format(family=family)
            for family in CORTEX_FRESHNESS_FAMILIES
        )
        family_times: dict[str, datetime] = {}
        freshness_failure = None
        try:
            freshness_result = self._prometheus_query(base, freshness_query, deadline)
            for row in freshness_result:
                labels = row.get("metric") if isinstance(row, dict) else None
                family = labels.get("family") if isinstance(labels, dict) else None
                if family not in CORTEX_FRESHNESS_FAMILIES:
                    raise SourceAdapterError(
                        "Prometheus freshness result has an unknown/missing family label"
                    )
                timestamp_value, _evaluation_time = self._prometheus_value([row])
                if timestamp_value is None:
                    raise SourceAdapterError(
                        f"Prometheus freshness result is empty for {family}"
                    )
                family_times[family] = datetime.fromtimestamp(
                    timestamp_value, tz=dt_timezone.utc,
                )
        except (requests.RequestException, SourceAdapterError, ValueError, OverflowError) as exc:
            freshness_failure = str(exc)

        def field_sample_time(field: str) -> tuple[datetime | None, str | None]:
            families = field_families[field]
            if freshness_failure:
                return None, f"raw-series freshness query failed: {freshness_failure}"
            missing = [family for family in families if family not in family_times]
            if missing:
                return None, "missing raw freshness family/families: " + ", ".join(missing)
            sample_time = min(family_times[family] for family in families)
            age = max(0.0, (self.now - sample_time).total_seconds())
            if age > EXTERNAL_SOURCE_STALE_AFTER:
                return None, (
                    f"raw source sample is stale ({int(age)}s old; "
                    f"limit {EXTERNAL_SOURCE_STALE_AFTER}s)"
                )
            return sample_time, None

        for field, query in queries.items():
            source_time, freshness_error = field_sample_time(field)
            if freshness_error:
                data[field] = None
                errors.append(self._adapter_error(
                    source="prometheus",
                    message=f"Prometheus source freshness invalid for {field}: {freshness_error}",
                    required_action=f"Restore fresh raw metric coverage for {field}.",
                    owner_group="cortex",
                    related_task="PLAT-4775",
                ))
                continue
            try:
                result = self._prometheus_query(base, query, deadline)
                value, _evaluation_time = self._prometheus_value(result)
                data[field] = self._number(value)
                if value is None:
                    errors.append(self._adapter_error(
                        source="prometheus",
                        message=f"Prometheus query returned an empty vector for {field}",
                        required_action=(
                            f"Restore/export the {field} metric; empty vectors are unknown, not zero."
                        ),
                        owner_group="cortex",
                        related_task="PLAT-4775",
                    ))
                else:
                    sample_times.append(source_time)
            except (requests.RequestException, SourceAdapterError, ValueError) as exc:
                data[field] = None
                errors.append(self._adapter_error(
                    source="prometheus",
                    message=f"bounded Prometheus query failed for {field}: {exc}",
                    required_action=f"Restore the {field} query within the source deadline.",
                    owner_group="cortex",
                    related_task="PLAT-4775",
                ))

        model_source_time, model_freshness_error = field_sample_time("model_usage_last_24h")
        try:
            if model_freshness_error:
                raise SourceAdapterError(model_freshness_error)
            model_result = self._prometheus_query(base, model_query, deadline)
            model_usage = []
            for row in model_result:
                value, _evaluation_time = self._prometheus_value([row])
                labels = row.get("metric") if isinstance(row, dict) else {}
                labels = labels if isinstance(labels, dict) else {}
                model_usage.append({
                    "model": (
                        labels.get("model_name") or labels.get("model")
                        or labels.get("served_model_name") or "unknown"
                    ),
                    "tokens_last_24h": self._number(value),
                })
            data["model_usage_last_24h"] = model_usage if model_result else None
            if not model_result:
                errors.append(self._adapter_error(
                    source="prometheus",
                    message="Prometheus model-usage query returned an empty vector",
                    required_action="Restore model-labelled vLLM token metrics.",
                    owner_group="cortex",
                    related_task="PLAT-4775",
                ))
            else:
                sample_times.append(model_source_time)
        except (requests.RequestException, SourceAdapterError, ValueError) as exc:
            data["model_usage_last_24h"] = None
            errors.append(self._adapter_error(
                source="prometheus",
                message=f"bounded Prometheus query failed for model usage: {exc}",
                required_action="Restore model-labelled vLLM token metrics within the deadline.",
                owner_group="cortex",
                related_task="PLAT-4775",
            ))

        # No approved provider-labelled request/timeout series exists today.
        # Keep the unknown explicit instead of assigning token volume to a
        # provider or treating route fallback count as a timeout count.
        data["provider_usage_last_24h"] = None
        data["provider_timeout_signals_last_24h"] = None
        errors.append(self._adapter_error(
            source="cortex",
            message="provider-labelled usage/timeout metrics are not exported",
            required_action="Export org-safe provider usage and timeout counters.",
            owner_group="cortex",
            related_task="PLAT-4775",
        ))

        state = "degraded" if errors else "ok"
        if not sample_times:
            state = "unavailable"
        return self._section(
            state=state,
            sources=sources,
            data=data if sample_times else None,
            errors=errors,
            stale_after_seconds=EXTERNAL_SOURCE_STALE_AFTER,
            # The oldest contributing sample is the conservative section age.
            as_of=min(sample_times) if sample_times else None,
        )

    @staticmethod
    def _metric_agent_values(
        samples: dict[str, list[tuple[dict[str, str], float]]], name: str,
    ) -> dict[str, float]:
        values = {}
        for labels, value in samples.get(name, []):
            agent = (labels.get("agent") or "").strip().lower()
            if agent:
                values[agent] = value
        return values

    def _hive_fleet_section(self) -> dict[str, Any]:
        sources = [
            {"system": "hive", "ref": "GET /api/internal/company-state/fleet/v1/"},
        ]
        if not self._global_telemetry_allowed():
            section = self._global_source_not_configured("hive_fleet", "hive exporter")
            section["sources"] = sources
            return section

        url = os.environ.get("COMPANY_STATE_HIVE_URL", "").strip()
        token = os.environ.get("COMPANY_STATE_HIVE_TOKEN", "").strip()
        if not url or url.lower() == "disabled" or not url.startswith(("http://", "https://")):
            return self._section(
                state="unavailable", sources=sources, data=None, as_of=None,
                stale_after_seconds=EXTERNAL_SOURCE_STALE_AFTER,
                errors=[self._adapter_error(
                    source="hive_fleet",
                    message="COMPANY_STATE_HIVE_URL is not configured with an http(s) endpoint",
                    required_action="Restore the bounded Hive company-state URL.",
                    owner_group="devops", related_task="PLAT-4775",
                )],
            )
        if not token:
            return self._section(
                state="unavailable", sources=sources, data=None, as_of=None,
                stale_after_seconds=EXTERNAL_SOURCE_STALE_AFTER,
                errors=[self._adapter_error(
                    source="hive_fleet",
                    message="COMPANY_STATE_HIVE_TOKEN is not configured",
                    required_action="Restore the dedicated Pulse-to-Hive read credential.",
                    owner_group="devops", related_task="HIVE-1878",
                )],
            )
        try:
            response = requests.get(
                url,
                params={"org_slug": self.org.slug},
                headers={"Authorization": f"Bearer {token}"},
                timeout=self._source_timeout_seconds(),
            )
            payload = self._response_json(response, source="Hive company-state")
            if payload.get("schema_version") != "hive.company_state.fleet.v1":
                raise SourceAdapterError("Hive company-state returned an unsupported schema")
            if payload.get("status") == "not_configured":
                return self._section(
                    state="not_configured", sources=sources, data=None, errors=[], as_of=None,
                    stale_after_seconds=EXTERNAL_SOURCE_STALE_AFTER,
                )
            if payload.get("scope") != "platform_aggregate":
                raise SourceAdapterError("Hive company-state returned an invalid scope")
            observed_at = datetime.fromisoformat(
                str(payload.get("observed_at") or "").replace("Z", "+00:00")
            )
            if observed_at.tzinfo is None:
                raise SourceAdapterError("Hive company-state observed_at must be timezone-aware")
            fleet = payload.get("fleet")
            tenants = payload.get("tenants")
            agents = payload.get("agents")
            if not isinstance(fleet, dict) or not isinstance(tenants, dict) or not isinstance(agents, list):
                raise SourceAdapterError("Hive company-state omitted fleet/tenant/agent data")

            provider_down = []
            auth_unavailable = []
            wedged = []
            capacity_unavailable = []
            incomplete = False
            for agent in agents:
                username = str(agent.get("username") or "").strip().lower()
                if not username:
                    raise SourceAdapterError("Hive company-state returned an unnamed agent")
                families = {name: agent.get(name) for name in (
                    "provider_healthy", "auth_failed", "bridge_auth_unavailable",
                    "mcp_errors", "wedge_errors", "empty_turn_wedge", "pulse_wedge",
                    "capacity_unavailable",
                )}
                if any(not isinstance(value, dict) or "known" not in value for value in families.values()):
                    raise SourceAdapterError(f"Hive company-state health contract is incomplete for {username}")
                if any(value.get("known") is not True for value in families.values()):
                    incomplete = True
                    continue
                if families["provider_healthy"].get("value") is False:
                    provider_down.append(username)
                if any(families[name].get("value") for name in (
                    "auth_failed", "bridge_auth_unavailable", "mcp_errors",
                )):
                    auth_unavailable.append(username)
                if any(families[name].get("value") for name in (
                    "wedge_errors", "empty_turn_wedge", "pulse_wedge",
                )):
                    wedged.append(username)
                if families["capacity_unavailable"].get("value") is True:
                    capacity_unavailable.append(username)

            unavailable = set(provider_down) | set(auth_unavailable) | set(wedged) | set(capacity_unavailable)
            enabled = fleet.get("enabled")
            running = fleet.get("running")
            stopped = fleet.get("stopped")
            if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in (enabled, running, stopped)):
                raise SourceAdapterError("Hive company-state fleet counts are invalid")
            data = {
                "tenant_count": tenants.get("total"),
                "tenant_summary": tenants,
                "fleet": {
                    "enabled": enabled,
                    "running": running,
                    "available": None if incomplete else max(0, enabled - len(unavailable)),
                    "stopped": stopped,
                    "unavailable": None if incomplete else len(unavailable),
                },
                "provider_down_agents": None if incomplete else sorted(provider_down),
                "auth_unavailable_agents": None if incomplete else sorted(auth_unavailable),
                "wedged_agents": None if incomplete else sorted(wedged),
                "capacity_unavailable_agents": None if incomplete else sorted(capacity_unavailable),
            }
            errors = []
            if incomplete:
                errors.append(self._adapter_error(
                    source="hive_fleet",
                    message="Hive company-state health contract contains unknown agent families",
                    required_action="Restore fresh explicit health for every agent family; unknown is not zero.",
                    owner_group="hive", related_task="PLAT-4775",
                ))
            return self._section(
                state="degraded" if errors else "ok",
                sources=sources,
                data=data,
                errors=errors,
                stale_after_seconds=EXTERNAL_SOURCE_STALE_AFTER,
                as_of=observed_at,
            )
        except (requests.RequestException, SourceAdapterError, ValueError, OverflowError) as exc:
            return self._section(
                state="unavailable", sources=sources, data=None, as_of=None,
                stale_after_seconds=EXTERNAL_SOURCE_STALE_AFTER,
                errors=[self._adapter_error(
                    source="hive_fleet",
                    message=f"bounded Hive company-state read failed: {exc}",
                    required_action="Restore a fresh, valid Hive company-state response.",
                    owner_group="devops", related_task="PLAT-4775",
                )],
            )

    def _product_backlog_section(self) -> dict[str, Any]:
        if self.org.id is None:
            product_projects = Project.objects.none()
        else:
            product_projects = Project.objects.filter(org_id=self.org.id)
        active = self._active_tasks()
        rows = list(
            active.exclude(project__isnull=True)
            .values("project__key", "project__name", "priority")
            .annotate(count=Count("id"))
            .order_by("project__key", "priority")
        )
        urgent_high = list(
            active.filter(priority__in=["urgent", "high"])
            .exclude(project__isnull=True)
            .values("item_key", "title", "priority", "project__key", "created_at")
            .order_by("priority", "created_at")[:20]
        )
        for item in urgent_high:
            if item.get("created_at"):
                item["created_at"] = item["created_at"].isoformat()
        return self._section(
            state="ok",
            sources=[{"system": "pulse", "ref": "Project/Item backlog aggregates"}],
            data={
                "project_count": product_projects.count(),
                "active_counts_by_project_priority": rows,
                "top_urgent_high_items": urgent_high,
            },
        )

    def _learning_loop_section(self) -> dict[str, Any]:
        from .learning_loop import compute_learning_loop_metrics, previous_month_bounds

        period_start, period_end = previous_month_bounds(self.now)
        metrics = compute_learning_loop_metrics(
            period_start,
            period_end,
            now=self.now,
            base_items=self._tasks(),
        )
        return self._section(
            state="ok",
            sources=[{"system": "pulse", "ref": "PLAT-1192 compute_learning_loop_metrics"}],
            errors=[],
            data={
                **metrics,
                "stale_open_prevention_mechanism_tasks_gt_30d": len(
                    metrics["stale_mechanisms"]
                ),
            },
        )

    @staticmethod
    def _parse_cycle_directive_keys(content: str) -> list[str]:
        marker = re.search(
            r"Top three for the next boundary:\s*(?P<body>.+?)(?:Canonical minute:|No shareholder escalation|$)",
            content,
            re.IGNORECASE | re.DOTALL,
        )
        if not marker:
            return []
        body = marker.group("body")
        segments = re.split(r"\s*\([123]\)\s*", body)
        keys = []
        for segment in segments[1:4]:
            match = _ITEM_KEY_RE.search(segment)
            if match and match.group(0) not in keys:
                keys.append(match.group(0))
        return keys

    def _current_cycle_metadata(self, parent: Item) -> tuple[Comment | None, list[str], dict[str, Any] | None]:
        comments = Comment.objects.filter(item=parent).exclude(author_email="system@pulse").order_by(
            "-created_at"
        )[:50]
        for comment in comments:
            content = comment.content or ""
            if not re.search(r"\bCycle\s+#?\d+\s+complete\b", content, re.IGNORECASE):
                continue
            keys = self._parse_cycle_directive_keys(content)
            minute_match = _MINUTE_REF_RE.search(content)
            minute = None
            if minute_match:
                minute = {
                    "page_id": minute_match.group("page"),
                    "version": int(minute_match.group("version")),
                    "source_task": "PLAT-1189",
                    "published_at": comment.created_at.isoformat(),
                }
            # The recurring close path also emits a terse status echo after
            # the structured completion record. It is not the machine source
            # and must not shadow the preceding directive/minute payload.
            if not keys and minute is None:
                continue
            return comment, keys, minute
        return None, [], None

    def _linked_work(self, item: Item, *, exclude_id: int | None = None) -> list[str]:
        if self.org.id is None:
            return []
        links = ItemLink.objects.filter(
            Q(source_item=item) | Q(target_item=item),
            source_item__project__org_id=self.org.id,
            target_item__project__org_id=self.org.id,
        ).select_related("source_item", "target_item")
        keys = set()
        for link in links:
            other = link.target_item if link.source_item_id == item.id else link.source_item
            if other.id != exclude_id and other.item_key:
                keys.add(other.item_key)
        return sorted(keys)

    def _executive_directives_section(self) -> dict[str, Any]:
        active = self._active_tasks()
        directive_q = (
            Q(tags__icontains="executive-directive")
            | item_labels_match_q(["executive-directive"])
        )
        parent = self._tasks().filter(item_key="PLAT-1189").first()
        cycle_comment = None
        cycle_keys: list[str] = []
        minute_ref = None
        if parent:
            cycle_comment, cycle_keys, minute_ref = self._current_cycle_metadata(parent)

        candidates = list(active.filter(directive_q).select_related("project"))
        by_key = {item.item_key: item for item in candidates if item.item_key}
        selected = [by_key[key] for key in cycle_keys if key in by_key]

        # Strict current-cycle metadata is preferred. If an older cycle did not
        # publish the structured completion comment, fall back only to tasks
        # machine-related/parented to PLAT-1189 — never scrape wiki prose.
        if not selected and parent:
            linked_ids = set(
                ItemLink.objects.filter(source_item=parent).values_list("target_item_id", flat=True)
            ) | set(
                ItemLink.objects.filter(target_item=parent).values_list("source_item_id", flat=True)
            )
            selected = [
                item for item in candidates
                if item.parent_item_id == parent.id or item.id in linked_ids
            ]

        cycle_position = {key: index for index, key in enumerate(cycle_keys)}
        selected.sort(key=lambda item: (
            PRIORITY_RANK.get((item.priority or "").lower(), 99),
            cycle_position.get(item.item_key, 99),
            item.due_date or datetime.max.replace(tzinfo=dt_timezone.utc),
            item.created_at,
        ))
        directives = []
        for item in selected[:3]:
            directives.append({
                "item_key": item.item_key,
                "title": item.title,
                "status": item.status,
                "priority": item.priority,
                "priority_rank": PRIORITY_RANK.get((item.priority or "").lower()),
                "owner": {
                    "assignee_email": item.assignee_email,
                    "owner_group": item.assignment_group,
                },
                "due_date": item.due_date.isoformat() if item.due_date else None,
                "related_work": self._linked_work(item, exclude_id=parent.id if parent else None),
            })

        errors = []
        if not directives:
            errors.append(self._adapter_error(
                source="pulse",
                message="No active current-cycle tasks labeled executive-directive",
                required_action="Publish the cycle's top three as labelled org-scoped Pulse tasks.",
                owner_group="admin-ops",
                related_task="PLAT-1189",
            ))
        if minute_ref is None:
            errors.append(self._adapter_error(
                source="pulse",
                message="Current-cycle completion comment has no strict canonical minute reference",
                required_action="Publish the canonical EVOL page id and version in the cycle completion comment.",
                owner_group="admin-ops",
                related_task="PLAT-1189",
            ))
        state = "ok" if directives and minute_ref else "degraded"
        return self._section(
            state=state,
            sources=[
                {"system": "pulse", "ref": "PLAT-1189 cycle completion + labelled directive tasks"},
                {"system": "wiki", "ref": "canonical minute reference only (prose not parsed)"},
            ],
            errors=errors,
            data={
                "top_directives": directives,
                "latest_minutes_ref": minute_ref,
                "directive_priority_order": ["urgent", "high", "normal", "low"],
                "directive_parsing_state": "ok" if directives and minute_ref else "degraded",
            },
            as_of=cycle_comment.created_at if cycle_comment else self.now,
        )

    def _source_status(self, sections: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for section_name, section in sections.items():
            for src in section.get("sources") or []:
                out.append({
                    "section": section_name,
                    "system": src.get("system"),
                    "ref": src.get("ref"),
                    "state": section.get("state"),
                    "as_of": section.get("as_of"),
                })
        return out

    def _missing_inputs(self, sections: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        missing = []
        terminal_related = self._terminal_related_tasks(sections)
        for section_name, section in sections.items():
            if section.get("state") in {"ok"}:
                continue
            for err in section.get("errors") or []:
                related = err.get("related_task")
                # PLAT-5899: terminal task owners (e.g. VEN-99) must never
                # remain an active missing-input owner; suppress them so the
                # Executive Cycle does not route work to a finished item.
                if related and related in terminal_related:
                    continue
                if err.get("required_action") or related:
                    missing.append({
                        "section": section_name,
                        "owner_group": err.get("owner_group") or "architecture",
                        "required_action": err.get("required_action") or err.get("message"),
                        "related_task": related or "PLAT-1190",
                    })
        return missing

    def _terminal_related_tasks(self, sections: dict[str, dict[str, Any]]) -> set[str]:
        """Return the set of related_task keys that reference terminal items.

        Best-effort rendering concern: if the DB is unavailable (or the test
        harness forbids queries), degrade to no suppression rather than failing
        the whole snapshot. Production always has the DB, so terminal owners
        (e.g. VEN-99) are suppressed there.
        """
        keys = {
            err.get("related_task")
            for section in sections.values()
            for err in section.get("errors") or []
            if err.get("related_task")
        }
        if not keys:
            return set()
        try:
            terminal = self._done_statuses()
            return set(
                Item.objects.filter(
                    item_key__in=keys,
                    status__in=terminal,
                ).values_list("item_key", flat=True)
            )
        except Exception:  # pragma: no cover - defensive; DB unavailable/forbidden
            return set()

    def _overall_state(self, sections: dict[str, dict[str, Any]]) -> str:
        required = ["pulse_flow", "queues", "alerts_incidents"]
        if any(sections[name]["state"] == "unavailable" for name in required):
            return "unavailable"
        if any(
            sections[name]["state"] in {"degraded", "stale", "not_configured"}
            for name in required
        ):
            return "degraded"
        optional_states = [s["state"] for name, s in sections.items() if name not in required]
        if any(state in {"unavailable", "degraded", "stale"} for state in optional_states):
            return "degraded"
        return "ok"
