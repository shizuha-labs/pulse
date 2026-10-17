"""PLS-1421 — the name-based beat dispatch must land sweep_org_health on 'freshness'.

django-celery-beat's DatabaseScheduler fires 'Sweep Org Health' via
app.send_task('tasks.tasks.sweep_org_health') — a name-based send whose router
consults only CELERY_TASK_ROUTES and then falls back to CELERY_TASK_DEFAULT_QUEUE.
The @shared_task(queue='freshness') decorator attribute is honored only on direct
apply_async, so without the task_routes entry the sweep kept landing on the
shared queue and the freshness worker idled forever — silently (aoi review 6473).
"""
from django.test import SimpleTestCase


class FreshnessRouteTests(SimpleTestCase):
    def test_router_routes_sweep_org_health_to_freshness(self):
        from taskmanager.celery import app

        route = app.amqp.router.route({}, 'tasks.tasks.sweep_org_health')
        self.assertEqual(
            route['queue'].name, 'freshness',
            'name-based send_task router must honor CELERY_TASK_ROUTES',
        )

    def test_unrouted_tasks_still_default_to_shared_queue(self):
        from taskmanager.celery import app

        route = app.amqp.router.route(
            {}, 'tasks.tasks.evaluate_pretriage_age_deadline',
        )
        self.assertEqual(route['queue'].name, 'shizuha_pulse')
