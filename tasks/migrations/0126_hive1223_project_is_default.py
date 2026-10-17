from django.db import migrations, models


class Migration(migrations.Migration):
    """HIVE-1223: per-org default project for fresh-org first-task onboarding.

    Additive + safe: adds ``Project.is_default`` (BooleanField, default False, so
    every existing row is a non-default) and a partial-unique constraint enforcing
    at most one default project per org. No existing row has ``is_default=True``, so
    the constraint is immediately satisfiable — no backfill, reversible.
    """

    dependencies = [
        ('tasks', '0125_item_closeout_generation_item_closeout_parent_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='project',
            name='is_default',
            field=models.BooleanField(default=False),
        ),
        # Django 4.2 AddField applies the default to existing rows but leaves the
        # column NOT NULL with no persistent DB-level default. Give it one so any
        # insert that omits is_default still gets false — notably migration-test
        # historical Project models (which predate this field) inserting against a
        # HEAD schema, and any non-ORM insert. Keeps makemigrations --check clean
        # (Django 4.2 doesn't track db-level defaults on the model).
        migrations.RunSQL(
            sql="ALTER TABLE tasks_project ALTER COLUMN is_default SET DEFAULT false;",
            reverse_sql="ALTER TABLE tasks_project ALTER COLUMN is_default DROP DEFAULT;",
        ),
        migrations.AddConstraint(
            model_name='project',
            constraint=models.UniqueConstraint(
                condition=models.Q(('is_default', True)),
                fields=('org_id',),
                name='uniq_default_project_per_org',
            ),
        ),
    ]
