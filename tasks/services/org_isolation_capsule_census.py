"""PLS-842 / BKSD-14 §7.1 — Gen-1 dry-run census of 0106 org-safe capsules.

Read-only. Enumerates every capsule matching the three-arm 0106 selector and
every outbound ``blocks`` edge from those capsules. Emits structured tuples +
digests so Architecture can freeze dispositions before any Gen-2 mutation.

Does NOT unlink edges or change status. Does NOT expose foreign org payloads
beyond the redacted legacy key already stored on the local capsule metadata.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from django.db.models import Q

from ..models import Item, ItemLink


# Same three-arm OR as 0106_backfill_org_safe_condition_capsules.py:58-64.
CAPSULE_SELECTOR = (
    Q(source='org_isolation_migration')
    | Q(metadata__org_isolation_replacement=True)
    | Q(title__startswith='[Org-local dependency]')
)

# Heuristic limbs for dry-run classification (Architecture freezes the final
# verdict). A capsule is "externally unverifiable" when its verification
# criteria require foreign-org evidence and no local evidence path is named.
_FOREIGN_MARKERS = (
    'foreign',
    'cross-org',
    'cross organization',
    'another organization',
    'legacy source',
    'do not re-link',
    'org-foreign',
)


def _stable_digest(payload: dict[str, Any]) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(',', ':'), default=str)
    return hashlib.sha256(blob.encode('utf-8')).hexdigest()


def _criteria_text(value: Any) -> str:
    """Deterministically normalize 0106 verification_criteria to redacted text.

    The real 0106 capsule stores ``verification_criteria`` as a JSON **list** of
    checklist strings (see ``_org_safe_condition_capsule_v1._verification_lines``),
    while some handwritten capsules use a plain string. ``_classify_disposition``
    calls ``.strip()`` on the value, so a list crashed live with
    ``AttributeError: 'list' object has no attribute 'strip'`` (PLS-916 / ichi
    2026-08-09). Normalize both shapes here, once, before digest + classify.
    """
    if value is None:
        return ''
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        parts = [str(v) for v in value if str(v).strip()]
        return ' '.join(parts)
    return str(value)


def _capsule_payload(item: Item) -> dict[str, Any]:
    meta = dict(item.metadata or {})
    structured = meta.get('org_safe_condition_capsule') or {}
    return {
        'item_id': item.id,
        'item_key': item.item_key,
        'title': item.title,
        'status': item.status,
        'org_id': getattr(item.project, 'org_id', None) if item.project_id else None,
        'project_id': item.project_id,
        'source': item.source or '',
        'assignment_group': item.assignment_group or '',
        'assignee_email': item.assignee_email or '',
        'former_external_item_key': meta.get('former_external_item_key') or '',
        # Never emit former_external_item_id as a live foreign pointer in the
        # public census body — keep only the redacted key + a boolean.
        'has_former_external_item_id': bool(meta.get('former_external_item_id')),
        'org_isolation_replacement': bool(meta.get('org_isolation_replacement')),
        'capsule_version': structured.get('version'),
        'condition_summary': (structured.get('condition_summary') or '')[:500],
        'verification_criteria': _criteria_text(
            structured.get('verification_criteria')
        )[:500],
        'description_has_capsule_heading': (
            '## Org-safe condition capsule' in str(item.description or '')
        ),
    }


def _classify_disposition(payload: dict[str, Any]) -> str:
    """Provisional limb for dry-run only — Architecture freezes the final set.

    Returns ``locally_verifiable`` or ``external_unverifiable_locally``.
    """
    text = ' '.join([
        str(payload.get('condition_summary') or ''),
        str(payload.get('verification_criteria') or ''),
        str(payload.get('title') or ''),
    ]).lower()
    if any(marker in text for marker in _FOREIGN_MARKERS):
        return 'external_unverifiable_locally'
    # Capsules whose former key is set but verification criteria empty after
    # redaction are treated as external until proven otherwise.
    if payload.get('former_external_item_key') and not (
        payload.get('verification_criteria') or ''
    ).strip():
        return 'external_unverifiable_locally'
    if payload.get('capsule_version') is None and not payload.get(
        'description_has_capsule_heading'
    ):
        return 'external_unverifiable_locally'
    return 'locally_verifiable'


def _edge_tuple(link: ItemLink, capsule: Item) -> dict[str, Any]:
    target = link.target_item
    same_org = (
        capsule.project_id
        and target is not None
        and target.project_id
        and getattr(capsule.project, 'org_id', None)
        == getattr(target.project, 'org_id', None)
    )
    return {
        'link_id': str(link.id),
        'capsule_key': capsule.item_key,
        'capsule_id': capsule.id,
        'dependent_key': target.item_key if target else None,
        'dependent_id': target.id if target else None,
        'dependent_status': target.status if target else None,
        'dependent_title': (target.title if target else '')[:120],
        'link_type': link.link_type,
        'same_org': bool(same_org),
        # Provisional: retain only when same-org; Architecture freezes.
        'provisional_decision': 'retain_candidate' if same_org else 'reject',
    }


def run_census(*, include_terminal: bool = True) -> dict[str, Any]:
    """Return a structured Gen-1 census. Never writes."""
    qs = (
        Item.objects.filter(CAPSULE_SELECTOR)
        .select_related('project')
        .order_by('id')
    )
    capsules = []
    edges = []
    for item in qs.iterator():
        payload = _capsule_payload(item)
        disposition = _classify_disposition(payload)
        capsule_digest = _stable_digest({
            'item_key': payload['item_key'],
            'former_external_item_key': payload['former_external_item_key'],
            'condition_summary': payload['condition_summary'],
            'verification_criteria': payload['verification_criteria'],
            'capsule_version': payload['capsule_version'],
        })
        capsules.append({
            **payload,
            'provisional_disposition': disposition,
            'capsule_digest': capsule_digest,
        })
        # Outbound blocks edges only (the harm class in PLS-842).
        for link in (
            ItemLink.objects.filter(source_item=item, link_type='blocks')
            .select_related('target_item', 'target_item__project')
            .order_by('id')
        ):
            edge = _edge_tuple(link, item)
            edge['capsule_digest'] = capsule_digest
            edge['edge_digest'] = _stable_digest(edge)
            edges.append(edge)

    report = {
        'schema': 'pls842.org_isolation_capsule_census.v1',
        'mode': 'dry_run',
        'census_valid': True,
        'selector': [
            "source='org_isolation_migration'",
            'metadata.org_isolation_replacement=true',
            "title startswith '[Org-local dependency]'",
        ],
        'capsule_count': len(capsules),
        'edge_count': len(edges),
        'capsules': capsules,
        'edges': edges,
        # Known frozen historical reject (acceptance 7).
        'must_remain_absent': [
            {'capsule_key': 'PLAT-4813', 'dependent_key': 'PLAT-4720', 'link_type': 'blocks'},
        ],
    }
    report['report_digest'] = _stable_digest({
        'capsule_count': report['capsule_count'],
        'edge_count': report['edge_count'],
        'capsule_digests': [c['capsule_digest'] for c in capsules],
        'edge_digests': [e['edge_digest'] for e in edges],
    })
    # Verify PLAT-4813→PLAT-4720 absence in the live edge set.
    absent_ok = not any(
        e.get('capsule_key') == 'PLAT-4813'
        and e.get('dependent_key') == 'PLAT-4720'
        and e.get('link_type') == 'blocks'
        for e in edges
    )
    report['plat4813_plat4720_absent'] = absent_ok
    if not absent_ok:
        report['census_valid'] = False
    return report
