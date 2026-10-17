"""
Unify WorkflowTransition: drop legacy ProjectWorkflow FK, rename jira_workflow → workflow.

This migration consolidates the dual-FK pattern on WorkflowTransition:
- Removes the `workflow` FK (pointing to ProjectWorkflow, legacy)
- Renames `jira_workflow` FK → `workflow` (pointing to Workflow)
- Updates indexes accordingly
"""

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0025_seed_jira_parity_defaults'),
    ]

    operations = [
        # 1. Remove the composite index on the old legacy workflow FK
        migrations.RemoveIndex(
            model_name='workflowtransition',
            name='tasks_workf_workflo_8d0be1_idx',
        ),

        # 2. Remove the old composite index on jira_workflow FK
        migrations.RemoveIndex(
            model_name='workflowtransition',
            name='tasks_workf_jira_wo_92a7b2_idx',
        ),

        # 3. Remove the legacy `workflow` FK (to ProjectWorkflow)
        migrations.RemoveField(
            model_name='workflowtransition',
            name='workflow',
        ),

        # 4. Rename `jira_workflow` → `workflow` (now points to Workflow model)
        migrations.RenameField(
            model_name='workflowtransition',
            old_name='jira_workflow',
            new_name='workflow',
        ),

        # 5. Add new composite index with the renamed field
        migrations.AddIndex(
            model_name='workflowtransition',
            index=models.Index(fields=['workflow', 'from_status'], name='tasks_wft_workflow_status_idx'),
        ),
    ]
