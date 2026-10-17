"""Seed/refresh TeamCapabilityProfile — the DB SoT for team -> capability TAGS
(operator 2026-07-11 / PLAT-4223 / migration 0096). Idempotent + ADDITIVE-merge
(never removes a manually-added cap).

Operator directive 2026-07-12: "required capabilities should be set on the team,
and Hive should ensure the underlying agents automatically get all the required
capabilities." A code task routed to e.g. `architecture` required `code-substrate`
+ `sara2574-push`, but those baseline coding caps were declared on NO team, so
the router found no capability-compatible seat and silently held the task for
hours. Declaring them here (a) makes Pulse routing pass (the gate reads this
table via WorkflowService._team_conferred_capabilities) and (b) makes Hive
provision them onto every member agent (Hive reconciles this same profile via
/api/internal/team-capabilities/ -> github identity, MCP, substrate).

Split of authority (unchanged): Pulse owns team -> abstract capability TAGS;
Hive owns TAG -> concrete resources. This file only declares tags.
"""
from django.core.management.base import BaseCommand

from tasks.models import TeamCapabilityProfile

# Baseline every CODE-AUTHORING team needs: a repo checkout (`code-substrate`),
# the fork-PR push identity (`sara2574-push`), and a GitHub token to push with
# (`github-token` — the required-capability normalizer expands sara2574-push →
# +github-token, so a team missing the token can't satisfy PR work). Write teams
# additionally get direct-write (`repo-write`). Elevated substrate stays team-specific.
CODE = ['code-substrate', 'sara2574-push', 'github-token']
WRITE = ['repo-write']

PROFILES = {
    # infra/ops — elevated substrate + they author infra code
    'devops':        ['kubeconfig', 'credential-broker', 'host-exec'] + CODE + WRITE,
    'platform-ops':  ['kubeconfig', 'credential-broker', 'host-exec'] + CODE + WRITE,
    # merge — merges PRs + maintains branches (rebase/conflict work needs checkout+push)
    'merge':         ['github:merge'] + CODE + WRITE,
    # review — reviews PRs; may check code out
    'review':        ['github:review'] + CODE,
    'security':      ['security-review'] + CODE,
    'admin-ops':     ['admin-org'],
    # code-authoring product/eng teams — baseline
    'architecture':  list(CODE),
    'engineering':   CODE + WRITE,
    'qa':            list(CODE),
    'documentation': list(CODE),
    'research-analytics': list(CODE),
    'quant':          list(CODE),
}


class Command(BaseCommand):
    help = 'Seed/refresh the DB TeamCapabilityProfile SoT (idempotent, additive-merge).'

    def add_arguments(self, parser):
        parser.add_argument('--prune', action='store_true',
                            help='Also REMOVE caps not in the seed (default: additive-only).')

    def handle(self, *args, **opts):
        changed = 0
        for team, caps in PROFILES.items():
            want = sorted(set(caps))
            row, created = TeamCapabilityProfile.objects.get_or_create(
                team_name=team,
                defaults={'capabilities': want, 'updated_by': 'seed_team_capability_profiles'},
            )
            if created:
                changed += 1
                self.stdout.write(f'+ {team}: {want}')
                continue
            have = set(row.capabilities or [])
            merged = set(want) if opts['prune'] else (have | set(want))
            if sorted(merged) != sorted(have):
                row.capabilities = sorted(merged)
                row.updated_by = 'seed_team_capability_profiles'
                row.save(update_fields=['capabilities', 'updated_by', 'updated_at'])
                changed += 1
                self.stdout.write(f'~ {team}: {sorted(merged)}')
        self.stdout.write(self.style.SUCCESS(f'seeded/updated {changed} team capability profile(s)'))
