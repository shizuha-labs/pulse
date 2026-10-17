from django.db import migrations, models


def seed_owner_action_policy(apps, schema_editor):
    workflow_status = apps.get_model('tasks', 'WorkflowStatus')
    workflow_status.objects.using(schema_editor.connection.alias).filter(
        workflow__slug='simple', status__slug='resolved_elsewhere',
    ).update(owner_action_policy='awareness')


class Migration(migrations.Migration):
    dependencies = [('tasks', '0176_merge_0175_leaves')]

    operations = [
        migrations.AddField(
            model_name='workflowstatus',
            name='owner_action_policy',
            field=models.CharField(
                max_length=20,
                choices=[('actionable', 'Actionable'), ('awareness', 'Awareness only')],
                default='actionable',
                help_text='Owner action expected in this workflow state; independent of terminality and capacity',
            ),
        ),
        migrations.RunPython(seed_owner_action_policy, migrations.RunPython.noop),
    ]
