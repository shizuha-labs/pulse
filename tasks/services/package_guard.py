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


def _line_of(description: str, text: str) -> int | None:
    needle = text.strip()
    if not needle or not description:
        return None
    for index, line in enumerate(description.splitlines(), 1):
        if needle in line:
            return index
    return None


def _with_units(units, description: str = "") -> str:
    """Name the spans the gate actually parsed. Do not invent extra units."""
    shown = []
    for index, unit in enumerate(units, 1):
        text = " ".join(str(unit).split())
        if len(text) > 120:
            text = text[:117] + "..."
        line = _line_of(description, text)
        label = f"sentence {line}" if line else str(index)
        shown.append(f"{label} '{text}'")
    return (
        f"{_REASON} Parsed {len(shown)} units: " + "; ".join(shown) + "."
    )


def package_of_tasks_reason(title: str, description: str = "") -> str:
    """Return the refusal text when title+description is a package, else ''."""
    title = title or ""
    description = description or ""
    phrase = _PACKAGE_PHRASE.search(title)
    item_range = _ITEM_RANGE.search(title)
    if phrase or item_range:
        units = []
        if phrase:
            units.append(phrase.group(0))
        if item_range:
            units.append(item_range.group(0))
        return _with_units(units, title)
    headings = _SECTION.findall(description)
    keys = set()
    for heading in headings:
        keys.update(_ITEM_KEY.findall(heading))
    if len(headings) >= 3 and len(keys) >= 3:
        return _with_units(headings, description)
    title_keys = _ITEM_KEY.findall(title)
    unique_keys = list(dict.fromkeys(title_keys))
    if len(unique_keys) >= 2 and " + " in title:
        return _with_units(unique_keys, title)
    return ""
