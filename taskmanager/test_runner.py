"""PLAT-1358 hermetic Django test runner.

Blocks real outbound sockets during Django's unittest runner while allowing
loopback/UNIX sockets for CI-owned Postgres/Redis sidecars.
"""
from __future__ import annotations

from django.core.management import call_command
from django.test import testcases
from django.test.runner import DiscoverRunner

from scripts.hermetic_socket_guard import socket_guard

# PLS-623: reference data (workflows, statuses, resolutions, transitions,
# schemes) is seeded by migrations, but TransactionTestCase flushes TRUNCATE
# every table — including those reference tables. Under `--keepdb` the retained
# parallel clones are never re-seeded by migrations, so any later test on the
# same clone sees empty reference data: `_auto_attach_workflow()` cannot resolve
# a workflow and new Items violate `task_must_have_workflow_name` (second local
# run fails with the misleading constraint error; CI is immune because every run
# starts with a fresh DB). Fix: after EVERY TransactionTestCase flush, reload the
# committed reference-data fixture. This is global (covers all current and future
# TransactionTestCase classes and every victim test on every parallel clone),
# not a per-class `fixtures = [...]` list that would silently miss the next one.
_REFERENCE_FIXTURE = 'reference_data.json'


def _reset_reference_sequences(database='default'):
    """Advance serial PK sequences after loaddata so tests can insert new rows.

    ``loaddata`` with natural-primary inserts rows without updating PostgreSQL
    sequences for AutoField tables (TeamRoster). Worse, TransactionTestCase
    with ``reset_sequences=True`` forces every sequence back to 1 via
    ``sequence_reset_by_name_sql`` *after* reference rows already occupy
    id=1..N — the next ``TeamRoster.objects.create()`` then collides on id=1.
    Always re-advance from ``max(id)`` after any load or sequence reset.
    """
    from django.apps import apps
    from django.core.management.color import no_style
    from django.db import connections

    conn = connections[database]
    model_labels = (
        'tasks.TeamRoster',
    )
    models = []
    for label in model_labels:
        try:
            models.append(apps.get_model(label))
        except LookupError:
            continue
    if not models:
        return
    style = no_style()
    sql_list = conn.ops.sequence_reset_sql(style, models)
    if not sql_list:
        return
    with conn.cursor() as cursor:
        for sql in sql_list:
            cursor.execute(sql)


def _load_reference_data(database='default'):
    """Load fixture + advance serial sequences on one alias."""
    call_command('loaddata', _REFERENCE_FIXTURE, verbosity=0, database=database)
    _reset_reference_sequences(database)


def _reference_data_flush(self):
    """Wrap TransactionTestCase._fixture_teardown to restore reference data
    after each flush, so retained parallel clones never serve missing
    workflows/statuses to subsequent tests."""
    from django.db import connections

    for db_name in self._databases_names(include_mirrors=False):
        inhibit_post_migrate = (
            self.available_apps is not None
            or (
                self.serialized_rollback
                and hasattr(connections[db_name], '_test_serialized_contents')
            )
        )
        call_command(
            'flush',
            verbosity=0,
            interactive=False,
            database=db_name,
            reset_sequences=False,
            allow_cascade=self.available_apps is not None,
            inhibit_post_migrate=inhibit_post_migrate,
        )
        try:
            _load_reference_data(db_name)
        except Exception:
            # Restore must not mask the test result. A failed restore surfaces
            # as the original task_must_have_workflow_name failure on the next
            # test that needs reference data — which is the right signal.
            pass


_original_fixture_setup = testcases.TransactionTestCase._fixture_setup


def _reference_data_fixture_setup(self):
    """After Django's setup (including reset_sequences=True → setval 1), reload
    reference data and re-advance serial sequences past max(id)."""
    _original_fixture_setup(self)
    for db_name in self._databases_names(include_mirrors=False):
        try:
            _load_reference_data(db_name)
        except Exception:
            pass


testcases.TransactionTestCase._fixture_teardown = _reference_data_flush
testcases.TransactionTestCase._fixture_setup = _reference_data_fixture_setup


def _seed_reference_data(database='default'):
    """Load the committed reference-data fixture into one database alias."""
    _load_reference_data(database)


def _seed_reference_data_into_named_db(db_name):
    """Load the fixture into a physical Postgres database by name.

    Used for retained parallel clones (``test_*_1`` … ``test_*_N``) that already
    exist under ``--keepdb`` and are not registered as Django aliases until a
    worker claims them. We temporarily retarget the default connection.
    """
    from django.db import connections

    conn = connections['default']
    original_name = conn.settings_dict['NAME']
    if original_name == db_name:
        _seed_reference_data('default')
        return
    conn.close()
    conn.settings_dict['NAME'] = db_name
    try:
        _seed_reference_data('default')
    finally:
        conn.close()
        conn.settings_dict['NAME'] = original_name


class HermeticDiscoverRunner(DiscoverRunner):
    def setup_test_environment(self, **kwargs):
        self._socket_guard = socket_guard(allow_loopback=True)
        self._socket_guard.__enter__()
        return super().setup_test_environment(**kwargs)

    def setup_databases(self, **kwargs):
        """Restore reference data into the base test DB AND every retained clone.

        With ``--keepdb`` the previous run's final TransactionTestCase flushes
        leave the base and every parallel clone (``test_*_1`` … ``test_*_N``)
        without workflows/statuses. Parallel workers then claim those empty
        clones via ``setup_worker_connection`` — they are NOT re-cloned from the
        base at worker start when keepdb is on. Seeding only the base therefore
        leaves every clone empty and the second local run still fails.

        Seed the base first, then every existing clone matching the Django
        naming convention, so every worker starts with the reference data that
        ``_auto_attach_workflow`` and other production paths expect. The
        per-flush restore in ``_reference_data_flush`` keeps them populated
        afterwards.
        """
        result = super().setup_databases(**kwargs)
        try:
            from django.db import connections

            base_name = connections['default'].settings_dict['NAME']
            _seed_reference_data('default')

            # Seed every retained parallel clone. With keepdb they already exist
            # and are empty; without keepdb they don't exist yet and we skip.
            parallel = int(getattr(self, 'parallel', 0) or 0)
            for i in range(1, parallel + 1):
                clone_name = f'{base_name}_{i}'
                try:
                    _seed_reference_data_into_named_db(clone_name)
                except Exception as exc:
                    # Clone may not exist yet (first run / no keepdb) — fine.
                    if 'does not exist' not in str(exc).lower():
                        print(
                            f'[PLS-623] setup reference-data load failed on '
                            f'{clone_name}: {type(exc).__name__}: {exc}'
                        )
        except Exception as exc:  # pragma: no cover - surface, never fatal here
            print(f'[PLS-623] setup reference-data load failed: {exc}')
        return result

    def teardown_test_environment(self, **kwargs):
        try:
            return super().teardown_test_environment(**kwargs)
        finally:
            guard = getattr(self, '_socket_guard', None)
            if guard is not None:
                guard.__exit__(None, None, None)
                self._socket_guard = None
