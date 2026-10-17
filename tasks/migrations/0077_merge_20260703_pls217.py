# PLS-217: merge existing task migration leaves so deployment-event tests can run.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0075_merge_0074_leaves'),
        ('tasks', '0075_pls214_recurring_cycle_stale_activity'),
        ('tasks', '0076_alter_status_category'),
    ]

    operations = []
