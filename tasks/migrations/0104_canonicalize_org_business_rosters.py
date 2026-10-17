"""Canonicalize organization-scoped legacy business roster names.

PLAT-4213 introduced organization-scoped rosters, but its data migration kept
the old org-prefixed team names.  New tasks use the unprefixed team identity,
so those legacy rows are invisible to exact ``(organization, team)`` routing.

This migration never creates a roster or a member. A legacy row is renamed in
place when possible. On a name collision, the existing canonical row keeps its
identity and absorbs only memberships that were already active; inactive
configuration must not become routing authority merely because it was merged.

It also retires ProjectMembership grants whose recorded provenance is
``assignment_seed``. Routing ownership is exact-item authority, not an implicit
project-wide authorization grant; explicit and legacy-unknown memberships are
left untouched.
"""

from django.db import migrations


ROSTER_RENAMES = (
    ('personal-3', 'personal-accounting', 'accounting'),
    ('shizuha-digital', 'digital-accounting', 'accounting'),
    ('shizuha-digital', 'digital-quant', 'quant'),
    ('shizuha-trading', 'securities-engineering', 'engineering'),
    ('shizuha-trading', 'trading-engineering', 'engineering'),
)

ORG_IDS = {
    'shizuha': 1,
    'shizuha-digital': 2,
    'shizuha-trading': 3,
    'personal-3': 4,
}

CAPABILITY_PROFILE_RENAMES = (
    ('personal-accounting', 'accounting'),
    ('digital-accounting', 'accounting'),
    ('digital-quant', 'quant'),
    ('securities-engineering', 'engineering'),
    ('trading-engineering', 'engineering'),
)


def _email_key(value):
    return str(value or '').strip().lower()


def _merge_tiers(*tier_sets):
    """Merge tier positions stably, with the first row winning duplicates."""
    depth = max((len(tiers or []) for tiers in tier_sets), default=0)
    merged = [[] for _ in range(depth)]
    seen = set()
    for tiers in tier_sets:
        for index, tier in enumerate(tiers or []):
            for member in tier or []:
                key = _email_key(member)
                if not key or key in seen:
                    continue
                seen.add(key)
                merged[index].append(member)
    return merged


def _merge_emails(*email_sets):
    merged = []
    seen = set()
    for values in email_sets:
        for member in values or []:
            key = _email_key(member)
            if not key or key in seen:
                continue
            seen.add(key)
            merged.append(member)
    return merged


def _member_keys(tiers, junior_emails):
    return {
        key
        for values in [*(tiers or []), junior_emails or []]
        for member in values or []
        if (key := _email_key(member))
    }


def _without_members(tiers, junior_emails, member_keys):
    cleaned_tiers = [
        [member for member in (tier or []) if _email_key(member) not in member_keys]
        for tier in tiers or []
    ]
    cleaned_juniors = [
        member
        for member in junior_emails or []
        if _email_key(member) not in member_keys
    ]
    return cleaned_tiers, cleaned_juniors


def _enforce_placement(TeamRoster, owner):
    """Mirror TeamRoster.save(): an active member has one roster placement."""
    if not owner.is_active:
        return
    member_keys = _member_keys(owner.tiers, owner.junior_emails)
    if not member_keys:
        return
    for roster in TeamRoster.objects.exclude(pk=owner.pk).order_by('pk'):
        tiers, juniors = _without_members(
            roster.tiers,
            roster.junior_emails,
            member_keys,
        )
        if tiers == (roster.tiers or []) and juniors == (roster.junior_emails or []):
            continue
        roster.tiers = tiers
        roster.junior_emails = juniors
        roster.save(update_fields=['tiers', 'junior_emails'])


def forwards(apps, schema_editor):
    TeamRoster = apps.get_model('tasks', 'TeamRoster')
    TeamCapabilityProfile = apps.get_model('tasks', 'TeamCapabilityProfile')
    Item = apps.get_model('tasks', 'Item')
    ProjectMembership = apps.get_model('tasks', 'ProjectMembership')
    WebhookKey = apps.get_model('tasks', 'WebhookKey')

    ProjectMembership.objects.filter(
        grant_source='assignment_seed',
        is_active=True,
    ).update(is_active=False)

    # The fixed order makes the winner deterministic even if already-corrupt
    # data places one email in more than one legacy roster.
    for organization_slug, legacy_name, canonical_name in ROSTER_RENAMES:
        legacy = TeamRoster.objects.filter(
            organization_slug=organization_slug,
            team_name=legacy_name,
        ).order_by('pk').first()
        if legacy is None:
            continue

        canonical = TeamRoster.objects.filter(
            organization_slug=organization_slug,
            team_name=canonical_name,
        ).order_by('pk').first()
        if canonical is None:
            legacy.team_name = canonical_name
            legacy.save(update_fields=['team_name'])
            _enforce_placement(TeamRoster, legacy)
            continue

        active_rows = [row for row in (canonical, legacy) if row.is_active]
        payload_rows = active_rows or [canonical, legacy]
        canonical.tiers = _merge_tiers(*(row.tiers for row in payload_rows))
        canonical.junior_emails = _merge_emails(
            *(row.junior_emails for row in payload_rows)
        )
        canonical.is_active = bool(active_rows)
        canonical.save(update_fields=['tiers', 'junior_emails', 'is_active'])
        legacy.delete()
        _enforce_placement(TeamRoster, canonical)

    # Capability policy is keyed by canonical team name and intentionally
    # organization-agnostic. Merge legacy aliases into that one policy row so
    # seed commands and runtime checks cannot resurrect a second team identity.
    for legacy_name, canonical_name in CAPABILITY_PROFILE_RENAMES:
        legacy = TeamCapabilityProfile.objects.filter(
            team_name=legacy_name,
        ).first()
        if legacy is None:
            continue
        canonical = TeamCapabilityProfile.objects.filter(
            team_name=canonical_name,
        ).first()
        if canonical is None:
            legacy.team_name = canonical_name
            legacy.updated_by = '0104_canonicalize_org_business_rosters'
            legacy.save(update_fields=['team_name', 'updated_by'])
            continue
        canonical.capabilities = sorted(set(
            (canonical.capabilities or []) + (legacy.capabilities or [])
        ))
        canonical.updated_by = '0104_canonicalize_org_business_rosters'
        canonical.save(update_fields=['capabilities', 'updated_by'])
        legacy.delete()

    # Task and integration routing values keep the bare canonical Team key;
    # project.org_id remains the separate tenant dimension.
    for organization_slug, legacy_name, canonical_name in ROSTER_RENAMES:
        organization_id = ORG_IDS[organization_slug]
        Item.objects.filter(
            project__org_id=organization_id,
            assignment_group=legacy_name,
        ).update(assignment_group=canonical_name)
        WebhookKey.objects.filter(
            default_project__org_id=organization_id,
            default_assignment_group=legacy_name,
        ).update(default_assignment_group=canonical_name)


class Migration(migrations.Migration):
    dependencies = [
        ('tasks', '0103_merge_research_into_research_analytics'),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
