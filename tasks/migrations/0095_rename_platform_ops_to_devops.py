"""Operator 2026-07-11: rename the infra/ops team platform-ops -> devops so the
platform uses ONE canonical terminology (no more platform-ops/devops alias
split-brain that routed CI failures to the wrong-capability agent).

'devops' is the canonical infra team name going forward; the old 'platform-ops'
spelling back-compat-aliases to it in WorkflowService._TEAM_ALIASES so any
lingering external emitter still resolves. This migration moves the durable
data: the TeamRoster row and every Item.assignment_group.
"""
from django.db import migrations

OLD = 'platform-ops'
NEW = 'devops'


def forward(apps, schema_editor):
    TeamRoster = apps.get_model('tasks', 'TeamRoster')
    Item = apps.get_model('tasks', 'Item')
    # Remove placeholder inactive 'devops' rows so the rename can't collide on a
    # (team_name, org) uniqueness. The active infra team lives under platform-ops.
    TeamRoster.objects.filter(team_name=NEW, is_active=False).delete()
    # Rename the roster team(s) platform-ops -> devops (org-agnostic; only shizuha
    # has one today, but rename any to be safe).
    for r in TeamRoster.objects.filter(team_name=OLD):
        r.team_name = NEW
        r.save(update_fields=['team_name', 'updated_at'])
    # Migrate stored assignment_group on items (381 rows at write time).
    Item.objects.filter(assignment_group=OLD).update(assignment_group=NEW)


def backward(apps, schema_editor):
    TeamRoster = apps.get_model('tasks', 'TeamRoster')
    Item = apps.get_model('tasks', 'Item')
    for r in TeamRoster.objects.filter(team_name=NEW, is_active=True):
        r.team_name = OLD
        r.save(update_fields=['team_name', 'updated_at'])
    Item.objects.filter(assignment_group=NEW).update(assignment_group=OLD)


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0094_plat4213_teamroster_org'),
    ]

    operations = [
        migrations.RunPython(forward, backward),
    ]
