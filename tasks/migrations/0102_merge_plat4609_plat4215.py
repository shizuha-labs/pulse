"""Merge migration: PLAT-4609 (0100_plat4609_escalation_receipts) and
PLAT-4215 (0101_plat4215_team_owned_blocker) landed in parallel from 0099,
leaving two leaf nodes — every fresh `manage.py test`/migrate errored with
"Conflicting migrations detected" (PR merges bypass the local push gates,
PLAT-4516). No operations; this only joins the graph.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0100_plat4609_escalation_receipts'),
        ('tasks', '0101_plat4215_team_owned_blocker'),
    ]

    operations = []
