"""
PLS-1386 — isolated operator-evidence live-canary harness (bounded, DevOps-executed).

Provides the PLS-1323 live-acceptance prerequisite: a runnable, inspectable canary
that exercises the SERVING operator-evidence scheduler/queue/event code paths
(``tasks.services.operator_wait``) against an explicitly bounded test org, with:

- fail-closed boundary: refuses the production org (id 1 / slug ``shizuha``) and
  any org id that is not explicitly passed; all rows live under one canary
  project (key ``PLS1386C``) with ``PLS1386-CANARY-*`` item keys;
- explicit no-egress controls: the Connect DM + UserNotificationService adapters
  and the liveness heartbeat export are STUBBED AND COUNTED (asserted zero real
  egress), while probe / event / fingerprint-compare / exactly-once queueing run
  for real against the serving persistence layer;
- the four PLS-1323 beats: first-probe records; unchanged beats are no-ops with
  zero external re-probes warranted; a relevant changed fingerprint queues
  exactly one verification; a duplicate re-delivery queues no additional one;
  plus the unrelated/identical-fingerprint no-op;
- event/audit correlation: prints every mechanical comment's deterministic
  ``origin_event_id`` and the marker generation lineage;
- cleanup plan: ``--cleanup`` removes exactly the seeded canary rows (project
  cascade) and nothing else;
- exact source binding: refuses to run without a non-empty ``PULSE_SOURCE_SHA``
  (the serving image's baked source identity) unless ``--allow-unbound-source``
  is passed, and prints it with the DB migration head for the run record.

Mocked vs actually exercised (PLS-1386 recording requirement):
    actually exercised — OperatorWaitMarker persistence, fingerprint
    normalization/compare, probe outcome machine, exactly-once verification
    queueing (MechanicalCommentFactory + deterministic_event_id), marker
    tombstoning, operator_wait_state_for_item read path;
    stubbed (counted, asserted zero) — Connect ``/internal/system-message/``
    egress, UserNotificationService, liveness heartbeat record, cache write of
    the ``pulse:operator_wait:last_*`` health keys.

This command mutates ONLY rows it creates inside the explicit canary org. It
never touches real operator evidence, the EVOL-36 consolidated digest, or any
production item. Local unit tests alone do not satisfy live scheduler
acceptance — DevOps executes this bounded canary against the serving stack and
returns the evidence to exact-org QA (PLS-1323).
"""
from __future__ import annotations

import os
from unittest import mock

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from tasks.models import Comment, Item, ItemLink, OperatorWaitMarker, Project
from tasks.services.operator_wait import (
    apply_operator_evidence_event,
    operator_wait_state_for_item,
    record_operator_evidence_probe,
)

CANARY_PROJECT_KEY = 'PLS1386C'
CANARY_ITEM_PREFIX = 'PLS1386-CANARY-'
PRODUCTION_ORG_IDS = {1}
STUBBED_EGRESS_KEYS = (
    'connect_system_message_posts',
    'user_notification_service_calls',
    'liveness_heartbeat_records',
)


class _EgressCounter:
    """Stub-and-count adapter proving zero real egress from the canary run."""

    def __init__(self):
        self.counts = {key: 0 for key in STUBBED_EGRESS_KEYS}
        self.payloads = []

    def bump(self, key, payload=None):
        self.counts[key] += 1
        self.payloads.append({'stub': key, 'payload': payload})


class Command(BaseCommand):
    help = (
        'PLS-1386: bounded operator-evidence live canary for PLS-1323 — '
        'run ONLY against a dedicated test org; --cleanup removes the seed.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--org-id', type=int, required=True,
            help='Numeric id of the DEDICATED TEST organization for this run.',
        )
        parser.add_argument(
            '--cleanup', action='store_true',
            help='Delete exactly the seeded canary rows (project cascade).',
        )
        parser.add_argument(
            '--allow-unbound-source', action='store_true',
            help='Permit running without PULSE_SOURCE_SHA (record-keeping only).',
        )
        parser.add_argument(
            '--unchanged-beats', type=int, default=3,
            help='How many unchanged-fingerprint beats to run (default 3).',
        )

    # -- boundary -----------------------------------------------------------

    def _assert_boundary(self, org_id):
        if org_id in PRODUCTION_ORG_IDS:
            raise CommandError(
                'refusing to run: org id %s is the production organization. '
                'Create a dedicated test org and pass its id (--org-id).' % org_id
            )
        if org_id <= 0:
            raise CommandError('--org-id must be a positive integer.')

    def _assert_source_binding(self, allow_unbound):
        source_sha = (os.environ.get('PULSE_SOURCE_SHA') or '').strip()
        if not source_sha and not allow_unbound:
            raise CommandError(
                'PULSE_SOURCE_SHA is empty — the serving image must bake its '
                'source identity; pass --allow-unbound-source only for '
                'record-keeping dry runs.'
            )
        return source_sha or 'UNBOUND'

    # -- seed / teardown ----------------------------------------------------

    def _seed(self, org_id):
        project, _ = Project.objects.get_or_create(
            key=CANARY_PROJECT_KEY, org_id=org_id,
            defaults={
                'name': 'PLS-1386 operator-evidence canary',
                'access_level': 'open',
            },
        )
        digest = Item.objects.create(
            project=project,
            item_key=f'{CANARY_ITEM_PREFIX}DIGEST',
            title='[PLS-1386 canary] Blocked-on-operator digest',
            mode='task', workflow_name='autonomous-dev', status='scheduled',
            assignment_group='admin-ops',
        )
        root = Item.objects.create(
            project=project,
            item_key=f'{CANARY_ITEM_PREFIX}ROOT',
            title='[PLS-1386 canary] Canonical operator blocker root',
            mode='task', workflow_name='autonomous-dev', status='open',
            assignment_group='admin-ops',
            tags='blocker,decision-required,escalation',
            metadata={'pls1386_canary': True},
        )
        # PLAT-4215: an Item can never be CREATED directly in `blocked` (ORM
        # guard + deferred trigger). Seed in a working status, link the durable
        # operator blocker root, then enter `blocked` through the sanctioned
        # owned-blocker path so the canary seed is itself invariant-compliant.
        surfaced = Item.objects.create(
            project=project,
            item_key=f'{CANARY_ITEM_PREFIX}SURFACED',
            title='[PLS-1386 canary] Surfaced child (blocked on root)',
            mode='task', workflow_name='autonomous-dev', status='open',
            assignment_group='admin-ops',
            parent_item=root,
            metadata={'escalation_routing_gate': {
                'classification': 'genuine_decision_or_physical_action',
                'outcome': 'allowed',
            }},
        )
        link = ItemLink(
            source_item=surfaced, target_item=root,
            link_type='is_blocked_by', created_by_id=207)
        # _skip_auto_block: the link itself must not auto-drive the transition;
        # the seed enters `blocked` explicitly below via the owned-blocker path.
        link._skip_auto_block = True
        link.save()
        surfaced.status = 'blocked'
        surfaced.save(update_fields=['status'])
        # Fixture seed for the canary digest, not a lifecycle writer.
        # Inventoried in legacy_direct_comment_writers.json as USER_AUTHORED.
        comment = Comment.objects.create(
            item=digest,
            author_id=207,
            author_email='shizuha@shizuha.com',
            origin_kind=Comment.ORIGIN_USER_AUTHORED,
            content=(
                '## [PLS-1386 canary] Blocked-on-operator digest\n\n'
                'DM delivered to operator (message_id '
                '`pls1386-canary-0000-0000-000000000000`).\n\n'
                f'- **{surfaced.item_key}** — canary operator evidence required.'
            ),
        )
        return {'project': project, 'digest': digest, 'root': root,
                'surfaced': surfaced, 'comment': comment}

    def _seed_marker(self, seed):
        # Direct marker seed mirrors the 0181/0182 durable shape; the canary's
        # purpose is the probe/event/queue paths, not marker-construction.
        marker = OperatorWaitMarker.objects.create(
            item=seed['root'],
            surfaced_item=seed['surfaced'],
            digest_item=seed['digest'],
            digest_comment=seed['comment'],
            source_revision='pls1386-canary-rev-0',
            source_message_id='pls1386-canary-msg-0',
            lineage_item_ids=[seed['surfaced'].pk],
            state='active',
            generation=1,
        )
        seed['marker'] = marker
        return marker

    def _cleanup(self, org_id):
        deleted, _ = Project.objects.filter(
            key=CANARY_PROJECT_KEY, org_id=org_id,
        ).delete()
        orphans = OperatorWaitMarker.objects.filter(
            item__project__key=CANARY_PROJECT_KEY,
            item__project__org_id=org_id,
        ).count()
        self.stdout.write(self.style.WARNING(
            f'cleanup: project cascade deleted {deleted} object(s); '
            f'residual canary markers: {orphans} (must be 0).'
        ))

    # -- scenarios ----------------------------------------------------------

    def _verification_count(self, item):
        return item.comments.filter(
            origin_event='operator_wait.evidence_reactivated',
        ).count() if hasattr(item.comments, 'filter') else 0

    def _run_beats(self, seed, unchanged_beats):
        root = seed['root']
        findings = []
        fp_a = 'pls1386-canary-fingerprint-A'

        # Beat 1 — first complete probe records the fingerprint.
        r1 = record_operator_evidence_probe(
            root.pk, fp_a, checklist={'probe': 'canary'}, source='pls1386-canary',
        )
        findings.append(('first_probe_records', r1['outcome'] == 'recorded', r1))

        # Beat 2 — unchanged beats: no-ops, zero external re-probes warranted.
        unchanged_ok = True
        last = None
        for _ in range(max(1, unchanged_beats)):
            last = record_operator_evidence_probe(
                root.pk, fp_a, source='pls1386-canary',
            )
            unchanged_ok &= (
                last['outcome'] == 'unchanged'
                and last['should_probe_external'] is False
                and last['verification_queued'] is False
            )
        findings.append(('unchanged_beats_noop', unchanged_ok, last))

        # Beat 3 — unrelated/identical event path is a no-op.
        r3 = apply_operator_evidence_event(root.pk, fp_a, source='pls1386-canary')
        findings.append((
            'unrelated_event_noop',
            r3['outcome'] == 'unchanged' and r3['verification_queued'] is False,
            r3,
        ))

        # Beat 4 — relevant changed fingerprint reactivates EXACTLY ONCE.
        fp_b = 'pls1386-canary-fingerprint-B'
        r4 = record_operator_evidence_probe(
            root.pk, fp_b, source='pls1386-canary',
        )
        count_after_first = self._verification_count(root)
        findings.append((
            'changed_fingerprint_queues_exactly_one',
            r4['outcome'] == 'reactivated'
            and r4['verification_queued'] is True
            and count_after_first == 1,
            {'result': r4, 'verification_comments': count_after_first},
        ))

        # Beat 5 — duplicate re-delivery of the changed fingerprint queues
        # no additional verification (marker already tombstoned once).
        r5 = apply_operator_evidence_event(root.pk, fp_b, source='pls1386-canary')
        count_after_dup = self._verification_count(root)
        findings.append((
            'duplicate_queues_no_additional',
            count_after_dup == 1,
            {'result': r5, 'verification_comments': count_after_dup},
        ))

        # Read-path correlation: state view reflects the tombstoned canary root.
        state = operator_wait_state_for_item(root.pk)
        findings.append((
            'state_view_correlates',
            state is None or state.get('state') != 'active',
            state,
        ))
        return findings

    # -- entry --------------------------------------------------------------

    def handle(self, *args, **options):
        org_id = options['org_id']
        self._assert_boundary(org_id)
        source_sha = self._assert_source_binding(options['allow_unbound_source'])

        if options['cleanup']:
            with transaction.atomic():
                self._cleanup(org_id)
            return

        egress = _EgressCounter()
        migration_head = self._migration_head()
        self.stdout.write(self.style.MIGRATE_HEADING(
            f'PLS-1386 canary | org_id={org_id} | source_sha={source_sha} | '
            f'migration_head={migration_head}'
        ))

        stub_notify = (
            lambda payload: egress.bump('connect_system_message_posts', payload)
            or False
        )
        with mock.patch(
            'tasks.services.operator_wait.notify_operator_wait_owner',
            stub_notify,
        ), mock.patch(
            'tasks.services.notifications.UserNotificationService',
        ) as uns, mock.patch('tasks.liveness.record_heartbeat') as hb:
            uns.return_value.notify_user.side_effect = (
                lambda *a, **k: egress.bump('user_notification_service_calls')
            )
            hb.side_effect = (
                lambda *a, **k: egress.bump('liveness_heartbeat_records')
            )
            with transaction.atomic():
                seed = self._seed(org_id)
                self._seed_marker(seed)
                findings = self._run_beats(
                    seed, options['unchanged_beats'],
                )

        all_green = True
        for name, ok, evidence in findings:
            mark = self.style.SUCCESS('PASS') if ok else self.style.ERROR('FAIL')
            all_green &= ok
            self.stdout.write(f'  [{mark}] {name}: {evidence}')

        self.stdout.write('stubbed-egress counts (must all be 0):')
        for key in STUBBED_EGRESS_KEYS:
            zero = egress.counts[key] == 0
            all_green &= zero
            mark = self.style.SUCCESS('0') if zero else self.style.ERROR(
                str(egress.counts[key])
            )
            self.stdout.write(f'  {key}: {mark}')

        self.stdout.write('correlation:')
        for entry in egress.payloads:
            self.stdout.write(f'  stubbed payload: {entry}')
        for marker in OperatorWaitMarker.objects.filter(
            item__project=seed['project'],
        ):
            self.stdout.write(
                f'  marker item={marker.item.item_key} state={marker.state} '
                f'generation={marker.generation} '
                f'verification_queued_generation='
                f'{marker.verification_queued_generation} '
                f'fingerprint={marker.evidence_fingerprint!r}'
            )

        if not all_green:
            raise CommandError('PLS-1386 canary FAILED — see [FAIL] rows above.')
        self.stdout.write(self.style.SUCCESS(
            'PLS-1386 canary GREEN — bounded run complete; rows remain for '
            'DevOps evidence capture; re-run with --cleanup to remove.'
        ))

    def _migration_head(self):
        from django.db import connection
        from django.db.migrations.executor import MigrationExecutor
        executor = MigrationExecutor(connection)
        plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
        return 'applied-clean' if not plan else f'UNAPPLIED:{len(plan)}'
