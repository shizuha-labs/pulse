"""PLS-835 / PLS-836: read-only semantic census of typed re-admission candidates.

Mutates nothing. Prints a machine-readable table of eligible PLAT-4215 children
that are focus-hazard candidates (active queue, no active upstream blocker,
active blocked descendant) while holding a live blocks link to a still-blocked
parent, with exact originating disposition and authority presence.

Architecture freezes FROZEN_KEYS only when census_valid=true.

Usage:
  python manage.py census_pls835_waiting_external
  python manage.py census_pls835_waiting_external --format=json
"""

from __future__ import annotations

import json
import sys

from django.core.management.base import BaseCommand

from tasks.models import (
    EscalationRoutingDisposition,
    ExternalWaitAuthority,
    Item,
    ItemLink,
)
from tasks.services.actionable_items import filter_active_queue_items
from tasks.services.external_wait import HUMAN_CLASSES
from tasks.services.queue_priority import (
    active_blocked_descendant_exists,
    active_upstream_blocker_exists,
)
from tasks.services.workflow_service import WorkflowService

ACTIVE_AUTHORITY_STATES = frozenset({
    ExternalWaitAuthority.STATE_ENTER_ARMED,
    ExternalWaitAuthority.STATE_WAITING,
    ExternalWaitAuthority.STATE_EXIT_ARMED,
})


def _is_terminal(status: str) -> bool:
    slug = (status or '').lower().replace('-', '_')
    try:
        return WorkflowService._status_is_terminal(slug)
    except Exception:
        return slug in {
            'done', 'completed', 'closed', 'cancelled', 'canceled', 'resolved',
            'rejected', 'duplicate', 'wont_fix', 'merged', 'failed', 'expired',
            'deferred', 'applied', 'change_rejected',
        }


def _limb_verdict(evidence: dict) -> tuple[bool, str]:
    if not isinstance(evidence, dict):
        return False, 'evidence_not_dict'
    dep = evidence.get('dependency')
    if not isinstance(dep, dict):
        return False, 'missing_dependency'
    ec = dep.get('evidence_class')
    missing = []
    if ec not in HUMAN_CLASSES:
        missing.append(f'evidence_class={ec!r}')
    for k in ('holder_ref', 'requested_subject', 'acceptance_check'):
        if not str(dep.get(k) or '').strip():
            missing.append(k)
    if missing:
        return False, 'fail:' + ','.join(missing)
    return True, f'pass:{ec}'


def _focus_limbs(child: Item) -> dict:
    """Mirror production focus/hazard predicates (not a hard-coded status set)."""
    in_active_queue = filter_active_queue_items(
        Item.objects.filter(pk=child.pk),
    ).filter(pk=child.pk).exists()
    flags = (
        Item.objects.filter(pk=child.pk)
        .annotate(
            _has_active_upstream=active_upstream_blocker_exists(),
            _has_active_blocked_descendant=active_blocked_descendant_exists(),
        )
        .values('_has_active_upstream', '_has_active_blocked_descendant')
        .first()
    ) or {}
    has_active_upstream = bool(flags.get('_has_active_upstream'))
    has_active_blocked_descendant = bool(
        flags.get('_has_active_blocked_descendant')
    )
    is_focus_candidate = (
        in_active_queue
        and not has_active_upstream
        and has_active_blocked_descendant
    )
    return {
        'in_active_queue': in_active_queue,
        'has_active_upstream': has_active_upstream,
        'has_active_blocked_descendant': has_active_blocked_descendant,
        'is_focus_candidate': is_focus_candidate,
    }


def _authority_verdict(child: Item, disp: EscalationRoutingDisposition) -> dict:
    """Classify live authorities for this child relative to the disposition.

    Gen-3 / CoS contract:
    - no active authority → ok (may remain eligible)
    - exactly one active authority bound to this disposition (human branch,
      matching org) → already admitted (eligible=false, census still valid)
    - any other active authority shape (missing/different disposition, wrong
      branch/org, multiple active rows) → ambiguity; eligible=false and
      census_valid=false
    """
    auths = list(
        ExternalWaitAuthority.objects
        .filter(item=child, state__in=ACTIVE_AUTHORITY_STATES)
        .order_by('-wait_generation', 'pk')
    )
    child_org = getattr(child, 'organization_id', None)
    if child_org is None and getattr(disp, 'project_id', None):
        child_org = getattr(disp.project, 'organization_id', None)

    base = {
        'authority_count_active': len(auths),
        'authority_id': str(auths[0].id) if auths else None,
        'authority_state': auths[0].state if auths else None,
        'authority_branch': auths[0].branch if auths else None,
        'authority_org_id': auths[0].organization_id if auths else None,
        'authority_matches_disposition': False,
        'authority_class': 'none',
        'authority_ambiguous': False,
        'authority_ambiguity_reason': None,
    }
    if not auths:
        return base

    if len(auths) > 1:
        base.update({
            'authority_class': 'multiple_active',
            'authority_ambiguous': True,
            'authority_ambiguity_reason': (
                f'multiple_active_authorities:{len(auths)}'
            ),
            'authority_ids': [str(a.id) for a in auths],
        })
        return base

    auth = auths[0]
    ev = auth.evidence if isinstance(auth.evidence, dict) else {}
    disp_id = str(ev.get('disposition_id') or '').strip()
    expected = str(disp.id)
    reasons = []
    if not disp_id:
        reasons.append('missing_disposition_id')
    elif disp_id != expected:
        reasons.append(f'disposition_mismatch:{disp_id}')
    if auth.branch != ExternalWaitAuthority.BRANCH_HUMAN:
        reasons.append(f'branch={auth.branch!r}')
    if child_org is not None and auth.organization_id != child_org:
        reasons.append(
            f'org_mismatch:auth={auth.organization_id},item={child_org}'
        )

    if not reasons:
        base.update({
            'authority_class': 'same_disposition',
            'authority_matches_disposition': True,
        })
        return base

    base.update({
        'authority_class': 'ambiguous',
        'authority_ambiguous': True,
        'authority_ambiguity_reason': ','.join(reasons),
        'authority_evidence_disposition_id': disp_id or None,
    })
    return base


class Command(BaseCommand):
    help = (
        'PLS-835 read-only semantic census of typed waiting_external re-admission '
        'candidates (no mutations). Exit 0 only when census_valid=true.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--format',
            choices=('table', 'json'),
            default='table',
            help='Output format (default: table)',
        )

    def handle(self, *args, **options):
        rows = []
        ambiguity_rows = []
        error_rows = []
        seen_child_pks = set()
        disposition_child_pks = set()

        qs = (
            EscalationRoutingDisposition.objects
            .filter(child_item__isnull=False)
            .select_related('child_item', 'item', 'project')
            .order_by('created_at')
        )
        # Latest disposition per child wins for binding; duplicates invalidate.
        latest_by_child: dict[int, EscalationRoutingDisposition] = {}
        disp_counts: dict[int, int] = {}
        for disp in qs.iterator(chunk_size=200):
            child = disp.child_item
            if child is None:
                continue
            disp_counts[child.pk] = disp_counts.get(child.pk, 0) + 1
            latest_by_child[child.pk] = disp
            disposition_child_pks.add(child.pk)

        for child_pk, disp in latest_by_child.items():
            child = disp.child_item
            parent = disp.item
            row, amb, err = self._classify_pair(
                child, parent, disp,
                duplicate_disposition=(disp_counts.get(child_pk, 0) > 1),
            )
            if row is None:
                continue
            seen_child_pks.add(child.pk)
            rows.append(row)
            ambiguity_rows.extend(amb)
            error_rows.extend(err)

        # Tag-side inventory: PLAT-4215 task children that could be focus/hazard
        # but lack a disposition-side row → census incomplete (aoi two-sided).
        tag_side = Item.objects.filter(
            mode='task',
            resolution__isnull=True,
            is_archived=False,
        ).exclude(
            status__iexact='waiting_external',
        ).filter(tags__icontains='PLAT-4215')

        for child in tag_side.iterator(chunk_size=200):
            if child.pk in seen_child_pks:
                continue
            if _is_terminal(child.status):
                continue
            tags = {
                p.strip()
                for p in str(child.tags or '').split(',')
                if p.strip()
            }
            if 'PLAT-4215' not in tags:
                continue
            try:
                focus = _focus_limbs(child)
            except Exception as exc:  # pragma: no cover
                error_rows.append({
                    'child_key': child.item_key or f'#{child.pk}',
                    'disposition_id': None,
                    'error': f'tag_side_focus:{exc}',
                    'side': 'tag',
                })
                continue
            # Only flag focus/hazard band orphans — not every historical tag.
            if not focus.get('is_focus_candidate'):
                continue
            ambiguity_rows.append({
                'child_key': child.item_key or f'#{child.pk}',
                'disposition_id': None,
                'authority_class': 'missing_originating_disposition',
                'authority_ambiguity_reason': (
                    'tag_side_focus_child_without_disposition_binding'
                ),
                'side': 'tag',
            })
            rows.append({
                'eligible': False,
                'side': 'tag',
                'child_key': child.item_key or f'#{child.pk}',
                'child_status': child.status,
                'child_pk': child.pk,
                'child_mode': child.mode,
                'child_assignee_email': child.assignee_email or '',
                'child_workflow_name': child.workflow_name or '',
                'disposition_id': None,
                'disposition_generation': None,
                'disposition_created_at': None,
                'limb_verdict': 'missing_disposition',
                'ineligible_reasons': [
                    'missing_originating_disposition',
                    'tag_side_orphan',
                ],
                'in_active_queue': focus.get('in_active_queue'),
                'has_active_upstream': focus.get('has_active_upstream'),
                'has_active_blocked_descendant': focus.get(
                    'has_active_blocked_descendant'
                ),
                'is_focus_candidate': focus.get('is_focus_candidate'),
                'authority_id': None,
                'authority_state': None,
                'authority_branch': None,
                'authority_org_id': None,
                'authority_count_active': None,
                'authority_matches_disposition': False,
                'authority_class': 'missing_originating_disposition',
                'authority_ambiguous': True,
                'authority_ambiguity_reason': (
                    'tag_side_focus_child_without_disposition_binding'
                ),
                'blocks_link_id': None,
                'parent_key': None,
                'parent_status': None,
                'parent_pk': None,
                'classification': None,
                'outcome': None,
            })

        # Duplicate eligible keys (same child, multiple eligible rows) invalidate.
        eligible_keys = sorted({
            r['child_key'] for r in rows if r['eligible']
        })
        key_counts: dict[str, int] = {}
        for r in rows:
            if r['eligible']:
                key_counts[r['child_key']] = key_counts.get(r['child_key'], 0) + 1
        for k, n in key_counts.items():
            if n > 1:
                ambiguity_rows.append({
                    'child_key': k,
                    'disposition_id': None,
                    'authority_class': 'duplicate_eligible_rows',
                    'authority_ambiguity_reason': f'eligible_row_count={n}',
                })

        census_valid = not ambiguity_rows and not error_rows
        payload = {
            'schema': 'shizuha.pulse.pls835-semantic-census.v3',
            'mutable': False,
            'census_valid': census_valid,
            'eligible_key_count': len(eligible_keys) if census_valid else 0,
            'eligible_keys': eligible_keys if census_valid else [],
            'eligible_keys_unvalidated': eligible_keys,
            'row_count': len(rows),
            'ambiguity_row_count': len(ambiguity_rows),
            'error_row_count': len(error_rows),
            'ambiguity_rows': ambiguity_rows,
            'error_rows': error_rows,
            'rows': rows,
            'population': {
                'definition': (
                    'state-defined: non-terminal task-mode PLAT-4215 focus/hazard '
                    'children not in typed waiting, with exact disposition binding; '
                    'not merge-time'
                ),
                'two_sided': True,
            },
            'focus_predicate': {
                'in_active_queue': 'filter_active_queue_items',
                'has_active_upstream': 'active_upstream_blocker_exists == false',
                'has_active_blocked_descendant': (
                    'active_blocked_descendant_exists == true'
                ),
            },
            'admission_gates': [
                'mode=task',
                'assignee_email non-empty',
                'PLAT-4215 tag',
                'classification=operator_held_dependency',
                'outcome=routed',
                'unique disposition binding',
                'human limbs',
                'blocks→blocked parent',
                'focus candidate',
                'authority uniqueness/binding',
            ],
        }

        if options['format'] == 'json':
            self.stdout.write(json.dumps(payload, indent=2, sort_keys=True))
        else:
            self.stdout.write(
                f"PLS-835 semantic census (read-only) census_valid={census_valid} "
                f"eligible_keys={payload['eligible_keys']!r} "
                f"n_eligible={payload['eligible_key_count']} "
                f"n_rows={len(rows)} n_ambiguity={len(ambiguity_rows)} "
                f"n_error={len(error_rows)}"
            )
            self.stdout.write(
                "child_key\tchild_status\tmode\tassignee\tfocus\tupstream\t"
                "descendant\tdisposition_id\tclass/outcome\tauthority_class\t"
                "parent_key\teligible\treasons"
            )
            for r in rows:
                self.stdout.write(
                    f"{r['child_key']}\t{r['child_status']}\t"
                    f"{r.get('child_mode')}\t{r.get('child_assignee_email') or '-'}\t"
                    f"{r['is_focus_candidate']}\t{r['has_active_upstream']}\t"
                    f"{r['has_active_blocked_descendant']}\t"
                    f"{r['disposition_id'] or '-'}\t"
                    f"{r.get('classification')}/{r.get('outcome')}\t"
                    f"{r['authority_class']}\t{r.get('parent_key') or '-'}\t"
                    f"{r['eligible']}\t"
                    f"{','.join(r['ineligible_reasons']) or '-'}"
                )
            if ambiguity_rows:
                self.stdout.write(
                    "AMBIGUITY_ROWS=" + json.dumps(ambiguity_rows, sort_keys=True)
                )
            self.stdout.write(
                "FREEZE_CANDIDATE_KEYS="
                + (",".join(payload['eligible_keys']) if census_valid else "")
            )
            if not census_valid:
                self.stdout.write(
                    "CENSUS_INVALID — do not freeze; resolve ambiguity/error rows"
                )

        if not census_valid:
            sys.exit(2)

    def _classify_pair(
        self,
        child: Item,
        parent: Item | None,
        disp: EscalationRoutingDisposition,
        *,
        duplicate_disposition: bool,
    ):
        ambiguity_rows = []
        error_rows = []
        if child is None or parent is None:
            return None, ambiguity_rows, error_rows
        if _is_terminal(child.status) or child.resolution_id:
            return None, ambiguity_rows, error_rows
        if (child.status or '').lower().replace('-', '_') == 'waiting_external':
            return None, ambiguity_rows, error_rows

        # Link first — needed to detect parent/link graph contradictions (rui P2).
        links = list(
            ItemLink.objects.filter(
                source_item=child,
                target_item=parent,
                link_type='blocks',
            ).only('id')
        )
        if not links:
            return None, ambiguity_rows, error_rows
        if len(links) > 1:
            ambiguity_rows.append({
                'child_key': child.item_key or f'#{child.pk}',
                'disposition_id': str(disp.id),
                'authority_class': 'duplicate_blocks_links',
                'authority_ambiguity_reason': f'blocks_link_count={len(links)}',
            })
        link = links[0]

        parent_slug = (parent.status or '').lower().replace('-', '_')
        parent_terminal = _is_terminal(parent.status)
        # Terminal parent + live link: relationship exhausted — skip (benign).
        if parent_terminal:
            return None, ambiguity_rows, error_rows
        # Non-terminal parent not blocked while block-holding link lives:
        # inconsistent graph — fail-closed (rui P2 / wiki v15+).
        if parent_slug != 'blocked':
            ambiguity_rows.append({
                'child_key': child.item_key or f'#{child.pk}',
                'disposition_id': str(disp.id),
                'authority_class': 'parent_link_graph_contradiction',
                'authority_ambiguity_reason': (
                    f'parent_status={parent.status!r}_nonterminal_with_live_blocks_link'
                ),
                'parent_key': parent.item_key or f'#{parent.pk}',
                'blocks_link_id': str(link.id),
            })
            row = {
                'eligible': False,
                'side': 'disposition',
                'child_key': child.item_key or f'#{child.pk}',
                'child_status': child.status,
                'child_pk': child.pk,
                'child_mode': child.mode,
                'child_assignee_email': child.assignee_email or '',
                'child_workflow_name': child.workflow_name or '',
                'disposition_id': str(disp.id),
                'disposition_generation': disp.generation,
                'disposition_created_at': (
                    disp.created_at.isoformat() if disp.created_at else None
                ),
                'limb_verdict': 'parent_link_graph_contradiction',
                'ineligible_reasons': [
                    f'parent_status={parent.status!r}_with_live_blocks_link',
                ],
                'in_active_queue': None,
                'has_active_upstream': None,
                'has_active_blocked_descendant': None,
                'is_focus_candidate': False,
                'authority_id': None,
                'authority_state': None,
                'authority_branch': None,
                'authority_org_id': None,
                'authority_count_active': None,
                'authority_matches_disposition': False,
                'authority_class': 'parent_link_graph_contradiction',
                'authority_ambiguous': True,
                'authority_ambiguity_reason': (
                    f'parent_status={parent.status!r}_nonterminal_with_live_blocks_link'
                ),
                'blocks_link_id': str(link.id),
                'parent_key': parent.item_key or f'#{parent.pk}',
                'parent_status': parent.status,
                'parent_pk': parent.pk,
                'classification': disp.classification,
                'outcome': disp.outcome,
            }
            return row, ambiguity_rows, error_rows

        reasons = []
        if duplicate_disposition:
            reasons.append('duplicate_originating_dispositions')
            ambiguity_rows.append({
                'child_key': child.item_key or f'#{child.pk}',
                'disposition_id': str(disp.id),
                'authority_class': 'duplicate_dispositions',
                'authority_ambiguity_reason': 'multiple_dispositions_for_child',
            })

        # Admission gates (mirror admit_human_escalation positive path)
        if (child.mode or '') != 'task':
            reasons.append(f'mode={child.mode!r}')
        if not str(child.assignee_email or '').strip():
            reasons.append('missing_assignee_email')
        if disp.classification != 'operator_held_dependency':
            reasons.append(f'classification={disp.classification!r}')
        if disp.outcome != 'routed':
            reasons.append(f'outcome={disp.outcome!r}')

        ok, limb = _limb_verdict(disp.normalized_evidence or {})
        if not ok:
            reasons.append(limb)

        tags = {
            p.strip()
            for p in str(getattr(child, 'tags', '') or '').split(',')
            if p.strip()
        }
        if 'PLAT-4215' not in tags:
            reasons.append('missing_PLAT-4215_tag')

        try:
            focus = _focus_limbs(child)
        except Exception as exc:  # pragma: no cover
            focus = {
                'in_active_queue': False,
                'has_active_upstream': None,
                'has_active_blocked_descendant': None,
                'is_focus_candidate': False,
            }
            reasons.append(f'focus_predicate_error:{exc.__class__.__name__}')
            error_rows.append({
                'child_key': child.item_key or f'#{child.pk}',
                'disposition_id': str(disp.id),
                'error': str(exc),
            })

        if not focus.get('is_focus_candidate'):
            reasons.append('not_focus_candidate')

        try:
            auth_v = _authority_verdict(child, disp)
        except Exception as exc:  # pragma: no cover
            auth_v = {
                'authority_count_active': None,
                'authority_id': None,
                'authority_state': None,
                'authority_branch': None,
                'authority_org_id': None,
                'authority_matches_disposition': False,
                'authority_class': 'error',
                'authority_ambiguous': True,
                'authority_ambiguity_reason': f'authority_error:{exc}',
            }
            error_rows.append({
                'child_key': child.item_key or f'#{child.pk}',
                'disposition_id': str(disp.id),
                'error': str(exc),
            })

        if auth_v.get('authority_class') == 'same_disposition':
            reasons.append('authority_present_same_disposition')
        if auth_v.get('authority_ambiguous'):
            reasons.append(
                'authority_ambiguous:'
                + str(auth_v.get('authority_ambiguity_reason') or 'unknown')
            )
            ambiguity_rows.append({
                'child_key': child.item_key or f'#{child.pk}',
                'disposition_id': str(disp.id),
                'authority_class': auth_v.get('authority_class'),
                'authority_ambiguity_reason': auth_v.get(
                    'authority_ambiguity_reason'
                ),
                'authority_id': auth_v.get('authority_id'),
                'authority_ids': auth_v.get('authority_ids'),
            })

        # Instrument contradictions invalidate the whole census (rui P1):
        # malformed classification/outcome/human limbs must not silently shrink
        # the freeze set — census_valid=false + empty eligible_keys.
        instrument_reasons = [
            r for r in reasons
            if r.startswith('classification=')
            or r.startswith('outcome=')
            or r.startswith('fail:')
            or r in ('evidence_not_dict', 'missing_dependency')
        ]
        if instrument_reasons:
            ambiguity_rows.append({
                'child_key': child.item_key or f'#{child.pk}',
                'disposition_id': str(disp.id),
                'authority_class': 'instrument_contradiction',
                'authority_ambiguity_reason': ','.join(instrument_reasons),
            })

        # Unassigned / non-task that would otherwise look freezeable still
        # invalidates the whole census (zen): never freeze beside a silent no-op.
        hard_invalid = any(
            r.startswith('mode=') or r == 'missing_assignee_email'
            for r in reasons
        ) and focus.get('is_focus_candidate')
        if hard_invalid:
            ambiguity_rows.append({
                'child_key': child.item_key or f'#{child.pk}',
                'disposition_id': str(disp.id),
                'authority_class': 'admission_precondition_failed',
                'authority_ambiguity_reason': ','.join(
                    r for r in reasons
                    if r.startswith('mode=') or r == 'missing_assignee_email'
                ),
            })

        eligible = not reasons
        row = {
            'eligible': eligible,
            'side': 'disposition',
            'child_key': child.item_key or f'#{child.pk}',
            'child_status': child.status,
            'child_pk': child.pk,
            'child_mode': child.mode,
            'child_assignee_email': child.assignee_email or '',
            'child_workflow_name': child.workflow_name or '',
            'disposition_id': str(disp.id),
            'disposition_generation': disp.generation,
            'disposition_created_at': (
                disp.created_at.isoformat() if disp.created_at else None
            ),
            'limb_verdict': limb,
            'ineligible_reasons': reasons,
            'in_active_queue': focus.get('in_active_queue'),
            'has_active_upstream': focus.get('has_active_upstream'),
            'has_active_blocked_descendant': focus.get(
                'has_active_blocked_descendant'
            ),
            'is_focus_candidate': focus.get('is_focus_candidate'),
            'authority_id': auth_v.get('authority_id'),
            'authority_state': auth_v.get('authority_state'),
            'authority_branch': auth_v.get('authority_branch'),
            'authority_org_id': auth_v.get('authority_org_id'),
            'authority_count_active': auth_v.get('authority_count_active'),
            'authority_matches_disposition': auth_v.get(
                'authority_matches_disposition'
            ),
            'authority_class': auth_v.get('authority_class'),
            'authority_ambiguous': bool(auth_v.get('authority_ambiguous')),
            'authority_ambiguity_reason': auth_v.get(
                'authority_ambiguity_reason'
            ),
            'blocks_link_id': str(link.id),
            'parent_key': parent.item_key or f'#{parent.pk}',
            'parent_status': parent.status,
            'parent_pk': parent.pk,
            'classification': disp.classification,
            'outcome': disp.outcome,
        }
        return row, ambiguity_rows, error_rows
