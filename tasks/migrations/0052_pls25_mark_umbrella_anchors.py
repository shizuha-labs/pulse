"""PLS-25: pin the known standing umbrella/tracking anchors to item_type='epic'.

The PLAT-236 rebalance sweep must SKIP epic/umbrella containers (their children
carry the work); WorkflowService._umbrella_item_q() detects them primarily by
item_type. But item_type defaults to 'task.generic' and these anchors may have
been created (MCP/API) without an explicit epic type, which is exactly why
SCLI-1/SCLI-3 bounced 3x/24h between agents. Pin the documented anchors to 'epic'
so they are caught regardless of how they were created (architect-review ask,
option (b) — the durable model). Idempotent and scoped to the known keys; the
broadened title-prefix signal in _umbrella_item_q() covers future umbrellas.
"""
from django.db import migrations

# Documented standing architecture/tracking anchors (PLS-25 description + aoi gate).
_UMBRELLA_KEYS = ['SCLI-1', 'SCLI-2', 'SCLI-3', 'EVOL-1']


def mark_umbrellas(apps, schema_editor):
    from django.db.models import Q
    Item = apps.get_model('tasks', 'Item')
    # Only pin anchors that are NOT already an umbrella type, and set the canonical
    # 'task.epic' (the IssueType default Epic prefix) — NOT bare 'epic' (Codex P2):
    # _umbrella_item_q() already matches epic / epic.* / task.epic*, so an anchor
    # already typed task.epic must be LEFT ALONE (rewriting it to bare 'epic' would
    # strip the canonical prefix and break API/UI filters keyed on item_type=task.epic*).
    already_umbrella = (
        Q(item_type='epic')
        | Q(item_type__startswith='epic.')
        | Q(item_type__startswith='task.epic')
    )
    (Item.objects.filter(item_key__in=_UMBRELLA_KEYS)
        .exclude(already_umbrella)
        .update(item_type='task.epic'))


def noop(apps, schema_editor):
    # No reverse: these are genuinely epics; don't re-generic them on rollback.
    pass


class Migration(migrations.Migration):
    dependencies = [('tasks', '0051_agent_project_membership')]
    operations = [migrations.RunPython(mark_umbrellas, noop)]
