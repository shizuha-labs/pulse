"""Reject a task that holds several units of work behind one status.

Work moves one unit at a time. A sitting, a one-package checklist, or a
numbered catalog of other tasks is not a task. Each unit is its own task,
assigned to the person who can do that unit.
"""

from __future__ import annotations

import re

_PACKAGE_PHRASE = re.compile(
    r"\b(?:one-package|operator sitting|assembled batch|windowed items)\b",
    re.IGNORECASE,
)
_ITEM_RANGE = re.compile(
    r"\bitems\s+\d+\s*[\u2013-]\s*\d+\b",
    re.IGNORECASE,
)
_SECTION = re.compile(r"(?m)^###\s+\d+\.\s+(.+)$")
_ITEM_KEY = re.compile(r"\b[A-Z][A-Z0-9]{1,8}-\d+\b")

_REASON = (
    "Pulse does not accept a package of tasks. Work moves one unit at a time. "
    "File one task per unit and assign it to the person who can do that unit."
)


def package_of_tasks_reason(title: str, description: str = "") -> str:
    """Return the refusal text when title+description is a package, else ''."""
    title = title or ""
    description = description or ""
    if _PACKAGE_PHRASE.search(title) or _ITEM_RANGE.search(title):
        return _REASON
    headings = _SECTION.findall(description)
    keys = set()
    for heading in headings:
        keys.update(_ITEM_KEY.findall(heading))
    if len(headings) >= 3 and len(keys) >= 3:
        return _REASON
    title_keys = _ITEM_KEY.findall(title)
    if len(set(title_keys)) >= 2 and " + " in title:
        return _REASON
    return ""
