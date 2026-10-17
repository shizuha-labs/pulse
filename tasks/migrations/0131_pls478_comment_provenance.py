"""PLS-478: additive, fail-closed provenance for mechanical comments."""
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('tasks', '0130_publicationattempt_publicationbinding_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='comment',
            name='origin_kind',
            field=models.CharField(
                choices=[
                    ('USER_AUTHORED', 'User authored'),
                    ('WORKFLOW_LIFECYCLE', 'Workflow lifecycle'),
                    ('VALIDATOR_AUDIT', 'Validator audit'),
                    ('RESOLVER_AUDIT', 'Resolver audit'),
                    ('LEGACY_UNTYPED', 'Legacy untyped'),
                ],
                db_index=True,
                default='LEGACY_UNTYPED',
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name='comment',
            name='origin_event',
            field=models.CharField(blank=True, default='', max_length=96),
        ),
        migrations.AddField(
            model_name='comment',
            name='template_version',
            field=models.PositiveSmallIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='comment',
            name='origin_event_id',
            field=models.CharField(
                blank=True,
                help_text='Stable durable-domain event identity; reused on retry/replay',
                max_length=255,
                null=True,
                unique=True,
            ),
        ),
        migrations.AddField(
            model_name='comment',
            name='origin_payload',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name='comment',
            name='caused_by_user_id',
            field=models.IntegerField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name='comment',
            name='caused_by_email_snapshot',
            field=models.EmailField(blank=True, default='', max_length=254),
        ),
        # Historical RunPython migrations use their pre-0131 Comment model and
        # therefore omit these columns when migration regression tests replay
        # the functions against the current schema. Keep database defaults as
        # the legacy fail-closed boundary rather than permitting NULLs.
        migrations.RunSQL(
            sql=(
                "ALTER TABLE tasks_comment "
                "ALTER COLUMN origin_kind SET DEFAULT 'LEGACY_UNTYPED', "
                "ALTER COLUMN origin_event SET DEFAULT '', "
                "ALTER COLUMN origin_payload SET DEFAULT '{}'::jsonb, "
                "ALTER COLUMN caused_by_email_snapshot SET DEFAULT '';"
            ),
            reverse_sql=(
                "ALTER TABLE tasks_comment "
                "ALTER COLUMN origin_kind DROP DEFAULT, "
                "ALTER COLUMN origin_event DROP DEFAULT, "
                "ALTER COLUMN origin_payload DROP DEFAULT, "
                "ALTER COLUMN caused_by_email_snapshot DROP DEFAULT;"
            ),
        ),
    ]
