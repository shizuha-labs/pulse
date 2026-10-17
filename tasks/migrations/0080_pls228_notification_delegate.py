"""PLS-228: adopt the out-of-band ``tasks_notification_delegate`` table.

The table exists in PROD (created out-of-band) but not in fresh DBs (CI test DBs,
new environments). A plain CreateModel would fail on prod ("table already
exists"); an empty DB op would leave fresh DBs without the table. So we use
SeparateDatabaseAndState: Django's STATE always gains the model, and the DATABASE
op creates the table only if it is absent (fresh DBs get it, prod is a no-op) —
self-healing everywhere with no manual --fake step.
"""
from django.db import migrations, models


TABLE = 'tasks_notification_delegate'


def create_if_absent(apps, schema_editor):
    existing = schema_editor.connection.introspection.table_names()
    if TABLE not in existing:
        model = apps.get_model('tasks', 'NotificationDelegate')
        schema_editor.create_model(model)


def drop_if_present(apps, schema_editor):
    existing = schema_editor.connection.introspection.table_names()
    if TABLE in existing:
        model = apps.get_model('tasks', 'NotificationDelegate')
        schema_editor.delete_model(model)


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0079_id39_user_lifecycle_event_receipt'),
    ]

    operations = [
        # State only: Django's migration state always gains the model + index.
        # (Kept separate from the DB op below because a RunPython nested inside
        # SeparateDatabaseAndState.database_operations receives the *pre-state*
        # ``apps`` registry — which does NOT contain NotificationDelegate — so
        # ``apps.get_model(...)`` would raise LookupError on any fresh DB where
        # the table is absent, e.g. CI test DBs. As a top-level op that follows
        # the state operation, create_if_absent's ``apps`` includes the model.)
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.CreateModel(
                    name='NotificationDelegate',
                    fields=[
                        ('id', models.BigAutoField(auto_created=True, primary_key=True,
                                                   serialize=False, verbose_name='ID')),
                        ('user_id', models.IntegerField(db_index=True,
                                                        help_text='Delegating user ID')),
                        ('delegate_id', models.IntegerField(
                            help_text='User ID that receives the copy')),
                        ('delegate_email', models.CharField(blank=True, default='', max_length=254)),
                        ('delegated_events', models.TextField(blank=True, default='')),
                        ('is_active', models.BooleanField(default=True)),
                    ],
                    options={'db_table': TABLE},
                ),
                migrations.AddIndex(
                    model_name='notificationdelegate',
                    index=models.Index(fields=['user_id', 'is_active'],
                                       name='tasks_notif_user_id_active_idx'),
                ),
            ],
        ),
        # DB only: create the table (+ Meta index) on fresh DBs; no-op on prod
        # where it already exists out-of-band. Runs against the full project
        # state, so apps.get_model('tasks', 'NotificationDelegate') resolves.
        migrations.RunPython(create_if_absent, drop_if_present),
    ]
