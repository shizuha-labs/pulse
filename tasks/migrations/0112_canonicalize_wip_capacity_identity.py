"""Collapse agent-email aliases onto one stable WIP-capacity subject."""

from django.db import migrations, models
from django.db.models import Max, Q


AGENT_SUFFIX = '@agents.shizuha.io'
PRIMARY_SUFFIX = '@shizuha.com'


def _canonical_email(value):
    normalized = str(value or '').strip().lower()
    if normalized.endswith(AGENT_SUFFIX):
        return normalized[:-len(AGENT_SUFFIX)] + PRIMARY_SUFFIX
    return normalized


def collapse_duplicate_capacity_rows(apps, schema_editor):
    Capacity = apps.get_model('tasks', 'AgentWipCapacity')
    Reservation = apps.get_model('tasks', 'WipSlotReservation')
    rows = list(Capacity.objects.order_by('organization_slug', 'pk'))

    # Alias-equivalent emails and equal non-null identity IDs form one connected
    # component. Build the components before updating any email so the existing
    # raw-email unique constraint can never be hit midway through reconciliation.
    parent = {row.pk: row.pk for row in rows}

    def find(value):
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(left, right):
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    by_email = {}
    by_identity = {}
    for row in rows:
        email_key = (row.organization_slug, _canonical_email(row.assignee_email))
        if email_key in by_email:
            union(by_email[email_key], row.pk)
        else:
            by_email[email_key] = row.pk
        if row.assignee_id is not None:
            id_key = (row.organization_slug, int(row.assignee_id))
            if id_key in by_identity:
                union(by_identity[id_key], row.pk)
            else:
                by_identity[id_key] = row.pk

    groups = {}
    for row in rows:
        groups.setdefault(find(row.pk), []).append(row)

    for members in groups.values():
        canonical_email = _canonical_email(members[0].assignee_email)
        survivor = next(
            (
                row for row in members
                if str(row.assignee_email or '').strip().lower() == canonical_email
            ),
            members[0],
        )
        duplicate_ids = [row.pk for row in members if row.pk != survivor.pk]
        if duplicate_ids:
            Reservation.objects.filter(capacity_id__in=duplicate_ids).update(
                capacity_id=survivor.pk,
            )
            Capacity.objects.filter(pk__in=duplicate_ids).delete()

        reservation_generation = (
            Reservation.objects.filter(capacity_id=survivor.pk)
            .aggregate(value=Max('assignment_generation'))['value']
            or 0
        )
        survivor.assignee_email = canonical_email
        survivor.assignee_id = next(
            (row.assignee_id for row in members if row.assignee_id is not None),
            None,
        )
        survivor.generation = max(
            [reservation_generation] + [int(row.generation or 0) for row in members]
        )
        survivor.save(update_fields=['assignee_email', 'assignee_id', 'generation'])


class Migration(migrations.Migration):
    # PostgreSQL refuses CREATE INDEX while the preceding data migration has
    # deferred FK trigger events in the same transaction. Keep reconciliation
    # itself atomic, commit it, then install the identity constraint.
    atomic = False
    dependencies = [('tasks', '0111_blocker_wip_invariant')]

    operations = [
        migrations.RunPython(
            collapse_duplicate_capacity_rows,
            migrations.RunPython.noop,
            atomic=True,
        ),
        migrations.AddConstraint(
            model_name='agentwipcapacity',
            constraint=models.UniqueConstraint(
                fields=('organization_slug', 'assignee_id'),
                condition=Q(assignee_id__isnull=False),
                name='uniq_agent_wip_capacity_identity',
            ),
        ),
    ]
