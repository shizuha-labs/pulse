"""Finish the platform-ops -> devops rename at every persistence boundary.

Migration 0095 moved the rows that existed at the time, but later workflow JSON
and a copied PostgreSQL alias table could mint or reject the legacy spelling.
Install alias parity first, then safely backfill rows and transition config.
"""
import json

from django.db import migrations


FORWARD_CANONICAL_TEAM_SQL = """
CREATE OR REPLACE FUNCTION tasks_canonical_team_key(p_team text)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT CASE replace(replace(lower(trim(COALESCE(p_team, ''))), '_', '-'), ' ', '-')
        WHEN 'personal-accounting' THEN 'accounting'
        WHEN 'digital-accounting' THEN 'accounting'
        WHEN 'digital-quant' THEN 'quant'
        WHEN 'securities-engineering' THEN 'engineering'
        WHEN 'trading-engineering' THEN 'engineering'
        WHEN 'admin_ops' THEN 'admin-ops'
        WHEN 'ceo-decisions' THEN 'human-ops'
        WHEN 'leads' THEN 'human-ops'
        WHEN 'triage' THEN 'human-ops'
        WHEN 'security-triage' THEN 'security'
        WHEN 'platform-ops' THEN 'devops'
        WHEN 'analytics' THEN 'research-analytics'
        WHEN 'research' THEN 'research-analytics'
        WHEN 'governance' THEN 'admin-ops'
        WHEN 'program-management' THEN 'admin-ops'
        ELSE replace(replace(lower(trim(COALESCE(p_team, ''))), '_', '-'), ' ', '-')
    END
$$;
"""


REVERSE_CANONICAL_TEAM_SQL = FORWARD_CANONICAL_TEAM_SQL.replace(
    "        WHEN 'platform-ops' THEN 'devops'\n",
    "",
)


def canonicalize_legacy_routing(apps, schema_editor):
    Item = apps.get_model('tasks', 'Item')
    TeamRoster = apps.get_model('tasks', 'TeamRoster')
    WorkflowTransition = apps.get_model('tasks', 'WorkflowTransition')

    # The SQL canonicalizer is installed by the preceding operation.  The
    # assignment trigger therefore sees old and new spellings as one context,
    # allowing existing concrete DevOps ownership to be normalized safely.
    Item.objects.filter(assignment_group='platform-ops').update(
        assignment_group='devops',
    )

    # A legacy roster may have been reintroduced after 0095.  Prefer the
    # canonical row when both exist; otherwise rename in place.
    for legacy in TeamRoster.objects.filter(team_name='platform-ops'):
        canonical = TeamRoster.objects.filter(
            organization_id=legacy.organization_id,
            team_name='devops',
        ).exclude(pk=legacy.pk).first()
        if canonical is None:
            legacy.team_name = 'devops'
            legacy.save(update_fields=['team_name', 'updated_at'])
        else:
            merged_tiers = [list(tier or []) for tier in (canonical.tiers or [])]
            for index, tier in enumerate(legacy.tiers or []):
                while len(merged_tiers) <= index:
                    merged_tiers.append([])
                known = {str(email).strip().lower() for email in merged_tiers[index]}
                for email in tier or []:
                    if str(email).strip().lower() not in known:
                        merged_tiers[index].append(email)
                        known.add(str(email).strip().lower())
            juniors = list(canonical.junior_emails or [])
            known_juniors = {str(email).strip().lower() for email in juniors}
            for email in legacy.junior_emails or []:
                if str(email).strip().lower() not in known_juniors:
                    juniors.append(email)
                    known_juniors.add(str(email).strip().lower())
            canonical.tiers = merged_tiers
            canonical.junior_emails = juniors
            canonical.is_active = canonical.is_active or legacy.is_active
            canonical.save(update_fields=[
                'tiers', 'junior_emails', 'is_active', 'updated_at',
            ])
            legacy.delete()

    for transition in WorkflowTransition.objects.all().iterator():
        post_functions = transition.post_functions
        if isinstance(post_functions, str):
            try:
                post_functions = json.loads(post_functions)
            except (TypeError, ValueError):
                continue
        if not isinstance(post_functions, list):
            continue
        changed = False
        normalized = []
        for post_function in post_functions:
            if not isinstance(post_function, dict):
                normalized.append(post_function)
                continue
            post_function = dict(post_function)
            if post_function.get('type') == 'assign_team':
                for key in ('value', 'team'):
                    value = str(post_function.get(key) or '').strip()
                    if value.lower().replace('_', '-').replace(' ', '-') == 'platform-ops':
                        post_function[key] = 'devops'
                        changed = True
            normalized.append(post_function)
        if changed:
            transition.post_functions = normalized
            transition.save(update_fields=['post_functions'])


class Migration(migrations.Migration):
    dependencies = [('tasks', '0108_pls509_reenable_after_blocked_classifier')]

    operations = [
        migrations.RunSQL(
            FORWARD_CANONICAL_TEAM_SQL,
            reverse_sql=REVERSE_CANONICAL_TEAM_SQL,
        ),
        migrations.RunPython(
            canonicalize_legacy_routing,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
