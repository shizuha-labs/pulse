"""Drain group-queued actionable tasks to concrete team members.

Operator 2026-07-11: "ensure pulse assigns tasks to underlying team members."
The membership rebalancer (sweep_rebalance_pods) moves PROJECT MEMBERSHIPS, not
tasks — nothing pushed group-queued (assignment_group set, assignee NULL)
actionable tasks onto concrete agents, so work piled in team queues while
members sat idle and in-progress tasks orphaned (orphan-safety violation).

This sweep resolves each queued actionable task to a concrete least-loaded
available member via WorkflowService._resolve_team_assignee (the SAME resolver
task-create/transition use — capability-aware, org-aware, tier-failover). Tasks
whose team has no capable available member are LOGGED as capability/routing
mismatches (not force-assigned to an agent that cannot execute them).

Dry-run by default; --apply to write. Kill-switch PULSE_QUEUE_ASSIGN_ENABLED.
"""
from __future__ import annotations

import os

from django.core.management.base import BaseCommand
from django.db.models import Q

from tasks.models import Item
from tasks.services.workflow_service import WorkflowService

# Same statuses as sweep_assign_group_queued. awaiting_merge and
# awaiting_deploy are stage work for the team that owns them.
ACTIONABLE = (
    'open', 'todo', 'doing', 'needs_triage', 'in_progress', 'in_review',
    'awaiting_deploy', 'awaiting_merge',
)
# Working states MUST have a concrete owner (orphan-safety invariant) — these
# are assigned first even if the team is at WIP cap.
WORKING = ('in_progress', 'in_review')


class Command(BaseCommand):
    help = 'Assign group-queued actionable tasks to concrete team members (operator 2026-07-11).'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Write assignments (default: dry-run).')
        parser.add_argument('--group', default='', help='Limit to one assignment_group.')
        parser.add_argument('--limit', type=int, default=0, help='Max assignments this run.')

    def handle(self, *args, **opts):
        if os.environ.get('PULSE_QUEUE_ASSIGN_ENABLED', '1') != '1':
            self.stdout.write('PULSE_QUEUE_ASSIGN_ENABLED=0 — skipping')
            return
        apply = bool(opts['apply'])
        limit = opts['limit'] or 0

        qs = (
            Item.objects.filter(status__in=ACTIONABLE, mode='task')
            .exclude(assignment_group__isnull=True).exclude(assignment_group='')
            .filter(Q(assignee_email__isnull=True) | Q(assignee_email=''))
            .filter(Q(assignee_id__isnull=True))
        )
        if opts['group']:
            qs = qs.filter(assignment_group=opts['group'])
        # Working-state orphans first (orphan-safety), then the rest.
        queued = sorted(qs, key=lambda i: (0 if i.status in WORKING else 1, str(i.item_key)))
        # PLAT-9027: never re-bind stale stage-state corpses onto the stage
        # seat. An awaiting_merge/awaiting_deploy item unassigned past the
        # freshness window is a dropped-merge-event candidate (PLAT-9010
        # class), not live queue work — re-binding it here is what flooded
        # the merge seat with the July/August graveyard (2026-09-17 wave).
        # Surface them for reconciliation instead; replay the missing
        # supported transition idempotently, never skip to terminal.
        from tasks.tasks import _is_stale_stage_candidate, _stage_assign_cutoff

        stage_cutoff = _stage_assign_cutoff()
        stale_stage = [i for i in queued if _is_stale_stage_candidate(i, stage_cutoff)]
        if stale_stage:
            self.stdout.write(self.style.WARNING(
                f'RECONCILER-MISS CANDIDATES excluded from re-bind: {len(stale_stage)} '
                '(stage-state unassigned past freshness window — replay the '
                'dropped merge/deploy event; do not re-seat, do not skip to terminal)'))
            for item in stale_stage[:40]:
                self.stdout.write(
                    f'   {item.item_key} [{item.status}] group={item.assignment_group} '
                    f'stage_idle_since={item.updated_at}')
            stale_pks = {i.pk for i in stale_stage}
            queued = [i for i in queued if i.pk not in stale_pks]

        assigned = mismatched = 0
        mismatch_keys = []
        for item in queued:
            if limit and assigned >= limit:
                break
            group = item.assignment_group
            uid, email = WorkflowService._resolve_team_assignee(
                group, item=item, project_key=getattr(item.project, 'key', None) if item.project_id else None)
            if not email:
                mismatched += 1
                mismatch_keys.append((item.item_key, group, item.status))
                continue
            if not apply:
                self.stdout.write(f'[dry-run] {item.item_key} [{item.status}] {group} -> {email}')
                assigned += 1
                continue
            item.assignee_id = uid
            item.assignee_email = email
            item.save(update_fields=['assignee_id', 'assignee_email', 'updated_at'])
            assigned += 1
            self.stdout.write(self.style.SUCCESS(f'{item.item_key} [{item.status}] {group} -> {email}'))

        mode = 'APPLY' if apply else 'DRY-RUN'
        self.stdout.write(f'{mode}: assigned={assigned} capability_mismatch={mismatched} of {len(queued)} queued')
        if mismatch_keys:
            self.stdout.write(
                'CAPABILITY/ROUTING MISMATCH (no capable available member — re-route to the right team):')
            for k, g, s in mismatch_keys[:40]:
                self.stdout.write(f'   {k} [{s}] group={g}')
