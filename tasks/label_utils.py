"""Canonical dual-shape task-label helpers (PLS-712).

Task labels exist in two supported JSON shapes: flat string arrays and
key/value objects.  Keep parsing and ORM containment predicates here so a new
consumer cannot accidentally support only one half of the persisted corpus.
"""
from __future__ import annotations

import json
from collections.abc import Mapping

from django.db.models import Q


LABEL_FILTER_HELP = (
    "labels must be a comma-separated label string, a JSON array of strings, "
    "or a JSON object with string key/value pairs."
)


class InvalidLabelFilter(ValueError):
    """The public labels filter could not be interpreted without widening."""


def _flat_label(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    label = value.strip()
    return label or None


def canonical_label_set(labels: object) -> frozenset[str]:
    """Return the stable flat-token view of an array/object label value.

    ``["repo:shizuha-labs/drive"]`` and
    ``{"repo": "shizuha-labs/drive"}`` intentionally normalize to the same
    token.  Invalid legacy members are ignored at this tolerant storage
    boundary; the public filter parser below is strict and rejects them.
    """
    normalized: set[str] = set()
    if isinstance(labels, Mapping):
        for raw_key, raw_value in labels.items():
            key = _flat_label(raw_key)
            value = _flat_label(raw_value)
            if key is not None and value is not None:
                normalized.add(f"{key}:{value}")
    elif isinstance(labels, (list, tuple, set, frozenset)):
        for raw_label in labels:
            label = _flat_label(raw_label)
            if label is not None:
                normalized.add(label)
    else:
        label = _flat_label(labels)
        if label is not None:
            normalized.add(label)
    return frozenset(normalized)


def parse_label_filter(raw: object) -> frozenset[str]:
    """Parse the fail-loud public filter contract into canonical tokens."""
    if not isinstance(raw, str) or not raw.strip():
        raise InvalidLabelFilter(LABEL_FILTER_HELP)

    value = raw.strip()
    if value.startswith(("[", "{")):
        try:
            decoded = json.loads(value)
        except (json.JSONDecodeError, TypeError) as exc:
            raise InvalidLabelFilter(LABEL_FILTER_HELP) from exc
        if not isinstance(decoded, (list, dict)):
            raise InvalidLabelFilter(LABEL_FILTER_HELP)
        if isinstance(decoded, list):
            if not decoded or any(_flat_label(part) is None for part in decoded):
                raise InvalidLabelFilter(LABEL_FILTER_HELP)
        else:
            if not decoded or any(
                _flat_label(key) is None or _flat_label(part) is None
                for key, part in decoded.items()
            ):
                raise InvalidLabelFilter(LABEL_FILTER_HELP)
        tokens = canonical_label_set(decoded)
    else:
        parts = value.split(",")
        if not parts or any(_flat_label(part) is None for part in parts):
            raise InvalidLabelFilter(LABEL_FILTER_HELP)
        tokens = canonical_label_set(parts)

    if not tokens:
        raise InvalidLabelFilter(LABEL_FILTER_HELP)
    return tokens


def item_labels_match_q(labels: object) -> Q:
    """AND canonical tokens; OR the array/object containment arms per token."""
    tokens = canonical_label_set(labels)
    if not tokens:
        return Q(pk__in=[])

    predicate = Q()
    for label in sorted(tokens):
        token_predicate = Q(labels__contains=[label])
        if ":" in label:
            key, value = label.split(":", 1)
            if key and value:
                token_predicate |= Q(labels__contains={key: value})
        predicate &= token_predicate
    return predicate
