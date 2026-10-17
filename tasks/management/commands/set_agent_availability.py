"""Mark an agent/employee active or inactive (on-leave) — durable, DB-backed.

This is the authoritative, persistent way to take an agent out of (or back into)
work rotation. It survives restarts, redeploys, sweep failures and Redis TTLs —
unlike the ephemeral ``pulse:agent_disabled`` cache. Routing skips inactive agents
and the rebalance sweep drains their in-flight work to active peers.

Examples:
    python manage.py set_agent_availability kai@shizuha.com --inactive --status on_leave \
        --reason "codex usage exhausted" --by operator
    python manage.py set_agent_availability kai@shizuha.com --active --by operator
    python manage.py set_agent_availability --list

``--by`` is REQUIRED on every write. It stamps the durable ``updated_by`` column,
which every automated recovery path treats as authority-bearing: a row tagged
``updated_by=operator`` is sacred and permanently exempt from auto-reactivation.
Leaving it to silently default to ``operator`` would convert a routine manual
clear into a row no sweep can ever restore (PLS-736). Callers must state who is
acting (a named agent, a runbook id, or an explicit deliberate ``operator``).
"""
from django.core.management.base import BaseCommand, CommandError

from tasks.models import AgentAvailability


class Command(BaseCommand):
    help = "Set an agent/employee active or inactive (on-leave) — durable, DB-backed."

    def add_arguments(self, parser):
        parser.add_argument('email', nargs='?', help='Agent email (e.g. kai@shizuha.com)')
        group = parser.add_mutually_exclusive_group()
        group.add_argument('--active', action='store_true', help='Mark active (available for work).')
        group.add_argument('--inactive', action='store_true', help='Mark inactive / on-leave.')
        parser.add_argument('--status', default=None,
                            choices=['active', 'inactive', 'on_leave', 'disabled'],
                            help='Explicit status: active|inactive|on_leave|disabled. '
                                 'Must not contradict --active/--inactive.')
        parser.add_argument('--reason', default='', help='Free-text reason (audit).')
        parser.add_argument('--by', help=(
            'Who set it (audit). REQUIRED on every write: stamps the durable '
            'updated_by column. A value of "operator" is sacred — rows stamped '
            'operator are permanently exempt from automated recovery. State an '
            'explicit identity (agent email or runbook id); use operator only '
            'for a deliberate force decision.'))
        parser.add_argument('--list', action='store_true', help='List all overrides and exit.')

    def handle(self, *args, **opts):
        if opts['list']:
            rows = AgentAvailability.objects.all().order_by('email')
            if not rows:
                self.stdout.write('(no agent availability overrides set)')
            for r in rows:
                self.stdout.write(f'{r.email:32} active={r.active!s:5} status={r.status:9} '
                                  f'by={r.updated_by} reason={r.reason[:60]}')
            # PLS-740: a force-parked seat (env list) appears in NO DB row and used
            # to appear in no census at all. Surface env-parked seats here so a
            # reader can tell a deliberately-parked seat from a healthy one.
            try:
                from tasks.services.workflow_service import WorkflowService
                env_parked = WorkflowService._configured_email_set(
                    'PULSE_INACTIVE_AGENT_EMAILS',
                    'PULSE_ABSENT_AGENT_EMAILS',
                    'PULSE_UNAVAILABLE_AGENT_EMAILS',
                )
            except Exception:
                env_parked = set()
            if env_parked:
                self.stdout.write('')
                self.stdout.write('env force-parked (PULSE_INACTIVE/ABSENT/UNAVAILABLE_AGENT_EMAILS):')
                for em in sorted(env_parked):
                    self.stdout.write(f'{em:32} active=False  status=force_parked by=env-list reason=env force-park')
            return

        email = (opts.get('email') or '').strip().lower()
        if not email:
            raise CommandError('email is required (or use --list)')
        if not (opts['active'] or opts['inactive'] or opts['status']):
            raise CommandError('specify --active, --inactive, or --status')

        # PLS-736 (P2): reject flag/--status CONTRADICTIONS instead of silently
        # resolving them. Previously `--inactive --status active` resolved
        # `active = opts['active'] or (status=='active')` and stamped the row
        # ACTIVE — the fail-open case in the ticket title. The boolean flag's
        # implied active-state and the explicit status must agree.
        flag_implies_active = bool(opts['active'])
        flag_implies_inactive = bool(opts['inactive'])
        if opts['status']:
            status_implies_active = (opts['status'] == 'active')
            if flag_implies_active and not status_implies_active:
                raise CommandError(
                    f'--active contradicts --status {opts["status"]}: '
                    f'{opts["status"]} implies inactive. Pick one intent.')
            if flag_implies_inactive and status_implies_active:
                raise CommandError(
                    '--inactive contradicts --status active: active implies '
                    'active. Pick one intent.')

        # PLS-736 (P1): `--by` is REQUIRED, never defaulted. The old default
        # `'operator'` is the one value every automated recovery path treats as
        # sacred — a routine manual clear stamped operator becomes permanently
        # unrecoverable by automation, and the success message gave no hint.
        by = (opts.get('by') or '').strip()
        if not by:
            raise CommandError(
                '--by is required: state who is making this change (agent email '
                'or runbook id, e.g. --by pls734-manual-clear). A bare '
                '"operator" default would stamp an irreversibly sacred row — '
                'pass --by operator only for a deliberate force decision.')

        active = opts['active'] or (opts['status'] == 'active')
        status = opts['status'] or ('active' if active else 'inactive')

        row, created = AgentAvailability.objects.update_or_create(
            email=email,
            defaults={
                'active': active,
                'status': status,
                'reason': opts['reason'],
                'updated_by': by,
            },
        )
        verb = 'created' if created else 'updated'
        self.stdout.write(self.style.SUCCESS(
            f'{verb}: {row.email} -> active={row.active} status={row.status} (by {row.updated_by})'
        ))
