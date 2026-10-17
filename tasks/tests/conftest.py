"""Seat/test-harness sqlite compatibility for the tasks test suite.

The CI gate runs the backend suite on Postgres (job-local pg_ctlcluster).
Seat-side verification runs on the sqlite backend (taskmanager.settings_test),
where Postgres-only `DO $$` RunSQL blocks (migration 0014) cannot execute.
This conftest no-ops exactly those blocks before the migration loader imports
them; the state operations still apply. On Postgres the patch is a no-op
(no `DO $$` in the SQL → untouched), so CI behavior is unchanged.

Migration 0014's RunSQL creates tasks_project.org_id when missing; with the
block no-op'd on sqlite, tests that filter on project__org_id create the
column themselves (see test_plat9181_rebalance_identity_dedup.setUpClass).
"""

from django.db import migrations as _dj_migrations

_orig_run_sql_init = _dj_migrations.RunSQL.__init__


def _postgres_only(sql) -> bool:
    return isinstance(sql, str) and ('DO $$' in sql or 'information_schema' in sql)


def _sqlite_safe_run_sql_init(self, sql, reverse_sql=None, *args, **kwargs):
    if _postgres_only(sql):
        sql = _dj_migrations.RunSQL.noop
    if _postgres_only(reverse_sql):
        reverse_sql = _dj_migrations.RunSQL.noop
    _orig_run_sql_init(self, sql, reverse_sql, *args, **kwargs)


_dj_migrations.RunSQL.__init__ = _sqlite_safe_run_sql_init

_orig_run_sql_db_fwd = _dj_migrations.RunSQL.database_forwards
_orig_run_sql_db_bwd = _dj_migrations.RunSQL.database_backwards


def _ensure_sqlite_org_id_column(schema_editor):
    """Migration 0014's non-DO RunSQL statements reference tasks_project.org_id
    (UPDATE backfill / CREATE INDEX). On sqlite the DO block that would create
    the column is no-op'd, so create it here before those statements run."""
    with schema_editor.connection.cursor() as cur:
        cols = {
            row[0]
            for row in schema_editor.connection.introspection.get_table_description(
                cur, 'tasks_project')
        }
        if 'org_id' not in cols:
            cur.execute('ALTER TABLE tasks_project ADD COLUMN org_id INTEGER')


def _sqlite_safe_db_fwd(self, app_label, schema_editor, *a, **k):
    if (
        schema_editor.connection.vendor == 'sqlite'
        and isinstance(self.sql, str)
        and 'org_id' in self.sql
    ):
        _ensure_sqlite_org_id_column(schema_editor)
    return _orig_run_sql_db_fwd(self, app_label, schema_editor, *a, **k)


def _sqlite_safe_db_bwd(self, app_label, schema_editor, *a, **k):
    if (
        schema_editor.connection.vendor == 'sqlite'
        and isinstance(self.reverse_sql, str)
        and 'org_id' in self.reverse_sql
    ):
        _ensure_sqlite_org_id_column(schema_editor)
    return _orig_run_sql_db_bwd(self, app_label, schema_editor, *a, **k)


_dj_migrations.RunSQL.database_forwards = _sqlite_safe_db_fwd
_dj_migrations.RunSQL.database_backwards = _sqlite_safe_db_bwd


_orig_run_python_init = _dj_migrations.RunPython.__init__


def _sqlite_safe_run_python_init(self, code, reverse_code=None, *args, **kwargs):
    """Wrap RunPython data functions so they no-op on the sqlite harness —
    several data migrations use Postgres-catalog (information_schema) raw
    SQL internally. On Postgres the wrapper is transparent."""

    def _vendor_guard(apps, schema_editor):
        if getattr(getattr(schema_editor, 'connection', None), 'vendor', '') == 'sqlite':
            return
        return code(apps, schema_editor)

    def _vendor_guard_reverse(apps, schema_editor):
        if reverse_code is None or reverse_code is _dj_migrations.RunPython.noop:
            return reverse_code
        if getattr(getattr(schema_editor, 'connection', None), 'vendor', '') == 'sqlite':
            return
        return reverse_code(apps, schema_editor)

    _orig_run_python_init(self, _vendor_guard, _vendor_guard_reverse, *args, **kwargs)


_dj_migrations.RunPython.__init__ = _sqlite_safe_run_python_init
