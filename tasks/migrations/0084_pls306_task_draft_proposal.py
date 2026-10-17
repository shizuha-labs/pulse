"""PLS-306: Add TaskDraftProposal model for AI work intake pipeline.

Stores natural-language → structured task draft proposals before confirmation.
The pipeline: parse prompt → retrieve context → search duplicates →
propose fields → validate → confirm/reject lifecycle.
"""
import uuid
from django.db import migrations, models


class Migration(migrations.Migration):
    """Add TaskDraftProposal model for AI work intake (PLS-306 / PLS-270 Slice C)."""

    dependencies = [
        ('tasks', '0083_pls240_reopen_terminal_review'),
    ]

    operations = [
        migrations.CreateModel(
            name='TaskDraftProposal',
            fields=[
                ('id', models.UUIDField(
                    primary_key=True, default=uuid.uuid4, editable=False,
                )),
                ('org_id', models.IntegerField(db_index=True)),
                ('requester_id', models.IntegerField(null=True, blank=True)),
                ('requester_email', models.EmailField(null=True, blank=True)),
                ('source', models.CharField(
                    max_length=50, default='mcp',
                    help_text="Source: 'mcp', 'api', 'ui'",
                )),
                ('raw_prompt', models.TextField(
                    help_text='Original natural-language prompt',
                )),
                ('proposed_project_id', models.IntegerField(null=True, blank=True)),
                ('proposed_workflow_slug', models.CharField(max_length=100, blank=True)),
                ('proposed_title', models.CharField(max_length=500, blank=True)),
                ('proposed_description', models.TextField(blank=True)),
                ('proposed_priority', models.CharField(max_length=20, blank=True)),
                ('proposed_assignment_group', models.CharField(max_length=100, blank=True)),
                ('proposed_labels', models.JSONField(default=list, blank=True)),
                ('proposed_tags', models.JSONField(default=list, blank=True)),
                ('proposed_parent_item_id', models.CharField(max_length=50, blank=True)),
                ('duplicate_candidates', models.JSONField(
                    default=list, blank=True,
                    help_text='List of {task_key, title, status, score} for likely duplicates',
                )),
                ('validation_status', models.CharField(
                    max_length=30, default='pending',
                    choices=[
                        ('pending', 'Pending'),
                        ('valid', 'Valid'),
                        ('invalid', 'Invalid - missing required fields'),
                        ('duplicate', 'Duplicate found - not created'),
                    ],
                )),
                ('validation_errors', models.JSONField(default=list, blank=True)),
                ('prompt_hash', models.CharField(
                    max_length=64, blank=True,
                    help_text='SHA-256 of raw_prompt',
                )),
                ('model_used', models.CharField(max_length=100, blank=True)),
                ('pipeline_version', models.CharField(max_length=20, default='1.0')),
                ('confirmed_item_id', models.CharField(
                    max_length=50, null=True, blank=True,
                    help_text='Item key (e.g., PROJ-123) of the created task',
                )),
                ('confirmed_at', models.DateTimeField(null=True, blank=True)),
                ('rejected_at', models.DateTimeField(null=True, blank=True)),
                ('rejected_reason', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'db_table': 'tasks_taskdraftproposal',
                'ordering': ['-created_at'],
                'indexes': [
                    models.Index(fields=['org_id', 'validation_status'], name='taskdraft_org_valstatus_idx'),
                    models.Index(fields=['requester_id'], name='taskdraft_requester_idx'),
                ],
            },
        ),
    ]
