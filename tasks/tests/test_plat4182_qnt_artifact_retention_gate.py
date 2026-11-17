"""PLAT-4182: QNT pre-close artifact-retention gate.

The regression mirrors the QNT-26 root cause: a QNT task closed code-only
without publishing the data snapshot to the canonical PVC. The gate is a
project-scoped validator on the autonomous-dev `done` transition: a QNT task
may close only after it records a verified artifact (fenced JSON matching the
Architecture contract) or an explicit N/A disposition.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase

from tasks.models import Comment, Item, Project, Status, Workflow, WorkflowTransition
from tasks.services.workflow_service import WorkflowService, WorkflowValidationError


VALID_VERIFIED = """QNT artifact retention: verified
```json
{
  "snapshot_id": "qnt3-20260819T000000Z-abc123def456",
  "canonical_path": "/data/market-data/snapshots/qnt3-20260819T000000Z-abc123def456",
  "manifest_path": "/data/market-data/snapshots/qnt3-20260819T000000Z-abc123def456/manifest.json",
  "checksums_path": "/data/market-data/snapshots/qnt3-20260819T000000Z-abc123def456/checksums/SHA256SUMS",
  "manifest_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "required_files": [
    "venues/binanceusdm/spot/BTCUSDT/ohlcv/timeframe=1h/part-00000.parquet",
    "venues/binanceusdm/spot/BTCUSDT/funding/part-00000.parquet",
    "venues/binanceusdm/spot/ETHUSDT/ohlcv/timeframe=1h/part-00000.parquet",
    "venues/binanceusdm/spot/ETHUSDT/funding/part-00000.parquet"
  ],
  "qnt_data_verify": {
    "snapshot_id": "qnt3-20260819T000000Z-abc123def456",
    "ok": true,
    "files_verified": 4,
    "problems": []
  }
}
```"""


class QntArtifactRetentionGateTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='plat4182', email='plat4182@shizuha.com', password='x',
            is_staff=True,
        )
        self.project = Project.objects.create(
            org_id=1, name='QNT Market Data', key='QNT',
        )
        self.other_project = Project.objects.create(
            org_id=1, name='Shizuha CLI', key='SCLI',
        )
        self.done_status, _ = Status.objects.get_or_create(
            slug='done', defaults={'name': 'Done', 'category': 'done'},
        )
        self.open_status, _ = Status.objects.get_or_create(
            slug='open', defaults={'name': 'Open', 'category': 'open'},
        )
        self.workflow, _ = Workflow.objects.get_or_create(
            slug='autonomous-dev',
            defaults={'name': 'Autonomous Dev', 'initial_status': self.open_status},
        )
        self.transition = WorkflowTransition.objects.create(
            workflow=self.workflow,
            name='Complete',
            from_status='in_review',
            to_status='done',
            validators=[{'type': 'qnt_data_artifact_gate', 'project_keys': ['QNT']}],
        )

    def _item(self, project):
        return Item.objects.create(
            title='task', project=project, status='open',
            workflow_name='autonomous-dev', created_by_id=self.user.id,
        )

    def _comment(self, item, content):
        return Comment.objects.create(
            item=item, author_id=self.user.id,
            author_email=self.user.email, author_name=self.user.username,
            content=content,
        )

    def test_verified_json_validation(self):
        self.assertTrue(WorkflowService._qnt_verified_json_valid(VALID_VERIFIED))
        # Missing qnt_data_verify.ok
        self.assertFalse(WorkflowService._qnt_verified_json_valid(
            VALID_VERIFIED.replace('"ok": true', '"ok": false'),
        ))
        # problems non-empty
        self.assertFalse(WorkflowService._qnt_verified_json_valid(
            VALID_VERIFIED.replace('"problems": []', '"problems": ["x"]'),
        ))
        # fewer than 4 required files
        self.assertFalse(WorkflowService._qnt_verified_json_valid(
            VALID_VERIFIED.replace(
                '"venues/binanceusdm/spot/BTCUSDT/funding/part-00000.parquet",\n    ',
                '',
            ),
        ))
        # not JSON at all
        self.assertFalse(WorkflowService._qnt_verified_json_valid(
            'QNT artifact retention: verified\nno json here',
        ))

    def test_artifact_retention_satisfied(self):
        item = self._item(self.project)
        self.assertFalse(
            WorkflowService._qnt_artifact_retention_satisfied(item),
            'no marker must fail closed',
        )
        self._comment(item, VALID_VERIFIED)
        self.assertTrue(
            WorkflowService._qnt_artifact_retention_satisfied(item),
            'verified marker + valid JSON must pass',
        )

    def test_na_disposition_passes(self):
        item = self._item(self.project)
        self._comment(
            item,
            'QNT artifact retention: N/A — research-only task, no artifact produced',
        )
        self.assertTrue(
            WorkflowService._qnt_artifact_retention_satisfied(item),
        )

    def test_invalid_verified_json_fails(self):
        item = self._item(self.project)
        self._comment(
            item,
            'QNT artifact retention: verified\n{"snapshot_id": "x"}',
        )
        self.assertFalse(
            WorkflowService._qnt_artifact_retention_satisfied(item),
            'verified marker with incomplete JSON must fail closed',
        )

    def test_validator_blocks_qnt_without_artifact(self):
        item = self._item(self.project)
        with self.assertRaises(WorkflowValidationError):
            WorkflowService._run_validators(self.transition, item, self.user)

    def test_validator_passes_qnt_with_verified_artifact(self):
        item = self._item(self.project)
        self._comment(item, VALID_VERIFIED)
        WorkflowService._run_validators(self.transition, item, self.user)

    def test_validator_scoped_to_qnt_project(self):
        # Non-QNT autonomous-dev task must NOT be gated.
        item = self._item(self.other_project)
        WorkflowService._run_validators(self.transition, item, self.user)
