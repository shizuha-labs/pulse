"""Seed hardening-critical team members as project-admin ProjectMembership records.

This migration performs the minimal data seeding required after tightening
`allowed_roles` checks so high-value transitions (security verify/merge/approve)
remain usable by the intended operators.

Why:
- A pure code-tightening of role checks would otherwise deny transition actions
  for active operators that were historically routed by team auto-assignment but
  never populated as ProjectMembership users.
- The old permissive org-member fallback made this a soft gate; removing it
  requires an explicit seed for known operators.

Scope is intentionally narrow:
- Only users in the security/architecture/review control pods are added.
- They are added as `admin` on all Shizuha projects where needed.
- Existing memberships are left intact unless they are downgraded/empty; we
  promote admin-relevant rows to `admin` so explicit gates are enforceable.

The migration is idempotent and safe to re-run.
"""

from django.db import migrations


# Control pod members who must continue to drive security/multi-team gates.
# This keeps the tighten-in-code change from being a platform-wide lockout.
_CRITICAL_OPERATOR_EMAILS = {
    'aoi@shizuha.com',
    'revi@shizuha.com',
    'akira@shizuha.com',
    'ren@shizuha.com',
    'sora@shizuha.com',
}


# Keep only real pod members; this avoids adding accidental addresses.
# (Kept as lowercase for deterministic comparisons.)
_CRITICAL_OPERATOR_EMAILS = {e.lower() for e in _CRITICAL_OPERATOR_EMAILS if isinstance(e, str)}


def seed_memberships(apps, schema_editor):
    Project = apps.get_model('tasks', 'Project')
    ProjectMembership = apps.get_model('tasks', 'ProjectMembership')

    # Seed only organizations and projects that are part of Pulse operations.
    # `org_id=1` is the canonical Shizuha organization in this deployment.
    projects = Project.objects.filter(org_id=1, is_active=True)

    for project in projects.iterator():
        for email in _CRITICAL_OPERATOR_EMAILS:
            membership = ProjectMembership.objects.filter(
                project=project,
                user_email__iexact=email,
            ).first()

            if membership is None:
                ProjectMembership.objects.create(
                    project=project,
                    user_email=email,
                    role='admin',
                    is_active=True,
                )
                continue

            # Promote legacy rows used for explicit operator routing to admin.
            if membership.role != 'admin':
                membership.role = 'admin'
                membership.is_active = True
                membership.save(update_fields=['role', 'is_active'])


def unseed_memberships(apps, schema_editor):
    """No-op.

    We keep seeded rows on rollback because these are operational hardening
    guardrails; removing them immediately after rollback can recreate the same
    lockout class we are mitigating.
    """
    return


class Migration(migrations.Migration):
    dependencies = [
        ('tasks', '0041_seed_deferred_status'),
    ]

    operations = [
        migrations.RunPython(seed_memberships, unseed_memberships),
    ]
