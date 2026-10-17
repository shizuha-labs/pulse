"""
Migration to centralize organization management in shizuha-admin.

This migration:
1. Adds org_id IntegerField to Project (references shizuha-admin orgs)
2. Copies organization FK id values to org_id
3. Removes the organization FK from Project
4. Drops the Organization and OrganizationMembership models

After this migration, shizuha-pulse no longer has its own Organization model.
All organization data is managed by shizuha-admin and accessed via AdminService.
"""

from django.db import migrations, models


def copy_org_ids_forward(apps, schema_editor):
    """Copy organization_id from FK to the new org_id field using raw SQL."""
    from django.db import connection
    with connection.cursor() as cursor:
        # Check if org_id column exists
        cursor.execute("""
            SELECT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'tasks_project' AND column_name = 'org_id'
            )
        """)
        org_id_exists = cursor.fetchone()[0]

        # Check if organization_id column exists
        cursor.execute("""
            SELECT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'tasks_project' AND column_name = 'organization_id'
            )
        """)
        organization_id_exists = cursor.fetchone()[0]

        if org_id_exists and organization_id_exists:
            # Copy values
            cursor.execute("UPDATE tasks_project SET org_id = organization_id WHERE organization_id IS NOT NULL")


def copy_org_ids_reverse(apps, schema_editor):
    """Reverse migration - no-op."""
    pass


def drop_unique_constraint(apps, schema_editor):
    """Drop the unique constraint on (organization_id, key) if it exists."""
    from django.db import connection
    with connection.cursor() as cursor:
        # Find and drop the constraint
        cursor.execute("""
            SELECT constraint_name FROM information_schema.table_constraints
            WHERE table_name = 'tasks_project'
            AND constraint_type = 'UNIQUE'
            AND constraint_name LIKE '%organization%'
        """)
        constraints = cursor.fetchall()
        for (constraint_name,) in constraints:
            cursor.execute(f'ALTER TABLE tasks_project DROP CONSTRAINT IF EXISTS "{constraint_name}"')


def cleanup_organization_tables(apps, schema_editor):
    """Drop Organization and OrganizationMembership tables if they exist."""
    from django.db import connection
    with connection.cursor() as cursor:
        # Drop OrganizationMembership first (has FK to Organization)
        cursor.execute("DROP TABLE IF EXISTS tasks_organizationmembership CASCADE")
        # Drop Organization
        cursor.execute("DROP TABLE IF EXISTS tasks_organization CASCADE")


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0013_allow_null_project_membership_user_id'),
    ]

    operations = [
        # Step 1: Add org_id field if it doesn't exist (database operation)
        # and update Django's state to know about the field
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    sql="""
                        DO $$
                        BEGIN
                            IF NOT EXISTS (
                                SELECT 1 FROM information_schema.columns
                                WHERE table_name = 'tasks_project' AND column_name = 'org_id'
                            ) THEN
                                ALTER TABLE tasks_project ADD COLUMN org_id INTEGER;
                                CREATE INDEX IF NOT EXISTS tasks_project_org_id_idx ON tasks_project(org_id);
                            END IF;
                        END $$;
                    """,
                    reverse_sql="SELECT 1;",  # No-op for reverse
                ),
            ],
            state_operations=[
                migrations.AddField(
                    model_name='project',
                    name='org_id',
                    field=models.IntegerField(default=1, db_index=True, help_text='Organization ID from shizuha-admin'),
                    preserve_default=False,
                ),
            ],
        ),

        # Step 2: Copy organization_id to org_id
        migrations.RunPython(copy_org_ids_forward, copy_org_ids_reverse),

        # Step 3: Set default and make non-nullable
        migrations.RunSQL(
            sql="""
                UPDATE tasks_project SET org_id = 1 WHERE org_id IS NULL;
                ALTER TABLE tasks_project ALTER COLUMN org_id SET NOT NULL;
                ALTER TABLE tasks_project ALTER COLUMN org_id SET DEFAULT 1;
            """,
            reverse_sql="ALTER TABLE tasks_project ALTER COLUMN org_id DROP NOT NULL;",
        ),

        # Step 4: Drop unique constraint on organization_id
        migrations.RunPython(drop_unique_constraint, migrations.RunPython.noop),

        # Step 5: Remove organization_id FK column if it exists
        migrations.RunSQL(
            sql="""
                DO $$
                BEGIN
                    IF EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'tasks_project' AND column_name = 'organization_id'
                    ) THEN
                        ALTER TABLE tasks_project DROP COLUMN organization_id CASCADE;
                    END IF;
                END $$;
            """,
            reverse_sql="SELECT 1;",  # Can't reverse this safely
        ),

        # Step 6: Drop Organization and OrganizationMembership tables
        migrations.RunPython(cleanup_organization_tables, migrations.RunPython.noop),
    ]
