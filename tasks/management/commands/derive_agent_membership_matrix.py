"""
EVOL-20 Ph0b — bootstrap the agent×project membership matrix + shadow report.

Derives each agent's project memberships from two inputs (HLD §6, page ab8fc292 v2):
  1. The PLS-8 ``_PROJECT_STAGE_LEADS`` map → ``primary`` + ``is_lead`` memberships
     (the operator's stage-lead tiering carries over verbatim).
  2. Last-N-day ``ItemActivity`` actor×project counts (the PLS-13 derivation
     source) → each agent's busiest project = ``primary``, next = ``secondary``.

Default is **REPORT-ONLY** (the *shadow report* — derive the matrix and print what
affinity WOULD change vs. today, writing nothing; zero behaviour change). ``--apply``
seeds the memberships via the audited ``agent_membership_service`` (≤4-active cap
enforced); the rows are INERT until Ph1 reads them. The matrix is CoS-reviewed
before Ph1 enables [SIGNED §6/§11].

Usage:
    python manage.py derive_agent_membership_matrix            # shadow report
    python manage.py derive_agent_membership_matrix --json
    python manage.py derive_agent_membership_matrix --apply    # seed (inert)
"""
import json
import logging
from collections import defaultdict
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand
from django.db.models import Count, Max, Q
from django.utils import timezone

from tasks.models import AgentProjectMembership, ItemActivity, Project
from tasks.services import agent_membership_service as svc
from tasks.services.workflow_service import WorkflowService

logger = logging.getLogger(__name__)

_MAX_ACTIVE = AgentProjectMembership.MAX_ACTIVE_PER_AGENT  # SIGNED cap = 4

# Provenance actor for derived rows. MUST be a valid email: create_membership /
# update_membership run full_clean(), which validates created_by_email — a bare
# label like 'derive_agent_membership_matrix' fails EmailField validation and
# aborts the would-add path with a ValidationError. The human-readable derivation
# reason is carried in `reason`; this only identifies the actor.
_ACTOR_EMAIL = 'derive-agent-membership-matrix@agents.shizuha.io'

_AGENTS_SUFFIX = '@agents.shizuha.io'
_CANON_SUFFIX = '@shizuha.com'


def _canonical_email(email):
    """Canonical membership email for an agent (Codex P2).

    Agents act internally under ``@agents.shizuha.io`` but their Shizuha-ID /
    stage-lead / membership identity is the matching ``@shizuha.com`` address
    (user_service convention). Fold the internal alias onto the canonical
    address so the same agent under both emails is ONE identity — otherwise
    activity under the alias derives a duplicate desired row AND dodges the
    email-keyed cap (the alias row isn't counted against the @shizuha.com rows).
    """
    e = (email or '').strip().lower()
    if e.endswith(_AGENTS_SUFFIX):
        e = e[: -len(_AGENTS_SUFFIX)] + _CANON_SUFFIX
    return e


def _known_agent_emails():
    """Canonical emails of all real agents, from the WorkflowService rosters.

    Domain-suffix matching (Codex P2) wrongly treated EVERY ``@shizuha.com``
    actor — humans, service accounts — as an agent, polluting the inert matrix
    whenever such an account touched items. The authoritative agent set is the
    DB-backed pod roster (plus the pinned stage leads, already a subset).

    PLAT-431: read the roster from the DB via ``_pod_roster()`` — the hardcoded
    ``_POD_ROSTER`` was removed, so iterating the (now-empty) code constant would
    yield an empty allow-list and silently un-gate the matrix. ``_pod_roster()``
    is 30s-cached, so this single read per command run is cheap.
    """
    # PLAT-431 (cycle 5): _pod_roster() returns ALL TeamRoster rows, including
    # operator/human-only teams (admin-ops, human-ops) that the old _POD_ROSTER
    # deliberately excluded — their human emails are NOT agents and must not get
    # activity-matrix rows. Skip non-agent teams by canonical name (structural,
    # not agent-specific, so it respects the no-agent-emails-in-code rule).
    _NON_AGENT_TEAMS = {'admin-ops', 'human-ops', 'ceo-decisions', 'leads'}
    roster = set()
    for team, val in WorkflowService._pod_roster().items():
        if WorkflowService._canonical_team_name(team) in _NON_AGENT_TEAMS:
            continue
        roster.update(_canonical_email(e) for e in WorkflowService._flat_roster(val))
    # PLAT-431 (cycle 2): the former second source — WorkflowService._PROJECT_STAGE_LEADS
    # — was emptied when per-project lead emails were removed from code, so iterating it
    # contributed nothing. Dropped the dead loop. Pinned project leads, if re-introduced,
    # will live in the DB; any real lead is already captured by the roster loop above.
    return roster


def _agent_available_for_membership(email):
    """Return (available, skip_reason) for derived membership safety.

    EVOL-38/PLAT-1225: stopped/disabled/unavailable agents must not become
    active affinity targets just because historical activity or sticky lead rows
    mention them. Reuse the assignment resolver's authoritative availability
    gate (operator disabled set, durable AgentAvailability rows, and live daemon
    health), so the shadow matrix and --apply see the same runtime truth as
    routing. Unknown availability fails closed: this command can seed routing
    rows, so a resolver/read error must never silently permit writes.
    """
    try:
        available = bool(WorkflowService._is_agent_available_for_assignment(email))
        return available, '' if available else 'unavailable'
    except Exception:
        logger.warning(
            'derive_agent_membership_matrix: availability check failed for %s; '
            'suppressing candidate this pass',
            email, exc_info=True,
        )
        return False, 'availability_unknown'


class Command(BaseCommand):
    help = (
        "EVOL-20 Ph0b: derive the agent×project membership matrix + shadow report. "
        "Report-only by default (zero behaviour change); --apply seeds inert rows."
    )

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Seed the derived memberships (default: report-only shadow).')
        parser.add_argument('--days', type=int, default=30,
                            help='ItemActivity lookback window in days (default 30).')
        parser.add_argument('--org', type=int, default=None,
                            help='Scope to a single organization id. Project.key is NOT '
                                 'globally unique (the (org,key) constraint was removed), so '
                                 'pass --org to disambiguate when a key exists in >1 org.')
        parser.add_argument('--json', action='store_true', dest='as_json',
                            help='Machine-readable output.')

    def handle(self, *args, **opts):
        days = opts['days']
        apply = opts['apply']
        org_id = opts.get('org')
        cutoff = timezone.now() - timedelta(days=days)

        # Project.key is not globally unique — the (organization, key) uniqueness
        # constraint was removed (Codex P2). Keying by bare key collapses two orgs'
        # same-keyed projects to one arbitrary row, applying memberships to the wrong
        # project. Scope to --org when given; otherwise detect cross-org key collisions
        # and DROP the ambiguous keys (recorded in `ambiguous`) rather than guess.
        # Exclude ARCHIVED and INACTIVE projects (Codex P2): ProjectViewSet.archive
        # flips is_archived but leaves is_active set; separately a project can be
        # is_active=False without being archived. The normal project list + permission
        # resolver only expose is_active=True projects, so either would otherwise get
        # active memberships seeded on --apply for a project nobody can reach.
        pqs = Project.objects.filter(is_archived=False, is_active=True)
        if org_id is not None:
            pqs = pqs.filter(org_id=org_id)
        projects: dict = {}
        ambiguous: set = set()
        for p in pqs:
            if not p.key:
                continue
            k = p.key.upper()
            if k in projects and projects[k].org_id != p.org_id:
                ambiguous.add(k)
            projects[k] = p
        for k in ambiguous:
            projects.pop(k, None)  # ambiguous across orgs and undisambiguated → skip

        # Canonical agent roster (Codex P2): only real agents get derived rows.
        known_agents = _known_agent_emails()

        # desired[email] = {PKEY: {tier, is_lead, reason, user_id}}; keyed by the
        # CANONICAL agent email so an agent's @agents.shizuha.io alias and its
        # @shizuha.com identity collapse to one row-set (Codex P2).
        desired: dict = defaultdict(dict)

        # ── (1) PLS-8 stage-leads → primary + is_lead ────────────────────────
        # Stage-lead rows are recorded FIRST (and `desired` is insertion-ordered),
        # so they claim the SIGNED cap's slots ahead of activity-derived rows
        # (leads outrank activity by design). The cap itself is NOT applied here:
        # it is enforced per-row in the apply/report stage below against the
        # agent's FULL existing active set — not just this run's rows — so the
        # shadow report equals what --apply actually seeds (Codex P2).
        # PLAT-431 (cycle 4): leads come from the DB ``ProjectTeamLead`` table
        # (sticky, lazily-elected per (project, team)), NOT the removed code-level
        # ``_PROJECT_STAGE_LEADS`` map (now empty — iterating it seeded nothing and
        # risked silently demoting existing is_lead rows). Read the active rows.
        try:
            from tasks.models import ProjectTeamLead
            # PLAT-431 (cycle 6): deterministic order — an agent can be lead for
            # several (project, team) pairs, and desired[email][project] is keyed by
            # project, so without a stable order the last-processed team's lead
            # reason for a given project would vary run-to-run with DB row order.
            lead_rows = list(
                ProjectTeamLead.objects.filter(is_active=True).order_by('project_key', 'team_name')
            )
        except Exception:
            # PLAT-431 (cycle 6/7): a transient DB error / missing table must not
            # SILENTLY drop all project leads. In --apply we're about to PERSIST
            # memberships, so an empty lead set would demote elected is_lead rows —
            # ABORT loudly rather than write a degraded result (:181). In the
            # default report-only mode there's nothing to corrupt, so log + degrade.
            logger.warning('derive_agent_membership_matrix: ProjectTeamLead query failed; '
                           '%s', 'aborting (--apply)' if apply else 'running with NO project leads this pass',
                           exc_info=True)
            if apply:
                raise
            lead_rows = []
        for row in lead_rows:
            pkey_u = str(row.project_key).strip().upper()
            if pkey_u not in projects:
                continue
            le = _canonical_email(row.lead_email)
            if le not in known_agents:
                continue
            desired[le][pkey_u] = {
                'tier': 'primary', 'is_lead': True, 'user_id': row.lead_id,
                'reason': f'DB ProjectTeamLead ({row.team_name}) for {pkey_u}',
            }

        # ── (2) last-N-day ItemActivity actor×project → primary/secondary ────
        # Scope activity to the TARGET projects only (Codex P2): filtering the Project
        # map alone left this query aggregating EVERY org's activity, so an --org A run
        # counted org B's same-keyed activity. item__project_id__in fixes it at source.
        target_proj_ids = [p.id for p in projects.values()]
        # Aggregate by canonical agent EMAIL, not (email, user_id) (Codex P2): the same
        # email with multiple user_ids (identity repair / stale rows) splits the counts
        # and can demote the true busiest project. The DB can't fold an agent's
        # @agents.shizuha.io alias onto its @shizuha.com identity, so re-merge the
        # per-(email, project) counts by CANONICAL email in Python (Codex P2) before
        # ranking, summing the alias + canonical counts.
        rows = (ItemActivity.objects
                .filter(created_at__gte=cutoff, item__project_id__in=target_proj_ids)
                .values('user_email', 'item__project__key')
                .annotate(n=Count('id')))
        merged: dict = defaultdict(int)  # (canon_email, PKEY) -> summed count
        for r in rows:
            email = _canonical_email(r['user_email'])
            pkey = (r['item__project__key'] or '').upper()
            if email not in known_agents or pkey not in projects:
                continue
            merged[(email, pkey)] += r['n']
        # Representative user_id per CANONICAL agent (arbitrary-but-stable; the email
        # is the canonical identity, the FK is best-effort — fold aliases via Max).
        email_uid: dict = {}
        for r in (ItemActivity.objects
                  .filter(created_at__gte=cutoff, item__project_id__in=target_proj_ids,
                          user_id__isnull=False)
                  .values('user_email').annotate(uid=Max('user_id'))):
            ce = _canonical_email(r['user_email'])
            if r['uid'] is not None and ce in known_agents:
                email_uid[ce] = max(email_uid.get(ce, 0), r['uid'])
        by_agent: dict = defaultdict(list)  # canon_email -> [(PKEY, count)]
        for (email, pkey), cnt in merged.items():
            by_agent[email].append((pkey, cnt))
        for email in by_agent:
            # Busiest project first; tie-break by project key so tied counts are
            # DETERMINISTIC (Codex P2): the aggregation query has no ORDER BY, so a
            # bare -count sort left tied projects in arbitrary order — a reviewed
            # shadow run could pick a different primary than a later --apply.
            by_agent[email].sort(key=lambda pc: (-pc[1], pc[0]))

        for email, ranked in by_agent.items():
            for rank, (pkey, cnt) in enumerate(ranked):  # busiest first
                if pkey in desired[email]:
                    continue  # a PLS-8 lead assignment for this project wins
                # Bound CANDIDATES at the absolute ceiling — no agent can ever hold
                # more than _MAX_ACTIVE active rows total; the real existing-aware cap
                # is applied per-row in the apply/report stage and may trim further.
                if len(desired[email]) >= _MAX_ACTIVE:
                    break
                desired[email][pkey] = {
                    'tier': 'primary' if rank == 0 else 'secondary',
                    'is_lead': False, 'user_id': email_uid.get(email),
                    'reason': f'activity-derived ({cnt} acts/{days}d, rank {rank + 1})',
                }

        # ── shadow diff vs current, optional apply ───────────────────────────
        report = []
        seeded = updated = skipped = skipped_at_cap = skipped_unavailable = skipped_availability_unknown = 0
        tag = '/lead'
        for email in sorted(desired):
            # Include INACTIVE rows (Codex P2): an expired row is invisible to an
            # active=True filter, so the command treats it as missing and create_*
            # hits the unique (project, agent_user_id) constraint → IntegrityError that
            # aborts the run. Detect the inactive match and REACTIVATE instead. Prefer
            # an active row when both exist for the same project key.
            # Scope the current-state map to the TARGET projects (Codex P2): keying by
            # bare project.key across all orgs let an agent's same-keyed membership in
            # another org populate cur[KEY] and drive wrong present/update decisions.
            # Match by EITHER identifier (Codex P2): activity specs carry a user_id and
            # model uniqueness is (project, agent_user_id), so a row created under an
            # OLD email for the same user must be found by id after email drift.
            auid = email_uid.get(email)
            ident = Q(agent_user_email__iexact=email)
            if auid:
                ident |= Q(agent_user_id=auid)
            cur: dict = {}
            for m in (AgentProjectMembership.objects
                      .filter(project_id__in=target_proj_ids).filter(ident)
                      .select_related('project')):
                k = m.project.key.upper()
                if k not in cur or (m.active and not cur[k].active):
                    cur[k] = m

            # SIGNED-cap budget (Codex P2): clean() counts the agent's FULL active set
            # (by email OR id, across ALL projects), so the room for NEW activations is
            # _MAX_ACTIVE minus what the agent already holds active — NOT minus only the
            # rows derived this run. Counting only this run's rows let the shadow report
            # show 4 would-adds while --apply seeded fewer (manual/other-project active
            # rows already consumed slots), so the CoS-reviewed matrix ≠ the seeded one.
            # Leads are first in `desired`, so they claim the budget ahead of activity.
            total_active_before = (AgentProjectMembership.objects
                                   .filter(active=True).filter(ident).count())
            budget = _MAX_ACTIVE - total_active_before
            activations = 0  # new activations (would-add / would-reactivate) so far

            for pkey, spec in desired[email].items():
                existing = cur.get(pkey)
                want = f"{spec['tier']}{tag if spec['is_lead'] else ''}"
                # Include email equality (Codex P2): an id-matched row with a STALE
                # agent_user_email must NOT count as in-sync — otherwise it reports
                # 'present' and the email correction (ident_fix) below, guarded by
                # action.startswith('would-'), is skipped, leaving the seeded identity
                # stale. Treating it as would-update routes it through the apply path
                # that adopts the canonical email.
                in_sync = (existing and existing.tier == spec['tier']
                           and existing.is_lead == spec['is_lead']
                           and (existing.agent_user_email or '').lower() == email)
                if existing and existing.active and in_sync:
                    action = 'present'
                elif existing and not existing.active:
                    action = f"would-reactivate -> {want}"
                elif existing:
                    action = f"would-update -> {want}"
                else:
                    action = f"would-add {want}"

                # Availability gate (PLAT-1225): stopped/disabled/unavailable agents
                # may still have historical activity or sticky ProjectTeamLead rows, but
                # EVOL-38 says they must not become active affinity targets unless a
                # deliberate reactivation happens first. Gate BEFORE cap accounting and
                # before --apply writes so shadow == apply exactly for this branch.
                available, availability_skip_reason = _agent_available_for_membership(email)
                if not available:
                    report.append({
                        'agent': email, 'project': pkey, 'reason': spec['reason'],
                        'action': f"{action} [SKIPPED: {availability_skip_reason}]",
                    })
                    skipped += 1
                    if availability_skip_reason == 'availability_unknown':
                        skipped_availability_unknown += 1
                    else:
                        skipped_unavailable += 1
                    continue

                # A NEW activation (would-add / would-reactivate) only fits if the
                # agent's current active total leaves room. present/would-update touch
                # an already-active row and never consume budget. Gate BEFORE reporting
                # or applying so shadow == apply exactly.
                is_new_activation = action.startswith('would-add') or action.startswith('would-reactivate')
                if is_new_activation and activations >= budget:
                    report.append({
                        'agent': email, 'project': pkey, 'reason': spec['reason'],
                        'action': f"{action} [SKIPPED: at cap "
                                  f"({total_active_before} existing + {activations} new >= {_MAX_ACTIVE})]",
                    })
                    skipped += 1
                    skipped_at_cap += 1
                    continue

                entry = {'agent': email, 'project': pkey, 'action': action, 'reason': spec['reason']}

                # Adopt the current canonical email when matching an existing row
                # (Codex P2): after email drift the id-matched row can carry a stale
                # agent_user_email, and the apply paths otherwise only touch tier/lead/
                # active — leaving the seeded matrix on the old identity. (Only the
                # email is adopted; we never rewrite agent_user_id, which would risk
                # the (project, agent_user_id) unique constraint.)
                ident_fix = {}
                if existing is not None and (existing.agent_user_email or '').lower() != email:
                    ident_fix['agent_user_email'] = email

                # Preflight an email rekey against the cap (Codex P2 — keep shadow==apply).
                # update_membership() runs full_clean() on an agent_user_email change, so
                # an over-cap agent's would-update rekey is REJECTED at --apply with a
                # ValidationError — but the shadow report would otherwise still say
                # 'would-update', breaking the shadow==apply guarantee. full_clean counts
                # the agent's active rows (canonical email OR id) EXCLUDING self, which
                # equals total_active_before - 1 (a would-update row is active + id-matched,
                # so it's in total_active_before), i.e. the rekey trips exactly when
                # total_active_before > _MAX_ACTIVE. Predict the skip identically in BOTH
                # modes. (would-reactivate rekeys are already covered by the budget gate
                # above, so this only bites would-update.)
                if (action.startswith('would-update') and ident_fix.get('agent_user_email')
                        and total_active_before > _MAX_ACTIVE):
                    entry['action'] += (
                        f" [SKIPPED: email rekey would exceed cap "
                        f"({total_active_before} active > {_MAX_ACTIVE})]"
                    )
                    skipped += 1
                    report.append(entry)
                    continue

                if apply and action.startswith('would-'):
                    try:
                        if existing is None:
                            svc.create_membership(
                                projects[pkey], agent_user_email=email,
                                agent_user_id=spec['user_id'], tier=spec['tier'],
                                is_lead=spec['is_lead'], reason=spec['reason'],
                                actor_email=_ACTOR_EMAIL,
                            )
                            entry['action'] = action.replace('would-add', 'ADDED')
                            seeded += 1
                            activations += 1
                        elif not existing.active:
                            # Reactivate + align tier/lead/identity; update_membership
                            # re-runs full_clean() on re-activation so the cap still binds.
                            svc.update_membership(
                                existing, active=True, tier=spec['tier'],
                                is_lead=spec['is_lead'], reason=spec['reason'],
                                actor_email=_ACTOR_EMAIL, **ident_fix,
                            )
                            entry['action'] = action.replace('would-reactivate', 'REACTIVATED')
                            seeded += 1
                            activations += 1
                        else:  # would-update: tier/lead/identity on an active row (Codex P2)
                            svc.update_membership(
                                existing, tier=spec['tier'], is_lead=spec['is_lead'],
                                reason=spec['reason'], actor_email=_ACTOR_EMAIL, **ident_fix,
                            )
                            entry['action'] = action.replace('would-update', 'UPDATED')
                            updated += 1
                    except ValidationError as exc:
                        msg = exc.messages[0] if getattr(exc, 'messages', None) else str(exc)
                        entry['action'] += f' [SKIPPED: {msg}]'
                        skipped += 1
                elif is_new_activation:
                    # shadow mode: consume the projected slot so later rows for the
                    # SAME agent see the budget as --apply would.
                    activations += 1
                report.append(entry)

        summary = {
            'mode': 'apply' if apply else 'shadow (report-only, no behaviour change)',
            'window_days': days,
            'org': org_id,
            'ambiguous_keys_skipped': sorted(ambiguous),
            'agents': len(desired),
            'memberships_derived': sum(len(v) for v in desired.values()),
            'seeded': seeded, 'updated': updated, 'skipped_total': skipped,
            'skipped_at_cap': skipped_at_cap,
            'skipped_unavailable': skipped_unavailable,
            'skipped_availability_unknown': skipped_availability_unknown,
        }

        if opts['as_json']:
            self.stdout.write(json.dumps({'summary': summary, 'matrix': report}, indent=2))
            return

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"EVOL-20 Ph0b membership matrix — {summary['mode']}"))
        if ambiguous:
            self.stdout.write(self.style.WARNING(
                f"  ⚠ skipped {len(ambiguous)} key(s) shared across orgs (pass --org to "
                f"disambiguate): {', '.join(sorted(ambiguous))}"))
        for e in report:
            line = f"  {e['agent']:<32} {e['project']:<6} {e['action']:<40} {e['reason']}"
            self.stdout.write(line)
        self.stdout.write(self.style.SUCCESS(
            f"\n{summary['agents']} agents, {summary['memberships_derived']} memberships derived"
            + (f"; {seeded} seeded, {updated} updated, {skipped} skipped "
               f"({skipped_unavailable} unavailable, {skipped_availability_unknown} availability unknown)." if apply
               else f" (shadow — nothing written; {skipped_unavailable} unavailable skipped; {skipped_availability_unknown} availability unknown; "
                    "re-run with --apply after CoS review).")))
