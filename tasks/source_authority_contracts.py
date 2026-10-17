"""Synchronous ORIG-18 cutover-contract client.

This adapter sits in the write transaction's validation path. A timeout,
malformed response, missing consumer, or explicit rejection aborts the mutation
before it can publish retired source-authority instructions.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from django.conf import settings


class SourceAuthorityContractError(RuntimeError):
    """Base class for typed contract-admission failures."""


class SourceAuthorityContractRejected(SourceAuthorityContractError):
    """The provider evaluated the candidate and found a retired contract."""


class SourceAuthorityContractUnavailable(SourceAuthorityContractError):
    """The provider could not return a trustworthy decision."""


def _summary(response: dict[str, Any]) -> str:
    violations = response.get("violations")
    if isinstance(violations, list) and violations and isinstance(violations[0], dict):
        violation = violations[0]
        return "%s/%s at %s: %s" % (
            violation.get("consumer", "registry"),
            violation.get("contract", "contract"),
            violation.get("location", "candidate"),
            violation.get("message", "rejected"),
        )
    return str(response.get("detail") or response.get("error") or "contract rejected")


def validate_source_authority_contract(
    mutation_kind: str,
    *,
    resource: dict[str, Any],
    candidate: Any,
    expected_consumer: str,
) -> dict[str, Any]:
    """Return the provider decision or raise before the caller saves.

    The explicit test bypass exists only under ``TESTING``; production cannot
    disable the contract by clearing a URL or toggling DEBUG.
    """
    if (
        getattr(settings, "TESTING", False)
        and getattr(settings, "SOURCE_AUTHORITY_CONTRACT_TEST_BYPASS", False)
    ):
        return {
            "decision": "allow",
            "registry_version": "test-bypass",
            "registry_sha256": "test-bypass",
            "matched_consumers": [expected_consumer],
            "violations": [],
        }

    url = str(getattr(settings, "SOURCE_AUTHORITY_CONTRACT_URL", "") or "").strip()
    if not url:
        raise SourceAuthorityContractUnavailable(
            "source-authority validator URL is not configured"
        )

    payload = json.dumps(
        {
            "protocol_version": 1,
            "mutation_kind": mutation_kind,
            "resource": resource,
            "candidate": candidate,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        url.rstrip("/") + "/contracts/validate",
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Shizuha-Event-Source": "pulse",
        },
    )
    timeout = float(
        getattr(settings, "SOURCE_AUTHORITY_CONTRACT_TIMEOUT_SECONDS", 3.0)
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            raw = response.read(1_000_001)
    except urllib.error.HTTPError as exc:
        raw = exc.read(1_000_001)
        try:
            detail = json.loads(raw.decode("utf-8"))
        except Exception:
            detail = {"error": "malformed_rejection", "detail": "non-JSON response"}
        if exc.code == 422:
            raise SourceAuthorityContractRejected(_summary(detail)) from exc
        raise SourceAuthorityContractUnavailable(
            "source-authority validator returned HTTP %s: %s"
            % (exc.code, _summary(detail))
        ) from exc
    except (OSError, TimeoutError, urllib.error.URLError) as exc:
        raise SourceAuthorityContractUnavailable(
            "source-authority validator unavailable: %s" % exc
        ) from exc

    if len(raw) > 1_000_000:
        raise SourceAuthorityContractUnavailable(
            "source-authority validator response exceeded 1 MB"
        )
    try:
        decision = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise SourceAuthorityContractUnavailable(
            "source-authority validator returned malformed JSON"
        ) from exc
    if status != 200 or not isinstance(decision, dict):
        raise SourceAuthorityContractUnavailable(
            "source-authority validator returned an invalid decision"
        )
    if decision.get("decision") != "allow":
        raise SourceAuthorityContractRejected(_summary(decision))
    if expected_consumer not in (decision.get("matched_consumers") or []):
        raise SourceAuthorityContractUnavailable(
            "source-authority registry did not evaluate required consumer %s"
            % expected_consumer
        )
    if not decision.get("registry_version") or not decision.get("registry_sha256"):
        raise SourceAuthorityContractUnavailable(
            "source-authority decision is missing its registry revision"
        )
    return decision
