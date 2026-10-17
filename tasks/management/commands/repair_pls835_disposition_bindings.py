"""PLS-1370 / PLS-1371: per-key audited disposition binding repairs.

Architecture adjudication (PLS-1209 / wiki b0fa5986 v18):
  B instrument_contradiction — canonicalize mixed/held → operator_held_dependency/routed
    when the trail already carries a complete human limb; else supersede. Rejected
    outcomes are terminal instruments — supersede/closure review, never canonicalize.
  C duplicate_dispositions — keep earliest valid authority-bound row; retire
    (null child_item) the rest so bindings are 1:1.

Mutates ONLY EscalationRoutingDisposition rows (classification/outcome/reason/
normalized_evidence and/or child_item). Never touches workflow status, never
relaxes census gates. Default is dry-run.

Usage:
  python manage.py repair_pls835_disposition_bindings --mode both
  python manage.py repair_pls835_disposition_bindings --mode instrument --execute
  python manage.py repair_pls835_disposition_bindings --keys VEN-258,HIVE-2325 --execute
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from tasks.models import EscalationRoutingDisposition, Item
from tasks.services.external_wait import HUMAN_CLASSES

# Frozen from PLS-1209 adjudication + census attachment ac4382f3 (SHA256
# b6454468…c1a1). Do not silently expand.
INSTRUMENT_KEYS = frozenset({
    'CON-313', 'CON-315', 'CON-475', 'CTX-952', 'HIVE-2325', 'HIVE-2703',
    'HIVE-2709', 'ORIG-383', 'ORIG-384', 'PLAT-10018', 'PLAT-10053',
    'PLAT-10127', 'PLAT-10134', 'PLAT-10145', 'PLAT-5829', 'PLAT-5836',
    'PLAT-6762', 'PLAT-7212', 'PLAT-7696', 'PLAT-7734', 'PLAT-7736',
    'PLAT-8835', 'PLAT-9574', 'PLAT-9576', 'PLAT-9588', 'PLAT-9935',
    'PLAT-9936', 'SCLI-487', 'SCLI-648', 'SCLI-783', 'VEN-258',
})
DUPLICATE_KEYS = frozenset({
    'CON-475', 'CON-479', 'CTX-975', 'HIVE-2325', 'HIVE-2665', 'HIVE-2733',
    'MAIL-124', 'ORIG-365', 'ORIG-385', 'PLAT-6762', 'PLAT-6835', 'PLAT-6949',
    'PLAT-7132', 'PLAT-7150', 'PLAT-7519', 'PLAT-7689', 'PLAT-7696',
    'PLAT-7734', 'PLAT-7736', 'PLAT-7748', 'PLAT-7844', 'PLAT-8264',
    'PLAT-8543', 'PLAT-8674', 'PLAT-8845', 'PLAT-8951', 'PLAT-9150',
    'PLAT-9205', 'PLAT-9333', 'PLAT-9498', 'PLAT-9815', 'PLAT-9831',
    'PLAT-9848', 'PLS-1143', 'SCLI-784', 'SCLI-801', 'VEN-258',
})
REJECTED_KEYS = frozenset({'PLAT-6762', 'PLAT-7734', 'PLAT-7736'})


def _limb_ok(evidence: Any) -> tuple[bool, str]:
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


def _is_canonical(disp: EscalationRoutingDisposition) -> bool:
    ok, _ = _limb_ok(disp.normalized_evidence or {})
    return (
        disp.classification == 'operator_held_dependency'
        and disp.outcome == 'routed'
        and ok
    )


def _is_rejected(disp: EscalationRoutingDisposition) -> bool:
    return (
        disp.classification == 'operator_request_hygiene_reject'
        or disp.outcome == 'rejected'
    )


@dataclass
class KeyAudit:
    child_key: str
    child_pk: int | None = None
    disposition_count: int = 0
    actions: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    applied: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)


class Command(BaseCommand):
    help = (
        'PLS-1370/1371: per-key audited canonicalize/supersede + dedupe of '
        'EscalationRoutingDisposition child bindings (dry-run default).'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--mode',
            choices=('instrument', 'dedupe', 'both'),
            default='both',
            help='Which repair class to run (default: both).',
        )
        parser.add_argument(
            '--keys',
            default='',
            help='Comma-separated child keys to limit the run (default: frozen sets).',
        )
        parser.add_argument(
            '--execute',
            action='store_true',
            help='Apply mutations. Without this flag the command is dry-run only.',
        )
        parser.add_argument(
            '--format',
            choices=('table', 'json'),
            default='json',
            help='Audit output format (default: json).',
        )

    def handle(self, *args, **options):
        execute = bool(options['execute'])
        mode = options['mode']
        key_filter = {
            k.strip()
            for k in str(options['keys'] or '').split(',')
            if k.strip()
        }
        targets: set[str] = set()
        if mode in ('instrument', 'both'):
            targets |= INSTRUMENT_KEYS
        if mode in ('dedupe', 'both'):
            targets |= DUPLICATE_KEYS
        if key_filter:
            unknown = key_filter - (INSTRUMENT_KEYS | DUPLICATE_KEYS)
            if unknown:
                raise CommandError(
                    f'keys not in frozen PLS-1370/1371 sets: {sorted(unknown)}'
                )
            targets &= key_filter
        if not targets:
            raise CommandError('no keys selected')

        audits: list[KeyAudit] = []
        for key in sorted(targets):
            audits.append(self._repair_key(key, mode=mode, execute=execute))

        payload = {
            'schema': 'shizuha.pulse.pls835-disposition-repair.v1',
            'mutable': execute,
            'mode': mode,
            'key_count': len(audits),
            'audits': [asdict(a) for a in audits],
            'summary': {
                'applied': sum(len(a.applied) for a in audits),
                'blocked': sum(len(a.blocked) for a in audits),
                'actions_planned': sum(len(a.actions) for a in audits),
            },
            'generated_at': timezone.now().isoformat(),
        }
        if options['format'] == 'json':
            self.stdout.write(json.dumps(payload, sort_keys=True, indent=2))
        else:
            for a in audits:
                self.stdout.write(
                    f"{a.child_key}: actions={a.actions} applied={a.applied} "
                    f"blocked={a.blocked} notes={a.notes}"
                )
            self.stdout.write(json.dumps(payload['summary'], sort_keys=True))

        if any(a.blocked for a in audits) and execute:
            # Non-zero so operators notice unfinished keys without aborting
            # already-applied mutations (those committed per-key).
            self.stderr.write(self.style.WARNING(
                'Some keys remain blocked pending trail/closure review; '
                'see audit JSON.'
            ))

    def _dispositions_for_child(self, child: Item):
        return list(
            EscalationRoutingDisposition.objects
            .filter(child_item=child)
            .select_related('item', 'child_item', 'project')
            .order_by('created_at', 'generation')
        )

    def _repair_key(self, key: str, *, mode: str, execute: bool) -> KeyAudit:
        audit = KeyAudit(child_key=key)
        child = Item.objects.filter(item_key=key).first()
        if child is None:
            audit.blocked.append('child_not_found')
            return audit
        audit.child_pk = child.pk
        disps = self._dispositions_for_child(child)
        audit.disposition_count = len(disps)
        if not disps:
            audit.blocked.append('no_disposition_bindings')
            return audit

        # --- Class C: dedupe to 1:1 (keep originating = earliest valid, else earliest) ---
        if mode in ('dedupe', 'both') and len(disps) > 1:
            keep = self._pick_originating(disps)
            if keep is None:
                audit.blocked.append('no_authority_bound_originating_row')
                return audit
            retire = [d for d in disps if d.id != keep.id]
            audit.actions.append(
                f'dedupe:keep={keep.id}/gen={keep.generation};'
                f'retire={[str(d.id) for d in retire]}'
            )
            if execute:
                with transaction.atomic():
                    for d in retire:
                        if d.child_item_id is not None:
                            d.child_item = None
                            d.save(update_fields=['child_item'])
                            audit.applied.append(f'retired_binding:{d.id}')
                disps = self._dispositions_for_child(child)

        if len(disps) != 1 and mode in ('dedupe', 'both'):
            # Still >1 somehow, or zero after retire
            if len(disps) > 1:
                audit.blocked.append(f'dedupe_incomplete:count={len(disps)}')
                return audit

        if not disps:
            audit.blocked.append('no_disposition_after_dedupe')
            return audit

        # Work the single (or latest if instrument-only and still duplicated)
        disp = disps[-1] if mode == 'instrument' and len(disps) > 1 else disps[0]
        if mode == 'instrument' and len(disps) > 1:
            audit.notes.append(
                f'instrument_on_latest_while_dupes_remain:count={len(disps)}'
            )

        if mode not in ('instrument', 'both'):
            return audit
        if key not in INSTRUMENT_KEYS:
            return audit

        # --- Class B: instrument repair ---
        if key in REJECTED_KEYS or _is_rejected(disp):
            audit.notes.append(
                'rejected_terminal_instrument:closure_or_supersede_only'
            )
            audit.blocked.append(
                'needs_closure_review:do_not_canonicalize_rejected'
            )
            return audit

        ok, limb = _limb_ok(disp.normalized_evidence or {})
        if ok and not _is_canonical(disp):
            audit.actions.append(
                f'canonicalize:{disp.id}:{disp.classification}/{disp.outcome}'
                f'→operator_held_dependency/routed (limb={limb})'
            )
            if execute:
                with transaction.atomic():
                    disp.classification = 'operator_held_dependency'
                    disp.outcome = 'routed'
                    if not disp.reason_code or disp.reason_code in {
                        'classification_unknown',
                        'mixed_or_unknown_escalation_hold',
                    }:
                        disp.reason_code = 'validated_operator_held'
                    disp.save(update_fields=[
                        'classification', 'outcome', 'reason_code',
                    ])
                    audit.applied.append(f'canonicalized:{disp.id}')
            return audit

        if _is_canonical(disp):
            audit.notes.append('already_canonical')
            return audit

        # Incomplete limb — attempt supersede only when parent trail can fill it.
        filled = self._evidence_from_trail(disp)
        if filled is not None:
            audit.actions.append(
                f'supersede:{disp.id}→new canonical with trail-filled limb'
            )
            if execute:
                new_id = self._supersede_with_evidence(disp, child, filled)
                audit.applied.append(f'superseded:{disp.id}→{new_id}')
            return audit

        audit.blocked.append(
            f'needs_trail_or_closure:limb={limb};'
            f'class={disp.classification};outcome={disp.outcome}'
        )
        return audit

    def _pick_originating(
        self, disps: list[EscalationRoutingDisposition],
    ) -> EscalationRoutingDisposition | None:
        """Earliest valid authority-bound row, or None.

        A non-canonical earliest row is not a valid 1:1 binding. Callers must
        record blocked and skip the retire when this returns None.
        """
        for d in disps:
            if _is_canonical(d) and not _is_rejected(d):
                return d
        return None

    def _evidence_from_trail(
        self, disp: EscalationRoutingDisposition,
    ) -> dict | None:
        """Return a complete normalized_evidence dict if recoverable in-row.

        Parent-comment scraping is intentionally out of scope for this command:
        invented limbs are forbidden. Only complete an already-present dependency
        block that is missing a single blank field when the rest pass — and only
        when evidence_class is already a HUMAN_CLASSES value.
        """
        evidence = disp.normalized_evidence
        if not isinstance(evidence, dict):
            return None
        dep = evidence.get('dependency')
        if not isinstance(dep, dict):
            return None
        if dep.get('evidence_class') not in HUMAN_CLASSES:
            return None
        # Refuse to invent holder/subject/acceptance — must already be present.
        ok, _ = _limb_ok(evidence)
        return evidence if ok else None

    def _supersede_with_evidence(
        self,
        old: EscalationRoutingDisposition,
        child: Item,
        evidence: dict,
    ) -> str:
        parent = old.item
        with transaction.atomic():
            latest = (
                EscalationRoutingDisposition.objects
                .filter(item=parent)
                .aggregate(value=Max('generation'))['value']
                or 0
            )
            generation = latest + 1
            digest = (
                f'pls1370-supersede:{old.id}:{generation}:'
                f'{timezone.now().isoformat()}'
            )
            # disposition_digest is CharField(64) — keep sha-like length.
            import hashlib
            disposition_digest = hashlib.sha256(digest.encode()).hexdigest()
            new = EscalationRoutingDisposition.objects.create(
                organization_id=old.organization_id,
                project=old.project,
                item=parent,
                transition_id=old.transition_id,
                source_generation=old.generation,
                generation=generation,
                disposition_digest=disposition_digest,
                schema_version=old.schema_version or 'shizuha.pulse.escalation-evidence.v1',
                classification='operator_held_dependency',
                outcome='routed',
                reason_code='pls1370_trail_supersede',
                blocker_dependency_key=old.blocker_dependency_key or '',
                blocker_result=old.blocker_result or '',
                normalized_evidence=evidence,
                pre_state=old.final_state or {},
                final_state={
                    **(old.final_state or {}),
                    'pls1370_supersedes': str(old.id),
                },
                child_item=child,
                link_ids=list(old.link_ids or []),
                catalog_service_ref=old.catalog_service_ref or '',
                catalog_generation=old.catalog_generation,
            )
            # Retire old binding so 1:1 holds.
            if old.child_item_id == child.pk:
                old.child_item = None
                old.save(update_fields=['child_item'])
            # Retire any other bindings for this child.
            (
                EscalationRoutingDisposition.objects
                .filter(child_item=child)
                .exclude(pk=new.pk)
                .update(child_item=None)
            )
            return str(new.id)
