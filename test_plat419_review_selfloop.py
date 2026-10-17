"""PLAT-419 — WorkflowTransitionDetailSerializer rejects same-team review self-loops.

Tests the validate() guard in WorkflowTransitionDetailSerializer:
- A "Submit for * Review" transition that assigns to the same team as a bounce-back
  transition (Request Changes / Return for Changes / Failed Verification) in the same
  workflow is rejected.
- Cross-team review assignments and non-review transitions are unaffected.

Pure unit tests with mocked ORM — no DB or full Django stack needed.
"""
import sys
import os
import django
from django.conf import settings
from unittest.mock import MagicMock, patch
from uuid import uuid4

# Minimal Django settings — only what the serializer and DRF need.
if not settings.configured:
    settings.configure(
        DATABASES={'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': ':memory:'}},
        INSTALLED_APPS=[
            'django.contrib.contenttypes',
            'django.contrib.auth',
            'rest_framework',
            'tasks',
        ],
        USE_TZ=True,
        REST_FRAMEWORK={},
    )
    django.setup()

# Imports after Django setup
from rest_framework import serializers  # noqa: E402
from tasks.serializers import WorkflowTransitionDetailSerializer  # noqa: E402

WORKFLOW = MagicMock()
WORKFLOW.id = uuid4()


def _make_data(name, to_status, post_functions, workflow=WORKFLOW):
    return {
        'name': name,
        'to_status': to_status,
        'post_functions': post_functions,
        'workflow': workflow,
    }


def _bounce_transition(name, impl_team):
    t = MagicMock()
    t.name = name
    t.post_functions = [{'type': 'assign_team', 'value': impl_team}]
    return t


def _run_validate(name, to_status, post_functions, siblings, workflow=WORKFLOW, instance=None):
    """Call serializer.validate() with WorkflowTransition.objects.filter mocked."""
    s = WorkflowTransitionDetailSerializer.__new__(WorkflowTransitionDetailSerializer)
    s.instance = instance
    data = _make_data(name, to_status, post_functions, workflow)

    mock_qs = MagicMock()
    mock_qs.exclude.return_value = mock_qs
    mock_qs.__iter__ = MagicMock(return_value=iter(siblings))
    with patch('tasks.serializers.WorkflowTransition') as mock_model:
        mock_model.objects.filter.return_value = mock_qs
        return s.validate(data)


import pytest  # noqa: E402


class TestReviewSelfLoopRejected:
    # -------------------------------------------------------------------------
    # Rejected cases
    # -------------------------------------------------------------------------

    def test_submit_for_engineering_review_same_team_rejected(self):
        """Submit for Engineering Review → engineering triggers the guard."""
        with pytest.raises(serializers.ValidationError) as exc_info:
            _run_validate(
                name='Submit for Engineering Review',
                to_status='in_review',
                post_functions=[
                    {'type': 'assign_team', 'value': 'engineering'},
                    {'type': 'stamp_review_started'},
                ],
                siblings=[_bounce_transition('Request Changes', 'engineering')],
            )
        err = str(exc_info.value.detail)
        assert 'self-loop' in err
        assert 'engineering' in err

    def test_generic_submit_for_review_same_team_rejected(self):
        """Any 'Submit for * Review' name with matching team is rejected."""
        with pytest.raises(serializers.ValidationError):
            _run_validate(
                name='Submit for QA Review',
                to_status='in_review',
                post_functions=[{'type': 'assign_team', 'team': 'engineering'}],
                siblings=[_bounce_transition('Return for Changes', 'engineering')],
            )

    def test_in_review_stamp_same_team_rejected(self):
        """in_review + stamp_review_started triggers the guard too."""
        with pytest.raises(serializers.ValidationError):
            _run_validate(
                name='Move to Review',
                to_status='in_review',
                post_functions=[
                    {'type': 'stamp_review_started'},
                    {'type': 'assign_team', 'value': 'engineering'},
                ],
                siblings=[_bounce_transition('Failed Verification', 'engineering')],
            )

    def test_failed_verification_bounce_back_detected(self):
        """Failed Verification is a bounce-back keyword."""
        with pytest.raises(serializers.ValidationError):
            _run_validate(
                name='Submit for Code Review',
                to_status='in_review',
                post_functions=[{'type': 'assign_team', 'value': 'qa'}],
                siblings=[_bounce_transition('Failed Verification', 'qa')],
            )


class TestReviewSelfLoopAllowed:
    # -------------------------------------------------------------------------
    # Allowed cases
    # -------------------------------------------------------------------------

    def test_cross_team_review_allowed(self):
        """Submit for Code Review → review team is a valid cross-team route."""
        data = _run_validate(
            name='Submit for Code Review',
            to_status='in_review',
            post_functions=[
                {'type': 'assign_team', 'value': 'review'},
                {'type': 'stamp_review_started'},
            ],
            siblings=[_bounce_transition('Request Changes', 'engineering')],
        )
        assert data['name'] == 'Submit for Code Review'

    def test_architecture_review_allowed(self):
        """Submit for Architecture Review → architecture is fine."""
        data = _run_validate(
            name='Submit for Architecture Review',
            to_status='in_review',
            post_functions=[{'type': 'assign_team', 'value': 'architecture'}],
            siblings=[_bounce_transition('Request Changes', 'engineering')],
        )
        assert data['name'] == 'Submit for Architecture Review'

    def test_non_review_transition_not_affected(self):
        """A transition that isn't a review-submit is not checked."""
        data = _run_validate(
            name='Escalate',
            to_status='in_review',
            post_functions=[{'type': 'assign_team', 'value': 'engineering'}],
            siblings=[_bounce_transition('Request Changes', 'engineering')],
        )
        assert data['name'] == 'Escalate'

    def test_review_submit_no_assign_team_passes(self):
        """Submit for Review with no assign_team post-function passes the guard."""
        data = _run_validate(
            name='Submit for Code Review',
            to_status='in_review',
            post_functions=[{'type': 'stamp_review_started'}],
            siblings=[_bounce_transition('Request Changes', 'engineering')],
        )
        assert data['name'] == 'Submit for Code Review'

    def test_no_workflow_skips_guard(self):
        """Without a workflow context the guard is a no-op."""
        data = _run_validate(
            name='Submit for Engineering Review',
            to_status='in_review',
            post_functions=[{'type': 'assign_team', 'value': 'engineering'}],
            siblings=[],
            workflow=None,
        )
        assert data['workflow'] is None

    def test_no_bounce_back_transitions_passes(self):
        """Workflow with no bounce-back transitions: nothing to compare."""
        data = _run_validate(
            name='Submit for Engineering Review',
            to_status='in_review',
            post_functions=[{'type': 'assign_team', 'value': 'engineering'}],
            siblings=[],
        )
        assert data['name'] == 'Submit for Engineering Review'
