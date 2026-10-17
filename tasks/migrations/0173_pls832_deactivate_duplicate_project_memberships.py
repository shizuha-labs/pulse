"""PLS-832: deactivate duplicate ProjectMembership identity edges.

unique_together is only (project, user_id), so the same person can have both a
resolved user_id row and an email-only row. List endpoints now dedupe in
application code; this migration retires the weaker active duplicates so
storage matches the list contract.
"""

from django.db import migrations


_ROLE_PRIORITY = {
    'owner': 4,
    'admin': 3,
    'member': 2,
    'viewer': 1,
}


def _email_variants(email):
    raw = str(email or '').strip().lower()
    if not raw:
        return []
    variants = [raw]
    if raw.endswith('@shizuha.com'):
        variants.append(raw[: -len('@shizuha.com')] + '@agents.shizuha.io')
    elif raw.endswith('@agents.shizuha.io'):
        variants.append(raw[: -len('@agents.shizuha.io')] + '@shizuha.com')
    return list(dict.fromkeys(variants))


def _prefer(left, right):
    lp = _ROLE_PRIORITY.get(left.role, 0)
    rp = _ROLE_PRIORITY.get(right.role, 0)
    if lp != rp:
        return left if lp > rp else right
    left_uid = left.user_id is not None
    right_uid = right.user_id is not None
    if left_uid != right_uid:
        return left if left_uid else right
    left_ts = left.updated_at or left.created_at
    right_ts = right.updated_at or right.created_at
    if left_ts and right_ts and left_ts != right_ts:
        return left if left_ts >= right_ts else right
    return left


def deactivate_duplicates(apps, schema_editor):
    ProjectMembership = apps.get_model('tasks', 'ProjectMembership')
    project_ids = (
        ProjectMembership.objects.filter(is_active=True)
        .values_list('project_id', flat=True)
        .distinct()
    )
    for project_id in project_ids:
        rows = list(
            ProjectMembership.objects.filter(project_id=project_id, is_active=True)
        )
        if len(rows) <= 1:
            continue

        parent = list(range(len(rows)))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        def union(i, j):
            ri, rj = find(i), find(j)
            if ri != rj:
                parent[rj] = ri

        uid_first = {}
        email_first = {}
        for i, row in enumerate(rows):
            if row.user_id is not None:
                if row.user_id in uid_first:
                    union(i, uid_first[row.user_id])
                else:
                    uid_first[row.user_id] = i
            for variant in _email_variants(row.user_email):
                if variant in email_first:
                    union(i, email_first[variant])
                else:
                    email_first[variant] = i

        groups = {}
        for i, row in enumerate(rows):
            groups.setdefault(find(i), []).append(row)

        for cluster in groups.values():
            if len(cluster) <= 1:
                continue
            best = cluster[0]
            for other in cluster[1:]:
                best = _prefer(best, other)
            for row in cluster:
                if row.id == best.id:
                    continue
                ProjectMembership.objects.filter(pk=row.pk, is_active=True).update(
                    is_active=False,
                )


def noop_reverse(apps, schema_editor):
    # Deactivation is intentionally one-way; re-activating would reintroduce
    # the PLS-832 duplicate list bug.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ('tasks', '0172_item_current_state_item_current_state_updated_at'),
    ]

    operations = [
        migrations.RunPython(deactivate_duplicates, noop_reverse),
    ]
