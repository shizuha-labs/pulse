"""PLAT-1209: derive QA risk labels for tasks at creation time.

The QA Operating Model routes focused QA by a small risk-label set, the daily
high-risk sweep (PLAT-1147/#6559) matches completed tasks against it, and
PLS-184 auto-creates focused QA tasks from it. All three are blind when
authors never apply the labels (the 2026-07-02 audit matched 0 of 32 tasks on
a day with an auth-outage RCA, agent-runtime daemon changes, and a Books
rollout). This module derives the labels deterministically from title +
description so qualifying tasks carry them without relying on author habit.

Design constraints:
- Additive only: derived labels are merged into ``Item.tags`` (the field
  ``focused_qa.risk_labels_for_item`` tokenizes) and never remove or replace
  author-provided labels/tags.
- Deterministic + conservative: plain word-boundary regexes, no scoring, so a
  match is explainable and false positives stay reviewable.
- ``requires-live-qa`` is intentionally NOT auto-applied — it expresses author
  intent; the domain labels below are what the sweep/PLS-184 key on.
"""
from __future__ import annotations

import re

# Label -> patterns matched case-insensitively against title + description.
RISK_LABEL_PATTERNS: dict[str, list[str]] = {
    'auth': [
        r'\bauth(?:n|z|entication|orization)?\b',
        r'\blog[- ]?in\b|\blogin\b',
        r'\boauth\b', r'\bjwt\b', r'\bjwks\b', r'\brs256\b', r'\bsso\b',
        r'\btoken\s*review\b', r'\bcredential', r'\baccess[- ]token\b',
    ],
    'security-sensitive': [
        r'\bsecurity\b', r'\bvulnerab', r'\bcve-\d{4}', r'\bexploit',
        r'\brbac\b', r'\bsecrets?\b', r'\bpermission', r'\bprivilege',
        r'\bdata[- ]leak', r'\bzip[- ]?bomb\b', r'\binjection\b',
    ],
    'data-migration': [
        r'\bmigrations?\b', r'\bmigrate\b', r'\bbackfill',
        r'\bschema\s+(?:change|migration)\b', r'\bdata[- ]?loss\b',
    ],
    'payments-books': [
        r'\bpayments?\b', r'\bbilling\b', r'\binvoice', r'\bvouchers?\b',
        r'\bgst\b', r'\btds\b', r'\btcs\b', r'\bledgers?\b', r'\bbooks\b',
        r'\bfiscal\b',
    ],
    'agent-runtime': [
        r'\bagent[- ]runtime\b', r'\brt-fleet\b', r'\bfleet[- ]daemon\b',
        r'\bruntime[- ]fleet\b', r'\bmcp-auth-proxy\b', r'\bclaude-bridge\b',
        r'\bcodex\b', r'\bagent container', r'\bheartbeat', r'\bbridge\b',
    ],
    'infra-control-plane': [
        r'\bk3s\b', r'\bkubernetes\b', r'\bkubectl\b', r'\bhelm\b',
        r'\bingress\b', r'\bnginx\b', r'\bcoredns\b', r'\bprometheus\b',
        r'\balertmanager\b', r'\bcontrol[- ]plane\b', r'\blonghorn\b',
        r'\bnodeselector\b', r'\bcordon', r'\bnamespace\b',
    ],
    'user-facing': [
        r'\bui\b', r'\bux\b', r'\bfrontend\b', r'\bdashboards?\b',
        r'\bbrowser\b', r'\buser[- ]facing\b', r'\bmobile\b', r'\bwebview\b',
        r'\brenders?(?:ed|ing)?\b',
    ],
}

_COMPILED = {
    label: re.compile('|'.join(patterns), re.IGNORECASE)
    for label, patterns in RISK_LABEL_PATTERNS.items()
}


def derive_risk_labels(*texts: str | None) -> set[str]:
    """Return the risk labels whose patterns match any of the given texts."""
    corpus = ' '.join(t for t in texts if t)
    if not corpus:
        return set()
    return {label for label, rx in _COMPILED.items() if rx.search(corpus)}


def merge_into_tags(tags: str | None, labels: set[str]) -> str | None:
    """Append missing labels to a comma-separated tags string (additive)."""
    parts = [t.strip() for t in (tags or '').split(',') if t.strip()]
    seen = {p.lower() for p in parts}
    parts.extend(label for label in sorted(labels) if label.lower() not in seen)
    return ','.join(parts) if parts else tags
