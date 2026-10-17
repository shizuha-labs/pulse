"""Shared integrity helpers for op-typed workflow change requests.

The MCP proposal/revision/apply tools and the workflow transition engine must
hash and select seals identically.  Keep those primitives here rather than
duplicating security-sensitive canonicalisation in each caller.
"""
from __future__ import annotations

import hashlib
import json
import re


SEAL_MARKER = "TYPED-SPEC-SEAL"
CONTRACT_REF = (
    "op-typed contract v1.1, ENG wiki "
    "d328824d-055c-4a2f-a7ec-488fba708254"
)


def canonical_ops_json(operations):
    """Return the stable §3.7 digest input."""
    return json.dumps(
        operations,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def ops_digest(operations):
    return hashlib.sha256(canonical_ops_json(operations).encode("utf-8")).hexdigest()


def format_seal(operations, tier):
    return (
        f"{SEAL_MARKER} sha256={ops_digest(operations)} tier={tier} "
        f"ops={len(operations)} [{CONTRACT_REF} §3.7 — what's approved is what executes]"
    )


def parse_seal(text):
    if not isinstance(text, str) or SEAL_MARKER not in text:
        return None
    out = {}
    for token in text.split():
        if token.startswith("sha256="):
            out["sha256"] = token[len("sha256=") :]
        elif token.startswith("tier="):
            out["tier"] = token[len("tier=") :]
    return out if "sha256" in out and "tier" in out else None


def select_trusted_seal(comments, approved_at):
    """Return the latest pre-approval seal and post-approval seal timestamps."""
    trusted, trusted_ts, forged = None, "", []
    for comment in comments or []:
        parsed = parse_seal((comment or {}).get("content") or "")
        if not parsed:
            continue
        timestamp = str((comment or {}).get("created_at") or "")
        if approved_at and timestamp and timestamp >= str(approved_at):
            forged.append(timestamp)
            continue
        if timestamp >= trusted_ts:
            trusted, trusted_ts = parsed, timestamp
    return trusted, forged


def latest_approval_time(activities):
    latest = None
    for activity in activities or []:
        action = str((activity or {}).get("action") or "").strip().lower()
        new_value = str((activity or {}).get("new_value") or "").strip().lower()
        if action != "status_changed" or new_value != "approved":
            continue
        timestamp = str((activity or {}).get("created_at") or "")
        if timestamp and (latest is None or timestamp > latest):
            latest = timestamp
    return latest


_SPEC_SECTION_RE = re.compile(
    r"(?ms)(^### Change Specification\s*\n)(.*?)(?=^###\s|\Z)"
)
_SPEC_FORMAT_RE = re.compile(r"(?m)^\*\*Spec format\*\*:\s*.*$")
_JSON_FENCE_RE = re.compile(r"```json\s*\n(.*?)```", re.S)


def extract_typed_envelope(description):
    """Extract the typed envelope from the derived description projection."""
    section = _SPEC_SECTION_RE.search(description or "")
    if not section:
        return None
    fence = _JSON_FENCE_RE.search(section.group(2))
    if not fence:
        return None
    try:
        envelope = json.loads(fence.group(1))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return envelope if isinstance(envelope, dict) else None


def project_typed_envelope(description, envelope):
    """Regenerate the description's spec block from the authoritative envelope."""
    description = description or ""
    if not _SPEC_SECTION_RE.search(description):
        raise ValueError("workflow-change description has no Change Specification section")
    block = "```json\n" + json.dumps(envelope, indent=2, ensure_ascii=False) + "\n```\n\n"
    projected = _SPEC_SECTION_RE.sub(lambda match: match.group(1) + block, description, count=1)
    note = "**Spec format**: TYPED (op-typed contract — tier from op types)"
    if _SPEC_FORMAT_RE.search(projected):
        projected = _SPEC_FORMAT_RE.sub(note, projected, count=1)
    return projected
