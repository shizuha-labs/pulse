from datetime import datetime, timedelta
from unittest import mock

from celery import current_app
from django.test import TestCase, override_settings
from django.utils import timezone as dj_timezone
from django_celery_beat.models import PeriodicTask
from django_celery_beat.schedulers import DatabaseScheduler, ModelEntry

from tasks.tasks import ensure_periodic_task, setup_notification_schedules


class PeriodicTaskScheduleTests(TestCase):
    def test_setup_notification_schedules_crontab_cadences(self):
        """PLS-38: code-defined crontab values must not silently regress.

        PLS-32 happened because a runtime DB patch was overwritten on pod restart
        by setup_notification_schedules(). This test makes the code-defined
        cadence explicit and catches typos like `11,41` instead of `*/10`.
        """
        PeriodicTask.objects.all().delete()

        setup_notification_schedules()

        expected = {
            "Sweep Rebalance Pods": {"minute": "*/10", "hour": "*"},
            "Probe WIP Admission Health": {"minute": "*/10", "hour": "*"},
            "Sweep Org Health": {"minute": "*/10", "hour": "*"},
            "Sweep Agent Availability": {"minute": "*/3", "hour": "*"},
            "Sweep Resolved Blocked Items": {"minute": "*/5", "hour": "*"},
            "Sweep Active Blocker Links": {"minute": "*/5", "hour": "*"},
            "Sweep Stale Reviews": {"minute": "23", "hour": "*"},
            "Sweep Design Handoff": {"minute": "41", "hour": "*"},
            # "Sweep Archive Done Items" moved off crontab in 92810d7 — it now runs
            # on a 15-minute interval with a working age gate, and deliberately
            # clears its crontab so the old daily 03:17 slot cannot linger. Its
            # cadence stays pinned below, just against the interval schedule.
            # PLAT-724: every 30 min backstop for unassigned human-ops escalations
            "Sweep Stale Human-Ops Blockers": {"minute": "13,43", "hour": "*"},
            # PLAT-948: weekly retro prompt — Friday 11:30 UTC (17:00 IST)
            "Post Weekly Retro Prompt": {"minute": "30", "hour": "11"},
        }

        for name, cadence in expected.items():
            with self.subTest(periodic_task=name):
                task = PeriodicTask.objects.select_related("crontab").get(name=name)
                self.assertIsNotNone(task.crontab, f"{name} must use a crontab schedule")
                self.assertEqual(task.crontab.minute, cadence["minute"])
                self.assertEqual(task.crontab.hour, cadence["hour"])

        probe = PeriodicTask.objects.get(name="Probe WIP Admission Health")
        self.assertTrue(probe.enabled)
        self.assertEqual(probe.task, "tasks.tasks.probe_wip_admission_health")

        # PLS-38 still applies to the archive sweep, against its interval schedule:
        # pin BOTH that the old daily crontab is gone and that 15m is code-defined,
        # so a future edit cannot silently regress it back to once a day.
        archive = PeriodicTask.objects.select_related("interval", "crontab").get(
            name="Sweep Archive Done Items"
        )
        self.assertTrue(archive.enabled)
        self.assertEqual(archive.task, "tasks.tasks.sweep_archive_done_items")
        self.assertIsNone(archive.crontab)
        self.assertIsNotNone(archive.interval)
        self.assertEqual(archive.interval.every, 15)
        self.assertEqual(archive.interval.period, "minutes")

    def test_escalation_routing_outbox_sweep_is_scheduled(self):
        """PLAT-4609: committed outbox rows must recover if on_commit enqueue dies."""
        PeriodicTask.objects.all().delete()

        setup_notification_schedules()

        task = PeriodicTask.objects.select_related("interval").get(
            name="Sweep Escalation Routing Outbox"
        )
        self.assertTrue(task.enabled)
        self.assertEqual(task.task, "tasks.tasks.sweep_escalation_routing_outbox")
        self.assertIsNotNone(task.interval)
        self.assertEqual(task.interval.every, 1)
        self.assertEqual(task.interval.period, "minutes")

    def test_deployment_gate_outbox_sweep_is_scheduled(self):
        """PLS-521: committed gate outbox rows recover after enqueue loss."""
        PeriodicTask.objects.all().delete()

        setup_notification_schedules()

        task = PeriodicTask.objects.select_related("interval").get(
            name="Sweep Deployment Gate Outbox"
        )
        self.assertTrue(task.enabled)
        self.assertEqual(task.task, "tasks.tasks.sweep_deployment_gate_outbox")
        self.assertIsNotNone(task.interval)
        self.assertEqual(task.interval.every, 1)
        self.assertEqual(task.interval.period, "minutes")

    def test_external_wait_release_and_revalidation_are_scheduled(self):
        PeriodicTask.objects.all().delete()
        setup_notification_schedules()

        release = PeriodicTask.objects.select_related('interval').get(
            name='Sweep External Dependency Releases'
        )
        self.assertEqual(
            release.task, 'tasks.tasks.sweep_external_dependency_releases'
        )
        self.assertTrue(release.enabled)
        self.assertEqual(release.interval.every, 1)
        self.assertEqual(release.interval.period, 'minutes')

        response = PeriodicTask.objects.select_related('interval').get(
            name='Sweep External Response Holds'
        )
        self.assertEqual(
            response.task, 'tasks.tasks.sweep_external_response_holds'
        )
        self.assertTrue(response.enabled)
        self.assertEqual(response.interval.every, 1)
        self.assertEqual(response.interval.period, 'minutes')

        archive = PeriodicTask.objects.select_related('crontab').get(
            name='Archive External Response Hold Audit'
        )
        self.assertEqual(
            archive.task, 'tasks.tasks.archive_external_response_hold_audit'
        )
        self.assertTrue(archive.enabled)
        self.assertEqual(archive.crontab.minute, '37')
        self.assertEqual(archive.crontab.hour, '2')

        revalidate = PeriodicTask.objects.select_related('crontab').get(
            name='Revalidate Human External Waits'
        )
        self.assertEqual(
            revalidate.task, 'tasks.tasks.revalidate_human_external_waits'
        )
        self.assertTrue(revalidate.enabled)
        self.assertEqual(revalidate.crontab.minute, '17')
        self.assertEqual(revalidate.crontab.hour, '*/1')

    def test_publication_outbox_sweep_is_scheduled(self):
        """PLS-482: committed publication outbox rows have a durable recovery path."""
        PeriodicTask.objects.all().delete()

        setup_notification_schedules()

        task = PeriodicTask.objects.select_related("interval").get(
            name="Sweep Publication Outbox"
        )
        self.assertTrue(task.enabled)
        self.assertEqual(task.task, "tasks.tasks.sweep_publication_outbox")
        self.assertIsNotNone(task.interval)
        self.assertEqual(task.interval.every, 1)
        self.assertEqual(task.interval.period, "minutes")

    def test_qa_verification_outbox_sweep_is_scheduled(self):
        """PLS-593: committed delayed demand intents have a recovery path."""
        PeriodicTask.objects.all().delete()

        setup_notification_schedules()

        task = PeriodicTask.objects.select_related("interval").get(
            name="Sweep QA Verification Outbox"
        )
        self.assertTrue(task.enabled)
        self.assertEqual(task.task, "tasks.tasks.sweep_qa_verification_outbox")
        self.assertIsNotNone(task.interval)
        self.assertEqual(task.interval.every, 1)
        self.assertEqual(task.interval.period, "minutes")

    def test_linked_pr_sync_preserves_explicit_disable_across_setup(self):
        PeriodicTask.objects.all().delete()
        setup_notification_schedules()

        task = PeriodicTask.objects.get(name="Sync Linked PR Statuses")
        self.assertTrue(task.enabled)
        task.enabled = False
        task.save(update_fields=["enabled"])

        setup_notification_schedules()

        task.refresh_from_db()
        self.assertFalse(task.enabled)
        self.assertEqual(task.task, "tasks.tasks.sync_linked_pr_statuses")
        self.assertEqual(task.crontab.minute, "*/5")

    def test_repeated_setup_does_not_bump_date_changed_for_canary(self):
        """PLS-794: production-order — ready()/setup may run many times before
        the first 5-minute boundary. Unchanged materialization must not rewrite
        date_changed. django-celery-beat baselines never-run interval tasks on
        last_run_at or date_changed, so a bump postpones due forever.

        rei P2-2: exercise real DatabaseScheduler/ModelEntry reload + reserve/
        apply path across ≥2 setup/reload cycles with a controlled clock — not
        bare IntervalSchedule.is_due arithmetic.
        """
        PeriodicTask.objects.all().delete()
        setup_notification_schedules()

        canary = PeriodicTask.objects.select_related("interval").get(
            name="Attachment Storage Canary"
        )
        self.assertTrue(canary.enabled)
        self.assertEqual(canary.task, "tasks.attachment_storage_canary")
        self.assertIsNotNone(canary.interval)
        self.assertEqual(canary.interval.every, 5)
        self.assertEqual(canary.interval.period, "minutes")
        self.assertIsNone(canary.last_run_at)
        self.assertEqual(canary.total_run_count, 0)
        first_changed = canary.date_changed

        # Simulate multi-process bootstrap storms (web xN + worker xN + beat).
        for _ in range(5):
            setup_notification_schedules()

        canary.refresh_from_db()
        self.assertEqual(
            canary.date_changed,
            first_changed,
            "idempotent setup must not bump auto_now date_changed on no-op",
        )
        self.assertIsNone(canary.last_run_at)
        self.assertEqual(canary.total_run_count, 0)

        # Controlled clock pinned to the canary's date_changed baseline so
        # ModelEntry.__init__ (last_run_at = date_changed or _default_now) and
        # IntervalSchedule.nowfun agree. Advance only via this clock.
        clock = {"now": first_changed}

        def _clock_now(*_args, **_kwargs):
            return clock["now"]

        app = current_app._get_current_object()
        # Lazy: DatabaseScheduler.__init__ → setup_schedule reads .schedule.
        scheduler = DatabaseScheduler(app=app, lazy=True)

        # DatabaseScheduler.sync/schedule_changed call close_old_connections(),
        # which tears down Django TestCase's transactional connection and
        # cascades InterfaceError into later tests. Keep the test connection
        # open; production Beat owns its own process lifetime. Also cancel the
        # Finalize(sync) hook so GC after the test cannot close it either.
        try:
            with mock.patch(
                "django_celery_beat.schedulers.close_old_connections"
            ), mock.patch.object(
                ModelEntry, "_default_now", _clock_now
            ), mock.patch(
                "django_celery_beat.utils.now", _clock_now
            ), mock.patch(
                "django_celery_beat.models.now", _clock_now
            ), mock.patch(
                "django_celery_beat.schedulers.now", _clock_now
            ):
                # Cycle 1: initial Beat load after first setup storm.
                # Force a full schedule materialization without going through
                # schedule_changed (which also tries transaction.commit()).
                # Seed the in-memory schedule via all_as_schedule (the same path
                # DatabaseScheduler.schedule uses after a change detection).
                # Keep _initial_read=False and a fresh _last_full_sync so the
                # .schedule property does not re-enter schedule_changed/
                # transaction.commit on every access inside TestCase.
                scheduler._initial_read = False
                scheduler._last_full_sync = datetime.now()
                scheduler._last_timestamp = None
                scheduler._schedule = scheduler.all_as_schedule()
                entry = scheduler._schedule["Attachment Storage Canary"]
                self.assertFalse(
                    bool(entry.is_due().is_due),
                    "canary must not be due at the baseline instant",
                )

                # Cycle 2: another ready()/setup + forced schedule reload (as
                # Beat does after schedule_changed, or a fresh Beat process
                # boot). Re-materialize via all_as_schedule — the production
                # reload path after change detection.
                setup_notification_schedules()
                canary.refresh_from_db()
                self.assertEqual(canary.date_changed, first_changed)
                self.assertIsNone(canary.last_run_at)
                scheduler._schedule = scheduler.all_as_schedule()
                entry = scheduler._schedule["Attachment Storage Canary"]
                self.assertFalse(bool(entry.is_due().is_due))

                # Cycle 3: one more setup/reload, still never-run, still not due.
                setup_notification_schedules()
                scheduler._schedule = scheduler.all_as_schedule()
                entry = scheduler._schedule["Attachment Storage Canary"]
                self.assertFalse(bool(entry.is_due().is_due))
                baseline_last_run = entry.last_run_at

                # Wall-clock crosses the 5-minute interval without any further
                # date_changed bump — canary must become due and dispatch.
                clock["now"] = first_changed + timedelta(minutes=6)
                due_state = entry.is_due()
                self.assertTrue(
                    bool(due_state.is_due),
                    f"canary must be due 6m after baseline; got {due_state!r} "
                    f"(baseline_last_run={baseline_last_run!r}, "
                    f"now={clock['now']!r})",
                )

                # Production tick order: reserve (advances last_run_at/
                # total_run_count in-memory) then apply_entry →
                # apply_async(advance=False) → send. Skip scheduler.sync()
                # persistence (close_old_connections + dirty flush) — we assert
                # the in-memory Beat counters that ModelEntry.next() mutates.
                sent = []

                def _capture_apply(
                    entry_arg, producer=None, advance=True, **kwargs
                ):
                    sent.append(entry_arg.name)
                    return mock.Mock(id="pls-794-canary-msg")

                with mock.patch.object(
                    scheduler, "apply_async", side_effect=_capture_apply
                ), mock.patch.object(scheduler, "sync"):
                    next_entry = scheduler.reserve(entry)
                    scheduler.apply_entry(entry)

                self.assertEqual(sent, ["Attachment Storage Canary"])
                self.assertEqual(next_entry.total_run_count, 1)
                self.assertIsNotNone(next_entry.last_run_at)
                self.assertEqual(next_entry.last_run_at, clock["now"])
        finally:
            finalize = getattr(scheduler, "_finalize", None)
            if finalize is not None:
                finalize.cancel()
            scheduler._dirty = set()

    def test_canary_enabled_gate_still_converges_on_setup(self):
        """PLS-794: real code-owned drift (enabled gate) must still update."""
        PeriodicTask.objects.all().delete()
        setup_notification_schedules()
        canary = PeriodicTask.objects.get(name="Attachment Storage Canary")
        self.assertTrue(canary.enabled)
        before = canary.date_changed

        with override_settings(ATTACHMENT_CANARY_ENABLED=False):
            setup_notification_schedules()

        canary.refresh_from_db()
        self.assertFalse(canary.enabled)
        self.assertGreaterEqual(canary.date_changed, before)

        with override_settings(ATTACHMENT_CANARY_ENABLED=True):
            setup_notification_schedules()

        canary.refresh_from_db()
        self.assertTrue(canary.enabled)

    def test_linked_pr_disable_does_not_needlessly_bump_date_changed(self):
        """PLS-794: preserve_disabled path must also skip no-op saves."""
        PeriodicTask.objects.all().delete()
        setup_notification_schedules()

        task = PeriodicTask.objects.get(name="Sync Linked PR Statuses")
        task.enabled = False
        task.save(update_fields=["enabled"])
        task.refresh_from_db()
        first_changed = task.date_changed

        for _ in range(3):
            setup_notification_schedules()

        task.refresh_from_db()
        self.assertFalse(task.enabled)
        self.assertEqual(task.date_changed, first_changed)

    def test_ensure_periodic_task_helper_skips_identical_save(self):
        """Unit-level: ensure_periodic_task only saves when fields differ."""
        from django_celery_beat.models import IntervalSchedule

        interval, _ = IntervalSchedule.objects.get_or_create(
            every=5, period=IntervalSchedule.MINUTES
        )
        obj, created = ensure_periodic_task(
            name="PLS-794 Helper Probe",
            task="tasks.attachment_storage_canary",
            interval=interval,
            enabled=True,
        )
        self.assertTrue(created)
        first_changed = obj.date_changed

        obj2, created2 = ensure_periodic_task(
            name="PLS-794 Helper Probe",
            task="tasks.attachment_storage_canary",
            interval=interval,
            enabled=True,
        )
        self.assertFalse(created2)
        self.assertEqual(obj2.pk, obj.pk)
        obj2.refresh_from_db()
        self.assertEqual(obj2.date_changed, first_changed)

        # Drift repair still writes.
        obj3, created3 = ensure_periodic_task(
            name="PLS-794 Helper Probe",
            task="tasks.attachment_storage_canary",
            interval=interval,
            enabled=False,
        )
        self.assertFalse(created3)
        obj3.refresh_from_db()
        self.assertFalse(obj3.enabled)
        self.assertGreaterEqual(obj3.date_changed, first_changed)

    def test_ensure_periodic_task_does_not_clobber_beat_runtime_fields(self):
        """PLS-794 rei P2-1: drift repair must not overwrite Beat runtime counters.

        Production race: setup's get_or_create loads a snapshot, Beat then
        advances last_run_at/total_run_count on the row, and a stale full-model
        save() would write the pre-Beat counters back. The helper must
        save(update_fields=code-owned only) against a locked fresh row so Beat
        counters survive while code-owned drift still converges.
        """
        from django_celery_beat.models import IntervalSchedule

        interval, _ = IntervalSchedule.objects.get_or_create(
            every=5, period=IntervalSchedule.MINUTES
        )
        obj, created = ensure_periodic_task(
            name="PLS-794 Runtime Preserve",
            task="tasks.attachment_storage_canary",
            interval=interval,
            enabled=True,
        )
        self.assertTrue(created)
        self.assertIsNone(obj.last_run_at)
        self.assertEqual(obj.total_run_count, 0)

        # Stale in-memory snapshot as if setup read the row before Beat ran.
        stale = PeriodicTask.objects.get(pk=obj.pk)
        stale.last_run_at = None
        stale.total_run_count = 0
        stale.enabled = True

        # Beat advances runtime fields on the committed row.
        beat_run_at = dj_timezone.now() - timedelta(minutes=1)
        PeriodicTask.objects.filter(pk=obj.pk).update(
            last_run_at=beat_run_at,
            total_run_count=7,
        )
        # Confirm Beat state is on disk before the stale setup path runs.
        on_disk = PeriodicTask.objects.get(pk=obj.pk)
        self.assertEqual(on_disk.total_run_count, 7)
        self.assertEqual(on_disk.last_run_at, beat_run_at)

        def _stale_get_or_create(name, defaults=None):
            self.assertEqual(name, "PLS-794 Runtime Preserve")
            return stale, False

        with mock.patch.object(
            PeriodicTask.objects, "get_or_create", side_effect=_stale_get_or_create
        ):
            # Code-owned drift on the stale snapshot (enabled True → False).
            out, created_out = ensure_periodic_task(
                name="PLS-794 Runtime Preserve",
                task="tasks.attachment_storage_canary",
                interval=interval,
                enabled=False,
            )

        self.assertFalse(created_out)
        out.refresh_from_db()
        self.assertFalse(out.enabled, "code-owned enabled drift must still converge")
        self.assertEqual(
            out.total_run_count,
            7,
            "Beat total_run_count must survive stale setup drift-repair",
        )
        self.assertEqual(
            out.last_run_at,
            beat_run_at,
            "Beat last_run_at must survive stale setup drift-repair",
        )
