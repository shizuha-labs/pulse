"""PLS-1244: composite (action, created_at) index on ItemActivity.

lane-health `_last_assignment` runs, per lane, `filter(action='assigned',
item__assignment_group=<team>, item__mode='task').order_by('-created_at')
.first()`. The single-column action index cannot serve the reverse-chron
ordering, so the planner sorted the full action-filtered set per lane —
measured 1.66s/lane cold (~38s across 23 lanes, the dominant single cost of
the ~90-120s cold-snapshot build). The composite index lets the planner walk
the assigned subset newest-first and stop at the first join match.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0177_workflowstatus_owner_action_policy'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='itemactivity',
            index=models.Index(
                fields=['action', 'created_at'],
                name='tasks_item_action__created_idx',
            ),
        ),
    ]
