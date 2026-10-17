import io
import json
from unittest import mock
from urllib.error import HTTPError, URLError

from django.test import TestCase, override_settings

from tasks.models import Status, Workflow, WorkflowTransition
from tasks.serializers import (
    WorkflowDetailSerializer,
    WorkflowTransitionDetailSerializer,
)


class _Response:
    status = 200

    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit=-1):
        return self.payload


@override_settings(
    TESTING=False,
    SOURCE_AUTHORITY_CONTRACT_TEST_BYPASS=False,
    SOURCE_AUTHORITY_CONTRACT_URL='http://contract.invalid',
)
class SourceAuthorityMutationAdmissionTests(TestCase):
    def setUp(self):
        self.open_status = Status.objects.create(
            name='Open PLAT-4861',
            slug='open-plat-4861',
            category='todo',
        )
        self.done_status = Status.objects.create(
            name='Done PLAT-4861',
            slug='done-plat-4861',
            category='done',
        )
        self.workflow = Workflow.objects.create(
            name='PLAT-4861 fixture',
            slug='plat-4861-fixture',
            initial_status=self.open_status,
            knowledge_base=[],
        )
        self.transition = WorkflowTransition.objects.create(
            workflow=self.workflow,
            name='Finish',
            from_status=self.open_status.slug,
            to_status=self.done_status.slug,
            knowledge_articles=[],
        )

    def _allow(self, consumer='pulse.workflow_knowledge'):
        return _Response({
            'decision': 'allow',
            'registry_version': 'orig18-v1',
            'registry_sha256': 'a' * 64,
            'matched_consumers': [consumer],
            'violations': [],
        })

    @mock.patch('tasks.source_authority_contracts.urllib.request.urlopen')
    def test_workflow_knowledge_saves_only_after_matching_revisioned_allow(self, urlopen):
        urlopen.return_value = self._allow()
        serializer = WorkflowDetailSerializer(
            self.workflow,
            data={
                'knowledge_base': [{
                    'type': 'inline',
                    'title': 'Origin PR flow',
                    'content': 'Origin is canonical; GitHub is a read-only mirror.',
                }],
            },
            partial=True,
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save()
        self.workflow.refresh_from_db()
        self.assertEqual(
            self.workflow.knowledge_base[0]['title'],
            'Origin PR flow',
        )

    @mock.patch('tasks.source_authority_contracts.urllib.request.urlopen')
    def test_plat4682_rejection_aborts_before_workflow_save(self, urlopen):
        payload = {
            'decision': 'reject',
            'registry_version': 'orig18-v1',
            'registry_sha256': 'b' * 64,
            'matched_consumers': ['pulse.workflow_knowledge'],
            'violations': [{
                'consumer': 'pulse.workflow_knowledge',
                'contract': 'github-layer-merge-gate',
                'location': 'pulse.workflow_knowledge:plat-4861-fixture',
                'message': 'PLAT-4682 retired this GitHub-layer merge instruction',
            }],
        }
        urlopen.side_effect = HTTPError(
            'http://contract.invalid/contracts/validate',
            422,
            'rejected',
            {},
            io.BytesIO(json.dumps(payload).encode()),
        )
        serializer = WorkflowDetailSerializer(
            self.workflow,
            data={
                'knowledge_base': [{
                    'type': 'inline',
                    'title': 'stale',
                    'content': (
                        'The merge gate for any code still holds at the '
                        'GitHub layer regardless.'
                    ),
                }],
            },
            partial=True,
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn('source_authority_contract', serializer.errors)
        self.workflow.refresh_from_db()
        self.assertEqual(self.workflow.knowledge_base, [])

    @mock.patch('tasks.source_authority_contracts.urllib.request.urlopen')
    def test_validator_unavailable_fails_closed_for_transition_update(self, urlopen):
        urlopen.side_effect = URLError('provider offline')
        serializer = WorkflowTransitionDetailSerializer(
            self.transition,
            data={
                'knowledge_articles': [{
                    'type': 'inline',
                    'title': 'new instructions',
                    'content': 'Use Origin.',
                }],
            },
            partial=True,
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn('source_authority_contract', serializer.errors)
        self.transition.refresh_from_db()
        self.assertEqual(self.transition.knowledge_articles, [])

    @mock.patch('tasks.source_authority_contracts.urllib.request.urlopen')
    def test_missing_required_consumer_is_not_treated_as_allow(self, urlopen):
        urlopen.return_value = self._allow(consumer='some.other.consumer')
        serializer = WorkflowDetailSerializer(
            self.workflow,
            data={'description': 'harmless edit'},
            partial=True,
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn('did not evaluate required consumer', str(serializer.errors))
