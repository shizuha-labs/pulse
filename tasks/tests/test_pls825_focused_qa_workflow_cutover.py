"""PLS-825 (PLS-819 S2): focused-QA workflow cutover tests.

Covers the HLD §6 rollout contract around the generator:
- new creation binds the dedicated ``focused-qa`` workflow and one immutable
  generation row (one artifact per generating event);
- retrying the same generating event returns the existing task/generation
  (idempotency via the generation-uniqueness contract);
- the rollout gate disables new focused-qa creation until schema, workflow
  inventory, equivalence-key inventory, backfill counts, and handler probes are
  green — falling back to the legacy reviewed-task path (compatibility);
- ``PULSE_FOCUSED_QA_WORKFLOW=reviewed-task`` keeps the legacy generator path
  available (disabled by default) during the initial compatibility window;
- ordinary ``reviewed-task`` behavior is unchanged (negative control).
"""
from unittest.mock import patch

from django.test import TestCase

from tasks.models import (
    DeploymentEvent,
    FocusedQAGeneration,
    Item,
    Project,
    Workflow,
    WorkflowTransition,
)
from tasks.services.deployment_events import upsert_deployment_event
from tasks.services.focused_qa import (
    COMPAT_WORKFLOW_NAME,
    FOCUSED_QA_WORKFLOW_SLUG,
    _focused_qa_workflow_name,
    _focused_qa_workflow_ready,
    ensure_focused_qa_for_item,
    focused_qa_rollout_gate,
)


def _make_source(project, title='Runtime replay fix'):
    return Item.objects.create(
        project=project,
        item_key=project.get_next_item_key(),
        title=title,
        item_type='task.bug',
        labels={'requires-live-qa': True},
        status='open',
    )


def _deploy(project, source, commit='c' * 40, risk='requires-live-qa'):
    event, created, devops = upsert_deployment_event({
        'project_key': project.key,
        'repo': 'shizuha-labs/cortex',
        'service': 'cortex',
        'environment': 'live',
        'cluster': 'shizuha-k3s',
        'commit_sha': commit,
        'image_digest': 'sha256:abc',
        'pipeline_run_id': 'run-%s' % commit[:8],
        'state': 'smoke_passed',
        'smoke_result': 'passed',
        'source_task_keys': [source.item_key],
        'metadata': {'risk_labels': [risk]},
    })
    return event


class FocusedQAWorkflowCutoverTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(key='PLS', org_id=1, name='Pulse')
        # PLS-571: mint-successful tests traverse routing (post-eligibility by
        # design); stub classify_roster_email to the offline resolution_error
        # behavior the suite was built against so no test hits the live ID
        # service.
        roster_patch = patch(
            'tasks.services.canonical_identity.classify_roster_email',
            side_effect=lambda email, **kw: ('resolution_error', email),
        )
        roster_patch.start()
        self.addCleanup(roster_patch.stop)

    def test_generator_binds_focused_qa_workflow_and_one_generation(self):
        source = _make_source(self.project)
        event = _deploy(self.project, source)
        qa = Item.objects.get(source='focused-qa-automation')

        self.assertEqual(qa.workflow_name, FOCUSED_QA_WORKFLOW_SLUG)
        self.assertTrue(hasattr(qa, 'focused_qa_generation'))
        gen = qa.focused_qa_generation
        self.assertEqual(gen.generation_key, f'deploy:{event.pk}:requires-live-qa:live')
        self.assertEqual(gen.originating_task_id, source.pk)
        self.assertEqual(gen.organization, '1')
        self.assertEqual(gen.environment, 'live')
        self.assertEqual(gen.observed_deployment_event_id, event.pk)
        self.assertEqual(gen.observed_build, 'c' * 40)
        self.assertEqual(gen.origin_surface_schema, 'task-root.v1')
        self.assertEqual(gen.origin_surface_key, f'task-root.v1:{source.pk}')
        # Exactly one generation row for this task.
        self.assertEqual(FocusedQAGeneration.objects.filter(focused_qa_task=qa).count(), 1)

    def test_reingest_same_event_returns_existing_task_and_generation(self):
        source = _make_source(self.project)
        _deploy(self.project, source)
        qa = Item.objects.get(source='focused-qa-automation')
        gen_key = qa.focused_qa_generation.generation_key

        # Re-ingest the identical deployment event: same task, same generation.
        _deploy(self.project, source)
        self.assertEqual(Item.objects.filter(source='focused-qa-automation').count(), 1)
        qa.refresh_from_db()
        self.assertEqual(qa.focused_qa_generation.generation_key, gen_key)
        self.assertEqual(FocusedQAGeneration.objects.filter(focused_qa_task=qa).count(), 1)

    def test_label_update_trigger_binds_origin_generation_key(self):
        source = _make_source(self.project)
        ensure_focused_qa_for_item(source, trigger='label_update')
        qa = Item.objects.get(source='focused-qa-automation')
        self.assertEqual(qa.workflow_name, FOCUSED_QA_WORKFLOW_SLUG)
        self.assertEqual(
            qa.focused_qa_generation.generation_key,
            f'origin:{source.pk}:requires-live-qa:live',
        )

    def test_rollout_gate_green_when_workflow_seeded(self):
        report = focused_qa_rollout_gate()
        self.assertTrue(report['schema'])
        self.assertTrue(report['workflow_inventory'])
        self.assertTrue(report['equivalence_key_inventory'])
        self.assertTrue(report['backfill'])
        self.assertTrue(report['handler_probes'])
        self.assertTrue(report['ready'])
        self.assertTrue(_focused_qa_workflow_ready())

    def test_rollout_gate_red_when_workflow_inventory_missing(self):
        wf = Workflow.objects.get(slug=FOCUSED_QA_WORKFLOW_SLUG)
        # Remove one required typed disposition -> inventory no longer green.
        WorkflowTransition.objects.filter(
            workflow=wf, name='Confirm Product Failure',
        ).delete()
        report = focused_qa_rollout_gate()
        self.assertFalse(report['workflow_inventory'])
        self.assertFalse(report['ready'])
        self.assertFalse(_focused_qa_workflow_ready())
        # Generator falls back to the legacy compatibility path.
        self.assertEqual(_focused_qa_workflow_name(), COMPAT_WORKFLOW_NAME)

    def test_rollout_gate_red_when_backfill_incomplete(self):
        # A non-terminal focused-qa-automation task WITHOUT a generation row
        # simulates an incomplete backfill -> new creation stays disabled.
        Item.objects.create(
            project=self.project,
            title='Orphan legacy focused QA',
            mode='task',
            item_type='task.focused_qa',
            source='focused-qa-automation',
            status='todo',
            workflow_name='reviewed-task',
            created_by_id=0,
            created_by_email='qa-automation@shizuha.com',
        )
        report = focused_qa_rollout_gate()
        self.assertFalse(report['backfill'])
        self.assertEqual(report['backfill_orphans'], 1)
        self.assertFalse(report['ready'])
        self.assertEqual(_focused_qa_workflow_name(), COMPAT_WORKFLOW_NAME)

    def test_compatibility_handler_forces_reviewed_task_path(self):
        with patch.dict('os.environ', {'PULSE_FOCUSED_QA_WORKFLOW': 'reviewed-task'}):
            self.assertEqual(_focused_qa_workflow_name(), COMPAT_WORKFLOW_NAME)
            source = _make_source(self.project)
            _deploy(self.project, source)
            qa = Item.objects.get(source='focused-qa-automation')
            self.assertEqual(qa.workflow_name, COMPAT_WORKFLOW_NAME)
            # Legacy compatibility path binds no generation.
            self.assertFalse(hasattr(qa, 'focused_qa_generation'))

    def test_kill_switch_disables_new_creation(self):
        with patch.dict('os.environ', {'PULSE_FOCUSED_QA_ENABLED': '0'}):
            source = _make_source(self.project)
            _deploy(self.project, source)
            self.assertEqual(Item.objects.filter(source='focused-qa-automation').count(), 0)

    def test_generic_reviewed_task_negative_control(self):
        """Ordinary reviewed-task behavior is unchanged: a plain reviewed-task
        item is not a focused-QA artifact and gets no generation."""
        item = Item.objects.create(
            project=self.project,
            title='Ordinary reviewed task',
            workflow_name='reviewed-task',
            status='todo',
        )
        self.assertEqual(item.workflow_name, 'reviewed-task')
        self.assertFalse(hasattr(item, 'focused_qa_generation'))
        self.assertEqual(FocusedQAGeneration.objects.filter(focused_qa_task=item).count(), 0)
