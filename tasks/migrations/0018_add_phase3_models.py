# Generated migration for Phase 3 advanced features
import uuid
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0017_add_sprint_model'),
    ]

    operations = [
        # Component model
        migrations.CreateModel(
            name='Component',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(help_text='Component name (e.g., Frontend, API)', max_length=255)),
                ('description', models.TextField(blank=True, help_text='Component description')),
                ('lead_id', models.IntegerField(blank=True, help_text='User ID of the component lead', null=True)),
                ('lead_email', models.EmailField(blank=True, help_text='Email of the component lead', max_length=254, null=True)),
                ('default_assignee_id', models.IntegerField(blank=True, help_text='Default user ID for items in this component', null=True)),
                ('default_assignee_email', models.EmailField(blank=True, help_text='Default email for items in this component', max_length=254, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('project', models.ForeignKey(help_text='Project this component belongs to', on_delete=django.db.models.deletion.CASCADE, related_name='components', to='tasks.project')),
            ],
            options={
                'db_table': 'tasks_component',
                'ordering': ['project', 'name'],
            },
        ),
        migrations.AddIndex(
            model_name='component',
            index=models.Index(fields=['project'], name='tasks_compo_project_3e5f4a_idx'),
        ),
        migrations.AddIndex(
            model_name='component',
            index=models.Index(fields=['lead_id'], name='tasks_compo_lead_id_8f2c1b_idx'),
        ),
        migrations.AddConstraint(
            model_name='component',
            constraint=models.UniqueConstraint(fields=('project', 'name'), name='unique_component_name_per_project'),
        ),

        # Version model
        migrations.CreateModel(
            name='Version',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(help_text='Version name (e.g., 1.0.0, 2.0-beta)', max_length=255)),
                ('description', models.TextField(blank=True, help_text='Version description')),
                ('status', models.CharField(choices=[('planning', 'Planning'), ('active', 'Active'), ('released', 'Released'), ('archived', 'Archived')], default='planning', help_text='Version status', max_length=20)),
                ('start_date', models.DateField(blank=True, help_text='Planned start date', null=True)),
                ('release_date', models.DateField(blank=True, help_text='Planned or actual release date', null=True)),
                ('released_at', models.DateTimeField(blank=True, help_text='Actual release timestamp', null=True)),
                ('sequence', models.IntegerField(default=0, help_text='Order in version list (lower = earlier)')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('project', models.ForeignKey(help_text='Project this version belongs to', on_delete=django.db.models.deletion.CASCADE, related_name='versions', to='tasks.project')),
            ],
            options={
                'db_table': 'tasks_version',
                'ordering': ['project', 'sequence', 'name'],
            },
        ),
        migrations.AddIndex(
            model_name='version',
            index=models.Index(fields=['project', 'status'], name='tasks_versi_project_a1b2c3_idx'),
        ),
        migrations.AddIndex(
            model_name='version',
            index=models.Index(fields=['release_date'], name='tasks_versi_release_d4e5f6_idx'),
        ),
        migrations.AddConstraint(
            model_name='version',
            constraint=models.UniqueConstraint(fields=('project', 'name'), name='unique_version_name_per_project'),
        ),

        # WorkLog model
        migrations.CreateModel(
            name='WorkLog',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('user_id', models.IntegerField(db_index=True, help_text='User ID who logged the work')),
                ('user_email', models.EmailField(help_text='Email of user who logged the work', max_length=254)),
                ('duration_minutes', models.PositiveIntegerField(help_text='Time spent in minutes')),
                ('worked_date', models.DateField(help_text='Date the work was performed')),
                ('description', models.TextField(blank=True, help_text='Description of work performed')),
                ('remaining_estimate_minutes', models.PositiveIntegerField(blank=True, help_text='New remaining estimate after this work log', null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('item', models.ForeignKey(help_text='Item this work log belongs to', on_delete=django.db.models.deletion.CASCADE, related_name='work_logs', to='tasks.item')),
            ],
            options={
                'db_table': 'tasks_work_log',
                'ordering': ['-worked_date', '-created_at'],
            },
        ),
        migrations.AddIndex(
            model_name='worklog',
            index=models.Index(fields=['item', 'worked_date'], name='tasks_workl_item_id_g7h8i9_idx'),
        ),
        migrations.AddIndex(
            model_name='worklog',
            index=models.Index(fields=['user_id'], name='tasks_workl_user_id_j0k1l2_idx'),
        ),

        # ProjectWorkflow model
        migrations.CreateModel(
            name='ProjectWorkflow',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('statuses', models.JSONField(default=list, help_text='List of workflow statuses with metadata')),
                ('initial_status', models.CharField(default='pending', help_text='Default status for new items', max_length=50)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('project', models.OneToOneField(help_text='Project this workflow belongs to', on_delete=django.db.models.deletion.CASCADE, related_name='workflow', to='tasks.project')),
            ],
            options={
                'db_table': 'tasks_project_workflow',
                'verbose_name': 'Project Workflow',
                'verbose_name_plural': 'Project Workflows',
            },
        ),

        # WorkflowTransition model
        migrations.CreateModel(
            name='WorkflowTransition',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('name', models.CharField(help_text="Transition name (e.g., 'Start Progress', 'Complete')", max_length=100)),
                ('from_status', models.CharField(help_text='Source status (* for any)', max_length=50)),
                ('to_status', models.CharField(help_text='Target status', max_length=50)),
                ('conditions', models.JSONField(blank=True, help_text='JSON Logic conditions for transition', null=True)),
                ('required_fields', models.JSONField(blank=True, default=list, help_text='Fields required for this transition')),
                ('screen_fields', models.JSONField(blank=True, default=list, help_text='Fields to show in transition dialog')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('workflow', models.ForeignKey(help_text='Workflow this transition belongs to', on_delete=django.db.models.deletion.CASCADE, related_name='transitions', to='tasks.projectworkflow')),
            ],
            options={
                'db_table': 'tasks_workflow_transition',
                'ordering': ['workflow', 'from_status', 'to_status'],
            },
        ),
        migrations.AddIndex(
            model_name='workflowtransition',
            index=models.Index(fields=['workflow', 'from_status'], name='tasks_workf_workflo_m3n4o5_idx'),
        ),

        # BoardColumn model
        migrations.CreateModel(
            name='BoardColumn',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('name', models.CharField(help_text='Column display name', max_length=100)),
                ('status_mapping', models.JSONField(default=list, help_text='List of statuses that map to this column')),
                ('position', models.IntegerField(default=0, help_text='Column position (0 = leftmost)')),
                ('wip_limit', models.PositiveIntegerField(blank=True, help_text='Maximum items allowed in this column', null=True)),
                ('color', models.CharField(default='#6b7280', help_text='Column header color (hex)', max_length=7)),
                ('is_collapsed', models.BooleanField(default=False, help_text='Whether column is collapsed by default')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('project', models.ForeignKey(help_text='Project this column belongs to', on_delete=django.db.models.deletion.CASCADE, related_name='board_columns', to='tasks.project')),
            ],
            options={
                'db_table': 'tasks_board_column',
                'ordering': ['project', 'position'],
            },
        ),
        migrations.AddIndex(
            model_name='boardcolumn',
            index=models.Index(fields=['project', 'position'], name='tasks_board_project_p6q7r8_idx'),
        ),

        # SavedFilter model
        migrations.CreateModel(
            name='SavedFilter',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('name', models.CharField(help_text='Filter name', max_length=255)),
                ('description', models.TextField(blank=True, help_text='Filter description')),
                ('jql', models.TextField(help_text='JQL query string')),
                ('user_id', models.IntegerField(db_index=True, help_text='User ID who created the filter')),
                ('user_email', models.EmailField(help_text='Email of filter owner', max_length=254)),
                ('is_shared', models.BooleanField(default=False, help_text='Whether filter is shared with team')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('project', models.ForeignKey(blank=True, help_text='Project scope (null = global)', null=True, on_delete=django.db.models.deletion.CASCADE, related_name='saved_filters', to='tasks.project')),
            ],
            options={
                'db_table': 'tasks_saved_filter',
                'ordering': ['user_id', 'name'],
            },
        ),
        migrations.AddIndex(
            model_name='savedfilter',
            index=models.Index(fields=['user_id'], name='tasks_saved_user_id_s9t0u1_idx'),
        ),
        migrations.AddIndex(
            model_name='savedfilter',
            index=models.Index(fields=['project', 'is_shared'], name='tasks_saved_project_v2w3x4_idx'),
        ),

        # Add new fields to Item model
        migrations.AddField(
            model_name='item',
            name='original_estimate_minutes',
            field=models.PositiveIntegerField(blank=True, help_text='Original time estimate in minutes', null=True),
        ),
        migrations.AddField(
            model_name='item',
            name='time_spent_minutes',
            field=models.PositiveIntegerField(default=0, help_text='Total time spent in minutes (auto-calculated from work logs)'),
        ),
        migrations.AddField(
            model_name='item',
            name='remaining_estimate_minutes',
            field=models.PositiveIntegerField(blank=True, help_text='Remaining time estimate in minutes', null=True),
        ),
        migrations.AddField(
            model_name='item',
            name='component',
            field=models.ForeignKey(blank=True, help_text='Component this item belongs to', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='items', to='tasks.component'),
        ),
        migrations.AddField(
            model_name='item',
            name='fix_version',
            field=models.ForeignKey(blank=True, help_text='Version where this item will be fixed', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='fix_items', to='tasks.version'),
        ),
        migrations.AddField(
            model_name='item',
            name='affects_versions',
            field=models.ManyToManyField(blank=True, help_text='Versions affected by this item', related_name='affected_items', to='tasks.version'),
        ),

        # Add indexes for new Item fields
        migrations.AddIndex(
            model_name='item',
            index=models.Index(fields=['component'], name='tasks_task_compone_y5z6a7_idx'),
        ),
        migrations.AddIndex(
            model_name='item',
            index=models.Index(fields=['fix_version'], name='tasks_task_fix_ver_b8c9d0_idx'),
        ),
    ]
