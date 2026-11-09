"""PLAT-12205: a hibernated seat is not a failover target.

The aoi → chika miss picked a hibernated seat because its telemetry sample
was green. The published hibernated set must outrank that sample unless the
caller explicitly asked for wakeable admission.
"""
from unittest.mock import patch

from django.test import SimpleTestCase

from tasks.services.workflow_service import WorkflowService


class HibernatedFailoverTargetTests(SimpleTestCase):
    def test_green_sample_does_not_make_a_hibernated_seat_available(self):
        with patch.object(
            WorkflowService, '_disabled_agent_emails', return_value=set(),
        ), patch.object(
            WorkflowService, '_hibernated_agent_emails',
            return_value={'chika@shizuha.com'},
        ), patch.object(
            WorkflowService, '_health_availability_map',
            return_value={'chika@shizuha.com': True},
        ), patch.object(
            WorkflowService, '_configured_agent_availability', return_value=None,
        ), patch.object(
            WorkflowService, '_is_human_account', return_value=False,
        ):
            self.assertFalse(
                WorkflowService._is_agent_available_for_assignment('chika@shizuha.com')
            )

    def test_wakeable_admission_still_reaches_a_hibernated_seat(self):
        with patch.object(
            WorkflowService, '_disabled_agent_emails', return_value=set(),
        ), patch.object(
            WorkflowService, '_hibernated_agent_emails',
            return_value={'chika@shizuha.com'},
        ), patch.object(
            WorkflowService, '_wakeable_agent_emails',
            return_value={'chika@shizuha.com'},
        ), patch.object(
            WorkflowService, '_is_agent_wakeable_for_new_assignment',
            return_value=True,
        ), patch.object(
            WorkflowService, '_is_human_account', return_value=False,
        ):
            self.assertTrue(
                WorkflowService._is_agent_available_for_assignment(
                    'chika@shizuha.com', allow_wakeable=True,
                )
            )

    def test_hibernated_green_sample_stays_closed_without_wakeable_ask(self):
        """Wakeable is published and the sample is green, but the caller
        did not ask for wakeable admission. Hibernated still loses."""
        with patch.object(
            WorkflowService, '_disabled_agent_emails', return_value=set(),
        ), patch.object(
            WorkflowService, '_hibernated_agent_emails',
            return_value={'chika@shizuha.com'},
        ), patch.object(
            WorkflowService, '_wakeable_agent_emails',
            return_value={'chika@shizuha.com'},
        ), patch.object(
            WorkflowService, '_health_availability_map',
            return_value={'chika@shizuha.com': True},
        ), patch.object(
            WorkflowService, '_configured_agent_availability', return_value=None,
        ), patch.object(
            WorkflowService, '_is_human_account', return_value=False,
        ):
            self.assertFalse(
                WorkflowService._is_agent_available_for_assignment(
                    'chika@shizuha.com',
                )
            )

    def test_non_hibernated_green_sample_stays_available(self):
        with patch.object(
            WorkflowService, '_disabled_agent_emails', return_value=set(),
        ), patch.object(
            WorkflowService, '_hibernated_agent_emails', return_value=set(),
        ), patch.object(
            WorkflowService, '_health_availability_map',
            return_value={'aoi@agents.shizuha.io': True},
        ), patch.object(
            WorkflowService, '_configured_agent_availability', return_value=None,
        ), patch.object(
            WorkflowService, '_is_human_account', return_value=False,
        ):
            self.assertTrue(
                WorkflowService._is_agent_available_for_assignment(
                    'aoi@agents.shizuha.io'
                )
            )
