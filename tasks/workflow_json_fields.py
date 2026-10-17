"""Helpers for workflow transition JSON-list payloads.

Workflow transition JSONField columns are writable through REST and MCP. Some
callers pass native JSON arrays, while MCP callers often pass JSON-encoded
strings. Normalize all write paths to typed lists and make read/runtime paths
fail-closed for malformed legacy rows.
"""

from __future__ import annotations

import json
from typing import Any, Literal


ListItemType = Literal["dict", "str"]


def normalize_json_list_field(
    value: Any,
    field_name: str,
    *,
    item_type: ListItemType = "dict",
    reject_malformed: bool = False,
) -> list[dict[str, Any]] | list[str]:
    """Return ``value`` as a typed JSON list.

    ``reject_malformed=True`` is for write paths: malformed/double-encoded
    strings raise ``ValueError`` so raw strings are never persisted. Runtime/read
    paths use ``False`` and receive ``[]``/skipped malformed entries instead of
    crashing workflow execution.
    """

    def malformed(message: str):
        if reject_malformed:
            raise ValueError(message)
        return []

    if value is None or value == "":
        return []

    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return malformed(f"{field_name} must be a JSON array")

    if item_type == "dict" and isinstance(value, dict):
        value = [value]

    if not isinstance(value, list):
        return malformed(f"{field_name} must be a JSON array")

    normalized = []
    for index, item in enumerate(value):
        if item_type == "dict":
            if isinstance(item, str):
                try:
                    item = json.loads(item)
                except json.JSONDecodeError:
                    if reject_malformed:
                        raise ValueError(f"{field_name}[{index}] must be an object, not a string")
                    continue
            if not isinstance(item, dict):
                if reject_malformed:
                    raise ValueError(f"{field_name}[{index}] must be an object")
                continue
            normalized.append(item)
        else:
            if not isinstance(item, str):
                if reject_malformed:
                    raise ValueError(f"{field_name}[{index}] must be a string")
                continue
            normalized.append(item)

    return normalized
