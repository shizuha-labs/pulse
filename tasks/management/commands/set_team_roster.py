"""Safely set a team's routing roster — validated.

The TeamRoster.tiers JSON is the authoritative team-routing source (PLAT-431,
DB-backed, no code roster). Editing it by hand (Django admin / shell) is how the
2026-06-15 char-splat corruption happened. This command is the validated CLI edit
path; the model's validate_roster_structure() guard rejects malformed input on
EVERY write path regardless, so this command can never write corruption.

Examples:
  # single load-balanced tier
  python manage.py set_team_roster engineering --tiers '[["ryo@shizuha.com","sara@shizuha.com"]]' --by hritik
  # primary + failover tiers
  python manage.py set_team_roster review --tiers '[["rei@shizuha.com"],["ren@shizuha.com"]]'
  # decommission a team (empty)
  python manage.py set_team_roster security --tiers '[]'
  # show current
  python manage.py set_team_roster engineering --show
"""
import json

from django.core.management.base import BaseCommand, CommandError

from tasks.models import TeamRoster


class Command(BaseCommand):
    help = "Set a team routing roster (validated). Prevents char-splat / malformed JSON."

    def add_arguments(self, parser):
        parser.add_argument('team', help='Team name (canonicalized, e.g. engineering, review, admin-ops)')
        parser.add_argument('--organization', default='shizuha',
                            help='Organization slug (default: shizuha)')
        parser.add_argument('--tiers', help='JSON: list of tiers, each a list of email strings, e.g. [["a@x.com"],["b@x.com"]]')
        parser.add_argument('--junior', default=None, help='JSON: flat list of junior-member emails')
        parser.add_argument('--inactive', action='store_true', help='Mark the roster row inactive')
        parser.add_argument('--active', action='store_true', help='Mark the roster row active (default for new rows)')
        parser.add_argument('--by', default='cli', help='Audit: who is making the change')
        parser.add_argument('--show', action='store_true', help='Print the current roster and exit')

    def handle(self, *args, **o):
        team = o['team']
        organization_slug = (o['organization'] or 'shizuha').strip().lower()
        if o['show']:
            row = TeamRoster.objects.filter(
                team_name=team, organization_slug=organization_slug,
            ).first()
            if not row:
                # canonicalization may differ; do a best-effort lookup via the model
                norm = TeamRoster(team_name=team)
                norm.team_name = (team or '').strip().lower().replace('_', '-').replace(' ', '-')
                norm.team_name = TeamRoster._CANONICAL_ALIASES.get(norm.team_name, norm.team_name)
                row = TeamRoster.objects.filter(
                    team_name=norm.team_name, organization_slug=organization_slug,
                ).first()
            if not row:
                raise CommandError(f'no roster row for team {team!r}')
            self.stdout.write(f'{row.organization_slug}/{row.team_name}: tiers={row.tiers} '
                              f'junior={row.junior_emails} active={row.is_active}')
            return

        if not o['tiers']:
            raise CommandError('--tiers is required (or use --show)')
        try:
            tiers = json.loads(o['tiers'])
        except Exception as e:
            raise CommandError(f'--tiers is not valid JSON: {e}')
        junior = None
        if o['junior'] is not None:
            try:
                junior = json.loads(o['junior'])
            except Exception as e:
                raise CommandError(f'--junior is not valid JSON: {e}')

        # Validate up-front for a clean CLI error (save() re-validates anyway).
        from django.core.exceptions import ValidationError
        try:
            TeamRoster.validate_roster_structure(tiers, junior if junior is not None else [])
        except ValidationError as e:
            raise CommandError(f'rejected by validation: {e.message_dict if hasattr(e, "message_dict") else e}')

        row, created = TeamRoster.objects.get_or_create(
            team_name=team, organization_slug=organization_slug,
        )
        row.tiers = tiers
        if junior is not None:
            row.junior_emails = junior
        if o['inactive']:
            row.is_active = False
        elif o['active']:
            row.is_active = True
        row.updated_by = o['by']
        try:
            row.save()
        except ValidationError as e:
            raise CommandError(f'rejected by validation: {e.message_dict if hasattr(e, "message_dict") else e}')

        self.stdout.write(self.style.SUCCESS(
            f'{"created" if created else "updated"} roster {row.organization_slug}/{row.team_name}: tiers={row.tiers} '
            f'junior={row.junior_emails} active={row.is_active}'
        ))
