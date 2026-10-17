"""PLAT-431 (cycle 5): explicit TeamRoster.junior_emails + seed JUNIOR_ONLY teams.

Replaces tier-POSITION junior inference (WorkflowService._junior_emails reading
``tiers[-1]``, which misidentified a senior failover as junior when an operator
removed the junior tier) with an explicit, operator-editable DB column. Seeds the
two JUNIOR_ONLY teams (review/documentation) with their current juniors so the
junior routing guard keeps working after the switch. Idempotent: only sets the
column when a row exists and hasn't already been given an explicit list.
"""
from django.db import migrations, models


_SEED = {
    'review': ['revi-jr@shizuha.com'],
    'documentation': ['yuki-jr@shizuha.com'],
}


def seed_junior_emails(apps, schema_editor):
    TeamRoster = apps.get_model('tasks', 'TeamRoster')
    for team_name, juniors in _SEED.items():
        row = TeamRoster.objects.filter(team_name=team_name).first()
        if row is not None and not row.junior_emails:
            row.junior_emails = juniors
            row.save(update_fields=['junior_emails'])


def noop(apps, schema_editor):
    # No-op reverse: don't wipe operator-edited junior lists on rollback.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0058_plat431_sync_review_merge_roster'),
    ]

    operations = [
        migrations.AddField(
            model_name='teamroster',
            name='junior_emails',
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.RunPython(seed_junior_emails, noop),
    ]
