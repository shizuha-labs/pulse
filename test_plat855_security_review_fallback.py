"""PLS-591 — exact review roles hold in-lane when their seat pool is empty."""
import importlib.util
import pathlib
import sys
import types
import unittest
from types import SimpleNamespace
from unittest import mock

COMMENT_CREATE = mock.Mock()

# Load workflow_service.py directly so this pure unit test does not import the
# heavyweight tasks.services package (which imports Django models at package init).
utasks_pkg = types.ModuleType('utasks')
utasks_pkg.__path__ = [str(pathlib.Path(__file__).parent / 'tasks')]
sys.modules.setdefault('utasks', utasks_pkg)
sys.modules.setdefault('utasks.models', types.SimpleNamespace(
    Comment=types.SimpleNamespace(
        ORIGIN_WORKFLOW_LIFECYCLE='workflow.lifecycle',
        ORIGIN_RESOLVER_AUDIT='resolver.audit',
        ORIGIN_VALIDATOR_AUDIT='validator.audit',
        objects=types.SimpleNamespace(create=COMMENT_CREATE),
    )
))
services_pkg = types.ModuleType('utasks.services')
services_pkg.__path__ = [str(pathlib.Path(__file__).parent / 'tasks' / 'services')]
sys.modules.setdefault('utasks.services', services_pkg)
_spec = importlib.util.spec_from_file_location(
    'utasks.services.workflow_service',
    pathlib.Path(__file__).parent / 'tasks' / 'services' / 'workflow_service.py',
)
_workflow_module = importlib.util.module_from_spec(_spec)
sys.modules['utasks.services.workflow_service'] = _workflow_module
_spec.loader.exec_module(_workflow_module)
WorkflowService = _workflow_module.WorkflowService


class _Item:
    id = 855
    item_key = 'HIVE-203'
    created_by_email = 'ichi@shizuha.com'
    created_by_id = 1
    project_id = None
    project = None
    labels = []
    tags = []

    def __init__(self, *, reviewable=True, status='in_review'):
        self.metadata = {'pull_requests': [{'url': 'https://origin.shizuha.com/shizuha-labs/hive/pulls/87'}]} if reviewable else {}
        self.source = ''
        self.status = status
        self.priority = 'high'
        self.assignment_group = 'devops'
        self.assignee_id = 7
        self.assignee_email = 'ichi@shizuha.com'
        self.saved_update_fields = None

    def assign(self, user, save=False):
        self.assignee_id = user.get('id')
        self.assignee_email = user.get('email')

    def unassign(self, save=False, **_kwargs):
        self.assignee_id = None
        self.assignee_email = None

    def save(self, update_fields=None):
        self.saved_update_fields = update_fields


class SecurityReviewFallbackTests(unittest.TestCase):
    def setUp(self):
        COMMENT_CREATE.reset_mock()

    def _assert_exact_role_hold(self, *, team, reviewable=True, status='in_review'):
        item = _Item(reviewable=reviewable, status=status)
        transition = SimpleNamespace(
            name=f'Submit for {team.title()} Review',
            post_functions=[{'type': 'assign_team', 'value': team}],
        )

        with mock.patch.object(
            WorkflowService, '_resolve_team_assignee', return_value=(None, None),
        ) as resolver, mock.patch.object(
            WorkflowService, '_record_review_seat_starvation', return_value=True,
        ) as hold, mock.patch.object(
            WorkflowService, '_maybe_file_zero_member_team_alert', return_value=None,
        ), mock.patch.object(
            WorkflowService, '_pod_roster', return_value={team: [[]]},
        ):
            # Two production-order post-function passes must converge on the
            # same exact-role group queue; neither may consult another role.
            WorkflowService.execute_post_functions(transition, item)
            WorkflowService.execute_post_functions(transition, item)

        self.assertEqual(item.assignment_group, team)
        self.assertIsNone(item.assignee_id)
        self.assertIsNone(item.assignee_email)
        self.assertEqual(
            [call.args[0] for call in resolver.call_args_list],
            [team, team],
        )
        expected_review_starvation_events = (
            2 if status == 'in_review' else 0
        )
        self.assertEqual(hold.call_count, expected_review_starvation_events)
        COMMENT_CREATE.assert_not_called()

    def test_linked_pr_security_gate_holds_on_security(self):
        self._assert_exact_role_hold(team='security', reviewable=True)

    def test_no_pr_security_gate_holds_on_security(self):
        self._assert_exact_role_hold(team='security', reviewable=False)

    def test_empty_review_pool_stays_on_review(self):
        self._assert_exact_role_hold(team='review')

    def test_empty_merge_pool_stays_on_merge(self):
        self._assert_exact_role_hold(team='merge', status='awaiting_merge')

    def test_valid_security_owner_survives_two_post_function_passes(self):
        item = _Item(reviewable=True, status='in_review')
        item.assignment_group = 'security'
        item.assignee_id = 42
        item.assignee_email = 'akira@shizuha.com'
        transition = SimpleNamespace(
            name='Submit for Security Review',
            post_functions=[{'type': 'assign_team', 'value': 'security'}],
        )

        with mock.patch.object(
            WorkflowService, '_resolve_team_assignee', return_value=(None, None),
        ), mock.patch.object(
            WorkflowService, '_is_team_member', return_value=True,
        ), mock.patch.object(
            WorkflowService, '_seat_satisfies_required_capability', return_value=True,
        ), mock.patch.object(
            WorkflowService, '_member_can_execute_from_status', return_value=True,
        ), mock.patch.object(
            WorkflowService, '_merge_sod_candidate_reasons', return_value=[],
        ), mock.patch.object(
            WorkflowService, '_record_review_seat_starvation', return_value=True,
        ) as hold, mock.patch.object(
            WorkflowService, '_pod_roster', return_value={'security': [['akira@shizuha.com']]},
        ):
            WorkflowService.execute_post_functions(transition, item)
            WorkflowService.execute_post_functions(transition, item)

        self.assertEqual(item.assignment_group, 'security')
        self.assertEqual(item.assignee_id, 42)
        self.assertEqual(item.assignee_email, 'akira@shizuha.com')
        hold.assert_not_called()

    def test_cross_rostered_prior_role_owner_is_cleared_on_security_handoff(self):
        """A capable Security member in Engineering is still a fresh handoff."""
        item = _Item(reviewable=True, status='in_review')
        item.assignment_group = 'engineering'
        item.assignee_id = 42
        item.assignee_email = 'akira@shizuha.com'
        transition = SimpleNamespace(
            name='Submit for Security Review',
            post_functions=[{'type': 'assign_team', 'value': 'security'}],
        )

        with mock.patch.object(
            WorkflowService, '_resolve_team_assignee', return_value=(None, None),
        ), mock.patch.object(
            WorkflowService, '_is_team_member', return_value=True,
        ), mock.patch.object(
            WorkflowService, '_seat_satisfies_required_capability', return_value=True,
        ), mock.patch.object(
            WorkflowService, '_member_can_execute_from_status', return_value=True,
        ), mock.patch.object(
            WorkflowService, '_merge_sod_candidate_reasons', return_value=[],
        ), mock.patch.object(
            WorkflowService, '_record_review_seat_starvation', return_value=True,
        ) as hold, mock.patch.object(
            WorkflowService, '_pod_roster', return_value={
                'security': [['akira@shizuha.com']],
            },
        ):
            WorkflowService.execute_post_functions(transition, item)

        self.assertEqual(item.assignment_group, 'security')
        self.assertIsNone(item.assignee_id)
        self.assertIsNone(item.assignee_email)
        hold.assert_called_once()
