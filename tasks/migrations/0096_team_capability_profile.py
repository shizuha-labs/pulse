"""Pulse SoT for team → capability tags (operator 2026-07-11).

Creates TeamCapabilityProfile and seeds it from the values currently hardcoded
in WorkflowService._TEAM_CONFERRED_CAPABILITIES, so the capability POLICY
(team → abstract capability tags) becomes queryable DB data instead of a code
constant that Hive must hand-mirror. Hive reads this via
/api/internal/team-capabilities/ and maps each tag → concrete skills/MCP/
credential-scopes on its end.

Seed is inlined (not imported from services/) to avoid the circular import
(services imports models). The dead 'platform-ops' key is dropped — it
canonicalizes to 'devops' after the 0095 rename.
"""
from django.db import migrations, models


SEED = {
    'devops': ['kubeconfig', 'credential-broker', 'host-exec'],
    'merge': ['github:merge', 'repo-write'],
    'review': ['github:review'],
    'security': ['security-review'],
    'admin-ops': ['admin-org'],
}


def seed(apps, schema_editor):
    TeamCapabilityProfile = apps.get_model('tasks', 'TeamCapabilityProfile')
    for team, caps in SEED.items():
        TeamCapabilityProfile.objects.update_or_create(
            team_name=team,
            defaults={'capabilities': caps, 'updated_by': 'migration:0096'},
        )


def unseed(apps, schema_editor):
    TeamCapabilityProfile = apps.get_model('tasks', 'TeamCapabilityProfile')
    TeamCapabilityProfile.objects.filter(team_name__in=SEED.keys()).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0095_rename_platform_ops_to_devops'),
    ]

    operations = [
        migrations.CreateModel(
            name='TeamCapabilityProfile',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('team_name', models.CharField(db_index=True, max_length=64, unique=True)),
                ('capabilities', models.JSONField(blank=True, default=list)),
                ('note', models.TextField(blank=True, default='')),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('updated_by', models.CharField(blank=True, default='', max_length=128)),
            ],
            options={
                'verbose_name': 'Team Capability Profile',
                'verbose_name_plural': 'Team Capability Profiles',
                'db_table': 'tasks_team_capability_profile',
                'ordering': ['team_name'],
            },
        ),
        migrations.RunPython(seed, unseed),
    ]
