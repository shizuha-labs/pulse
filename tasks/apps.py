import logging as _logging

from django.apps import AppConfig


def _apply_substrate_cap_mode_guc(sender, connection, **kwargs):
    """HIVE-1153 P1 #3 (review P1 #2 / re-review P1 #1): install the
    pulse.substrate_cap_mode SESSION GUC on EVERY postgres connection from the SAME
    validated env switch, so PULSE_SUBSTRATE_CAP_MODE=enforce activates the writer
    floor for ALL writers (ORM save, QuerySet.update, raw SQL) with no second knob.

    Module-level (not a ready() closure) so the fail-closed lifecycle is directly
    testable. Under ENFORCE a set_config failure FAILS CLOSED — the connection is
    closed and the error re-raised so acquisition aborts, because the guard triggers
    no-op on an unset GUC and would otherwise hand back an unprotected enforce
    session. off/shadow have no enforcement to lose and may degrade to a warning.
    """
    if getattr(connection, 'vendor', None) != 'postgresql':
        return
    from tasks.services.capability_evidence import (
        get_substrate_cap_mode, MODE_ENFORCE)
    # An invalid/dual switch raises here and MUST abort connection acquisition —
    # never operate with an ambiguous security switch.
    mode = get_substrate_cap_mode()
    try:
        with connection.cursor() as cursor:
            # is_local=false -> session scope: persists for the whole connection
            # so QuerySet.update / raw SQL on it also see it.
            cursor.execute(
                "SELECT set_config('pulse.substrate_cap_mode', %s, false)", [mode])
    except Exception:
        if mode == MODE_ENFORCE:
            try:
                connection.close()
            except Exception:
                pass
            raise
        _logging.getLogger(__name__).warning(
            'PLAT-4732 failed to set substrate_cap_mode session GUC (mode=%s)',
            mode, exc_info=True)


class TasksConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'tasks'

    def ready(self):
        import tasks.signals  # Import signals

        # HIVE-1153 P1 #3 (review P1 #2): install the pulse.substrate_cap_mode
        # SESSION GUC on every postgres connection from the sole env switch (the
        # module-level _apply_substrate_cap_mode_guc handles fail-closed-under-
        # enforce; see its docstring).
        from django.db.backends.signals import connection_created
        connection_created.connect(
            _apply_substrate_cap_mode_guc,
            dispatch_uid='hive1153_substrate_cap_mode_guc')

        # HIVE-1153 P2 #2: validate the substrate-capability activation switch
        # LOUD at boot, not lazily at first routing. An unknown value or the
        # retired dual PULSE_SUBSTRATE_CAP_ENFORCE aborts startup (fail closed —
        # never operate with an ambiguous security switch); an explicitly set
        # off/shadow/enforce boots cleanly. Skipped only
        # when explicitly opted out for tooling that must not import the service.
        import os
        if os.environ.get('PULSE_SKIP_SUBSTRATE_CAP_BOOT_CHECK', '').strip().lower() not in ('1', 'true', 'yes'):
            import logging
            from tasks.services.capability_evidence import get_substrate_cap_mode
            mode = get_substrate_cap_mode()  # raises SubstrateCapModeError -> abort boot
            logging.getLogger(__name__).info(
                'PLAT-4732 substrate_cap_mode=%s (validated at boot)', mode)

        # The isolated readiness listener must stay a read-only probe process.
        # In particular it must not contend on django-celery-beat rows while
        # booting: every public web/worker process already reconciles these
        # schedules, and probe_settings explicitly disables this bootstrap.
        from django.conf import settings
        if getattr(settings, 'PULSE_BOOTSTRAP_PERIODIC_SCHEDULES', True):
            # Setup periodic cleanup tasks
            try:
                from tasks.cleanup_tasks import setup_task_cleanup_schedule
                setup_task_cleanup_schedule()
            except Exception as e:
                # Ignore errors during setup (e.g., database not ready yet)
                import logging
                logger = logging.getLogger(__name__)
                logger.warning(f"Could not setup task cleanup schedule: {e}")

            # Setup notification schedules
            try:
                from tasks.tasks import setup_notification_schedules
                setup_notification_schedules()
            except Exception as e:
                import logging
                logger = logging.getLogger(__name__)
                logger.warning(f"Could not setup notification schedules: {e}")

            # PLS-1286: recurring-cycle lane watchdog (hourly beat)
            try:
                from tasks.check_recurring_lane_liveness import (
                    setup_recurring_lane_watchdog_schedule,
                )
                setup_recurring_lane_watchdog_schedule()
            except Exception as e:
                import logging
                logger = logging.getLogger(__name__)
                logger.warning(f"Could not setup recurring-cycle lane watchdog schedule: {e}")
