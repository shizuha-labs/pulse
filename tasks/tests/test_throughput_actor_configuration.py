"""The native throughput producer uses deployment-owned actor exclusions."""
import ast
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from django.test import SimpleTestCase


class ThroughputActorConfigurationTests(SimpleTestCase):
    def native_activity_selection(self, activities):
        # Execute exactly the native producer expression without running the
        # unrelated health-sweep writes or external transports around it.
        from tasks import tasks
        tree = ast.parse(Path(tasks.__file__).read_text())
        sweep = next(n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name=='sweep_org_health')
        assignment = next(n for n in ast.walk(sweep) if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='agent_acts' for t in n.targets))
        expression = ast.Expression(body=assignment.value)
        return eval(compile(expression,tasks.__file__,'eval'),{**vars(tasks),'acts':activities})

    @mock.patch.dict(os.environ, {'PULSE_THROUGHPUT_EXCLUDED_ACTOR_EMAILS':json.dumps(['operator@example.invalid'])})
    def test_actual_producer_excludes_only_the_configured_exact_actor(self):
        activities = [SimpleNamespace(user_email=email) for email in ('operator@example.invalid','operator-helper@example.invalid','agent@example.invalid')]
        result = self.native_activity_selection(activities)
        self.assertEqual([r.user_email for r in result], ['operator-helper@example.invalid','agent@example.invalid'])

    @mock.patch.dict(os.environ, {'PULSE_THROUGHPUT_EXCLUDED_ACTOR_EMAILS':''})
    def test_unconfigured_installation_does_not_guess_an_owner(self):
        activity = SimpleNamespace(user_email='operator@example.invalid')
        self.assertEqual(self.native_activity_selection([activity]),[activity])

    def test_invalid_config_fails_closed_without_disclosing_values(self):
        from tasks.tasks import _org_health_throughput_activity
        for value in ('{', '{}', '[null]', '["not-an-email"]'):
            with self.subTest(value=value), mock.patch.dict(os.environ, {'PULSE_THROUGHPUT_EXCLUDED_ACTOR_EMAILS':value}):
                with self.assertRaisesRegex(ValueError, '^Invalid PULSE_THROUGHPUT_EXCLUDED_ACTOR_EMAILS configuration$'):
                    _org_health_throughput_activity([])
