"""#10469 / PLAT-1180 learning-loop incident close gate."""

from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from tasks.services.workflow_service import WorkflowService, WorkflowValidationError


GATE_MESSAGE = (
    "Learning-loop gate (PLAT-1180): incidents cannot close without a linked "
    "prevention/mechanism task (label `prevention-mechanism`, linked via "
    "relates_to/blocks) OR an explicit comment `mechanism: N/A — <reason>`. "
    "Link the mechanism task or record the N/A, then retry."
)


def _transition():
    return SimpleNamespace(
        validators=[
            {
                "type": "linked_labeled_issue_or_comment_marker",
                "target_label": "prevention-mechanism",
                "link_types": ["relates_to", "blocks", "is_blocked_by"],
                "comment_marker_prefix": "mechanism: N/A — ",
                "message": GATE_MESSAGE,
            }
        ],
        from_status="documenting",
        to_status="resolved",
    )


class _Links(list):
    def select_related(self, *_args):
        return self


class _Link(SimpleNamespace):
    pass


class LearningLoopCloseGateTests(SimpleTestCase):
    def _item(self, *, item_id=1, title="Incident", labels=None, tags=None, comments=None):
        return SimpleNamespace(
            id=item_id,
            title=title,
            labels=labels or [],
            tags=tags or "",
            comments=comments or [],
        )

    def _mock_links(self, links):
        return patch("tasks.models.ItemLink.objects.filter", return_value=_Links(links))

    def test_blocks_when_no_mechanism_link_or_na_comment(self):
        with self._mock_links([]):
            with self.assertRaisesRegex(WorkflowValidationError, "Learning-loop gate"):
                WorkflowService._run_validators(_transition(), self._item())

    def test_allows_explicit_na_comment_with_reason(self):
        incident = self._item(comments=[
            SimpleNamespace(
                content="mechanism: N/A — one-off external provider outage; no durable mechanism available"
            )
        ])

        with self._mock_links([]):
            WorkflowService._run_validators(_transition(), incident)

    def test_rejects_na_comment_without_reason(self):
        incident = self._item(comments=[SimpleNamespace(content="mechanism: N/A — ")])

        with self._mock_links([]):
            with self.assertRaises(WorkflowValidationError):
                WorkflowService._run_validators(_transition(), incident)

    def test_allows_outward_link_to_prevention_mechanism_label(self):
        incident = self._item(item_id=1)
        mechanism = self._item(
            item_id=2,
            title="Add watchdog",
            labels=["prevention-mechanism"],
        )
        link = _Link(
            source_item_id=incident.id,
            target_item_id=mechanism.id,
            source_item=incident,
            target_item=mechanism,
            link_type="relates_to",
        )

        with self._mock_links([link]):
            WorkflowService._run_validators(_transition(), incident)

    def test_allows_inward_blocks_link_to_prevention_mechanism_label(self):
        incident = self._item(item_id=1)
        mechanism = self._item(
            item_id=2,
            title="Add recurrence guard",
            labels={"prevention-mechanism": True},
        )
        link = _Link(
            source_item_id=mechanism.id,
            target_item_id=incident.id,
            source_item=mechanism,
            target_item=incident,
            link_type="blocks",
        )

        with self._mock_links([link]):
            WorkflowService._run_validators(_transition(), incident)

    def test_rejects_link_to_unlabeled_task(self):
        incident = self._item(item_id=1)
        unlabeled = self._item(item_id=2, title="Unlabeled follow-up")
        link = _Link(
            source_item_id=incident.id,
            target_item_id=unlabeled.id,
            source_item=incident,
            target_item=unlabeled,
            link_type="relates_to",
        )

        with self._mock_links([link]):
            with self.assertRaises(WorkflowValidationError):
                WorkflowService._run_validators(_transition(), incident)
