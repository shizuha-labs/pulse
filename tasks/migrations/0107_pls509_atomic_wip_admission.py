from django.db import migrations, models
import django.db.models.deletion
import uuid


PLATFORM_AGENT_TEAMS = (
    'architecture', 'design', 'devops', 'documentation', 'engineering', 'eval',
    'merge', 'product', 'qa', 'research-analytics', 'review', 'security',
)


def enable_platform_wip(apps, schema_editor):
    TeamRoster = apps.get_model('tasks', 'TeamRoster')
    TeamRoster.objects.filter(
        organization_slug='shizuha',
        team_name__in=PLATFORM_AGENT_TEAMS,
        is_active=True,
    ).update(wip_enforced=True, active_wip_cap=1, ready_wip_cap=5)


def disable_platform_wip(apps, schema_editor):
    TeamRoster = apps.get_model('tasks', 'TeamRoster')
    TeamRoster.objects.filter(
        organization_slug='shizuha',
        team_name__in=PLATFORM_AGENT_TEAMS,
    ).update(wip_enforced=False)


class Migration(migrations.Migration):
    dependencies = [('tasks', '0106_backfill_org_safe_condition_capsules')]

    operations = [
        migrations.AddField(
            model_name='teamroster',
            name='active_wip_cap',
            field=models.PositiveSmallIntegerField(default=1),
        ),
        migrations.AddField(
            model_name='teamroster',
            name='ready_wip_cap',
            field=models.PositiveSmallIntegerField(default=5),
        ),
        migrations.AddField(
            model_name='teamroster',
            name='wip_enforced',
            field=models.BooleanField(default=False),
        ),
        # Keep database defaults after Django's AddField operation. Historical
        # migration-state models (used by data migrations and their regression
        # tests) do not know about fields added later, so their INSERTs omit
        # these columns. PostgreSQL must still be able to accept those writes.
        migrations.RunSQL(
            sql=(
                'ALTER TABLE tasks_team_roster '
                'ALTER COLUMN active_wip_cap SET DEFAULT 1; '
                'ALTER TABLE tasks_team_roster '
                'ALTER COLUMN ready_wip_cap SET DEFAULT 5; '
                'ALTER TABLE tasks_team_roster '
                'ALTER COLUMN wip_enforced SET DEFAULT FALSE;'
            ),
            reverse_sql=(
                'ALTER TABLE tasks_team_roster '
                'ALTER COLUMN active_wip_cap DROP DEFAULT; '
                'ALTER TABLE tasks_team_roster '
                'ALTER COLUMN ready_wip_cap DROP DEFAULT; '
                'ALTER TABLE tasks_team_roster '
                'ALTER COLUMN wip_enforced DROP DEFAULT;'
            ),
        ),
        migrations.CreateModel(
            name='AgentWipCapacity',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('organization_slug', models.CharField(default='shizuha', max_length=64)),
                ('assignee_email', models.EmailField(max_length=254)),
                ('assignee_id', models.IntegerField(blank=True, null=True)),
                ('generation', models.PositiveBigIntegerField(default=0)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
        ),
        migrations.CreateModel(
            name='WipSlotReservation',
            fields=[
                ('token', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('assignment_group', models.CharField(max_length=64)),
                ('slot_kind', models.CharField(choices=[('ready', 'Ready'), ('active', 'Active')], max_length=12)),
                ('assignment_generation', models.PositiveBigIntegerField()),
                ('expires_at', models.DateTimeField(db_index=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('capacity', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='reservations', to='tasks.agentwipcapacity')),
                ('item', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='wip_reservations', to='tasks.item')),
            ],
        ),
        migrations.AddConstraint(
            model_name='agentwipcapacity',
            constraint=models.UniqueConstraint(fields=('organization_slug', 'assignee_email'), name='uniq_agent_wip_capacity'),
        ),
        migrations.AddConstraint(
            model_name='wipslotreservation',
            constraint=models.UniqueConstraint(condition=models.Q(('item__isnull', False)), fields=('item', 'assignment_generation'), name='uniq_item_wip_generation'),
        ),
        migrations.RunPython(enable_platform_wip, disable_platform_wip),
    ]
