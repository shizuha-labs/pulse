"""Refuse every new entry into waiting_external.

The status stayed non-terminal and did not require a parent blocker, so
escalation children became chain roots nobody could work. Blocked plus an
owned blocker is the only hold. Existing parked rows can still leave
through a typed release; they cannot be created or re-entered.
"""

from django.db import migrations


POSTGRES_REFUSE_ENTRY_SQL = r"""
CREATE OR REPLACE FUNCTION tasks_guard_external_wait_status()
RETURNS trigger AS $$
BEGIN
    IF NEW.status IS DISTINCT FROM OLD.status
       AND NEW.status = 'waiting_external' THEN
        RAISE EXCEPTION
            'waiting_external is retired (operator 2026-09-26)'
            USING ERRCODE = '23514';
    END IF;
    IF OLD.status = 'waiting_external'
       AND NEW.status = 'waiting_external'
       AND NEW.external_wait_generation IS DISTINCT FROM OLD.external_wait_generation THEN
        RAISE EXCEPTION
            'waiting_external generation is immutable while parked (PLS-733)'
            USING ERRCODE = '23514';
    END IF;
    IF OLD.status = 'waiting_external'
       AND NEW.status IS DISTINCT FROM 'waiting_external'
       AND NOT EXISTS (
           SELECT 1 FROM tasks_external_wait_authority a
            WHERE a.item_id = NEW.id
              AND a.wait_generation = OLD.external_wait_generation
              AND a.state = 'exit_armed'
       ) THEN
        RAISE EXCEPTION
            'waiting_external requires typed release (PLS-733)'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION tasks_guard_external_wait_insert()
RETURNS trigger AS $$
BEGIN
    IF NEW.status = 'waiting_external' THEN
        RAISE EXCEPTION
            'waiting_external is retired (operator 2026-09-26)'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


SQLITE_REFUSE_ENTRY_SQL = r"""
DROP TRIGGER IF EXISTS tasks_external_wait_insert_guard;
CREATE TRIGGER tasks_external_wait_insert_guard
BEFORE INSERT ON tasks_task
FOR EACH ROW
WHEN NEW.status = 'waiting_external'
BEGIN
    SELECT RAISE(ABORT, 'waiting_external is retired (operator 2026-09-26)');
END;

DROP TRIGGER IF EXISTS tasks_external_wait_entry_guard;
CREATE TRIGGER tasks_external_wait_entry_guard
BEFORE UPDATE OF status, external_wait_generation ON tasks_task
FOR EACH ROW
WHEN NEW.status = 'waiting_external'
 AND OLD.status <> 'waiting_external'
BEGIN
    SELECT RAISE(ABORT, 'waiting_external is retired (operator 2026-09-26)');
END;
"""


def install_retired_entry_guard(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor == 'postgresql':
        schema_editor.execute(POSTGRES_REFUSE_ENTRY_SQL)
    elif vendor == 'sqlite':
        schema_editor.connection.connection.executescript(SQLITE_REFUSE_ENTRY_SQL)


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0187_item_status_window_index'),
    ]

    operations = [
        migrations.RunPython(install_retired_entry_guard, migrations.RunPython.noop),
    ]
