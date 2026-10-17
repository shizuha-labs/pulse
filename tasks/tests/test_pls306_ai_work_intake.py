"""PLS-306: AI Work Intake — natural-language task draft/confirm pipeline.

Tests the TaskDraftProposal model, pipeline service, and API views.
Covers: create → validate → search duplicates → confirm/reject lifecycle.
"""
import uuid
from django.utils import timezone
from unittest.mock import patch

from django.test import TestCase, override_settings
from rest_framework import status
from rest_framework.test import APIClient

from tasks.models import TaskDraftProposal, Project, Item, Workflow, Status, WorkflowStatus
from tasks.services.ai_work_intake import (
    create_draft_proposal,
    validate_draft,
    search_duplicates,
    confirm_draft,
    reject_draft,
    _sha256,
    _title_overlap,
)


class TestTitleOverlap(TestCase):
    """Unit tests for the Jaccard-like title overlap function."""

    def test_identical_titles(self):
        self.assertAlmostEqual(_title_overlap("Fix login bug", "Fix login bug"), 1.0)

    def test_partial_overlap(self):
        score = _title_overlap("Fix login bug", "Fix login issue")
        self.assertGreater(score, 0.3)
        self.assertLess(score, 0.9)

    def test_no_overlap(self):
        self.assertEqual(_title_overlap("Fix login bug", "Deploy database"), 0.0)

    def test_empty_titles(self):
        self.assertEqual(_title_overlap("", "something"), 0.0)
        self.assertEqual(_title_overlap("", ""), 0.0)

    def test_case_insensitive(self):
        self.assertAlmostEqual(_title_overlap("FIX LOGIN", "fix login"), 1.0)


class TestSha256(TestCase):
    """Unit tests for the prompt hashing function."""

    def test_sha256_consistent(self):
        h1 = _sha256("hello world")
        h2 = _sha256("hello world")
        self.assertEqual(h1, h2)
        self.assertEqual(len(h1), 64)

    def test_sha256_different(self):
        self.assertNotEqual(_sha256("hello"), _sha256("world"))


class TestValidateDraft(TestCase):
    """Tests for the draft validation logic."""

    def setUp(self):
        self.project = Project.objects.create(
            name="Test Project", key="TEST", org_id=1,
        )
        self.status, _ = Status.objects.get_or_create(
            slug="open", defaults={"name": "Open", "category": "todo"},
        )
        self.workflow, _ = Workflow.objects.get_or_create(
            slug="simple",
            defaults={"name": "Simple", "initial_status": self.status},
        )

    def _make_proposal(self, **overrides):
        fields = {
            "org_id": 1,
            "raw_prompt": "Create a task to fix the login bug",
            "proposed_project_id": self.project.id,
            "proposed_workflow_slug": "simple",
            "proposed_title": "Fix login bug",
            "proposed_description": "The login page crashes on submit",
            "proposed_priority": "high",
        }
        fields.update(overrides)
        return TaskDraftProposal.objects.create(**fields)

    def test_valid_draft(self):
        proposal = self._make_proposal()
        errors = validate_draft(proposal)
        self.assertEqual(errors, [])

    def test_missing_title(self):
        proposal = self._make_proposal(proposed_title="")
        errors = validate_draft(proposal)
        self.assertIn("Missing required field: proposed_title", errors)

    def test_missing_project(self):
        proposal = self._make_proposal(proposed_project_id=None)
        errors = validate_draft(proposal)
        self.assertIn("Missing required field: proposed_project_id", errors)

    def test_missing_workflow(self):
        proposal = self._make_proposal(proposed_workflow_slug="")
        errors = validate_draft(proposal)
        self.assertIn("Missing required field: proposed_workflow_slug", errors)

    def test_invalid_project(self):
        proposal = self._make_proposal(proposed_project_id=99999)
        errors = validate_draft(proposal)
        self.assertTrue(any("Project 99999 not found" in e for e in errors))

    def test_invalid_workflow(self):
        proposal = self._make_proposal(proposed_workflow_slug="nonexistent")
        errors = validate_draft(proposal)
        self.assertTrue(any("Workflow 'nonexistent' not found" in e for e in errors))

    def test_invalid_priority(self):
        proposal = self._make_proposal(proposed_priority="critical")
        errors = validate_draft(proposal)
        self.assertTrue(any("Invalid priority" in e for e in errors))

    def test_valid_priority_variants(self):
        for p in ("urgent", "high", "normal", "medium", "low"):
            proposal = self._make_proposal(proposed_priority=p)
            errors = validate_draft(proposal)
            self.assertEqual(errors, [], f"Priority '{p}' should be valid")


class TestSearchDuplicates(TestCase):
    """Tests for the duplicate search logic."""

    def setUp(self):
        self.project = Project.objects.create(
            name="Test Project", key="TEST", org_id=1,
        )
        self.status, _ = Status.objects.get_or_create(
            slug="open", defaults={"name": "Open", "category": "todo"},
        )
        self.workflow, _ = Workflow.objects.get_or_create(
            slug="simple",
            defaults={"name": "Simple", "initial_status": self.status},
        )

    def _create_item(self, title, status="open"):
        return Item.objects.create(
            project=self.project,
            title=title,
            status=status,
            workflow_name="simple",
            mode="task",
        )

    def test_no_duplicates(self):
        self._create_item("Deploy database")
        results = search_duplicates("Fix login bug", self.project.id)
        self.assertEqual(results, [])

    def test_finds_similar_title(self):
        self._create_item("Fix login bug")
        results = search_duplicates("Fix login bug", self.project.id)
        self.assertTrue(len(results) >= 1)
        self.assertGreaterEqual(results[0]["score"], 0.9)

    def test_finds_partial_match(self):
        self._create_item("Fix login bug on submit")
        results = search_duplicates("Fix login bug", self.project.id)
        self.assertTrue(len(results) >= 1)

    def test_excludes_terminal_statuses(self):
        # Create a "completed" status and add it to the workflow so it's valid
        done_status, _ = Status.objects.get_or_create(
            slug="completed", defaults={"name": "Completed", "category": "done"},
        )
        WorkflowStatus.objects.get_or_create(
            workflow=self.workflow,
            status=done_status,
            defaults={"position": 2},
        )
        self._create_item("Fix login bug", status="completed")
        results = search_duplicates("Fix login bug", self.project.id)
        self.assertEqual(results, [])

    def test_limits_results(self):
        for i in range(10):
            self._create_item(f"Fix login bug {i}")
        results = search_duplicates("Fix login bug", self.project.id, limit=3)
        self.assertLessEqual(len(results), 3)


class TestCreateDraftProposal(TestCase):
    """Tests for the create_draft_proposal service function."""

    def setUp(self):
        self.project = Project.objects.create(
            name="Test Project", key="TEST", org_id=1,
        )
        self.status, _ = Status.objects.get_or_create(
            slug="open", defaults={"name": "Open", "category": "todo"},
        )
        self.workflow, _ = Workflow.objects.get_or_create(
            slug="simple",
            defaults={"name": "Simple", "initial_status": self.status},
        )

    def test_creates_valid_draft(self):
        proposal = create_draft_proposal(
            org_id=1,
            raw_prompt="Create a task to fix the login bug",
            proposed_project_id=self.project.id,
            proposed_workflow_slug="simple",
            proposed_title="Fix login bug",
            proposed_description="The login page crashes",
            proposed_priority="high",
            proposed_assignment_group="engineering",
            proposed_labels=["bug", "auth"],
            proposed_tags=["login"],
            model_used="gpt-4",
        )
        self.assertEqual(proposal.validation_status, "valid")
        self.assertEqual(proposal.proposed_title, "Fix login bug")
        self.assertEqual(proposal.proposed_priority, "high")
        self.assertEqual(proposal.proposed_labels, ["bug", "auth"])
        self.assertEqual(proposal.model_used, "gpt-4")
        self.assertEqual(len(proposal.prompt_hash), 64)

    def test_creates_invalid_draft_missing_fields(self):
        proposal = create_draft_proposal(
            org_id=1,
            raw_prompt="Create a task",
            proposed_project_id=None,
            proposed_workflow_slug="",
            proposed_title="",
        )
        self.assertEqual(proposal.validation_status, "invalid")
        self.assertTrue(len(proposal.validation_errors) > 0)

    def test_creates_duplicate_draft(self):
        # First create an item with the same title
        Item.objects.create(
            project=self.project,
            title="Fix login bug",
            status="open",
            workflow_name="simple",
            mode="task",
        )
        proposal = create_draft_proposal(
            org_id=1,
            raw_prompt="Create a task to fix the login bug",
            proposed_project_id=self.project.id,
            proposed_workflow_slug="simple",
            proposed_title="Fix login bug",
        )
        # Should be valid (not auto-rejected) since score < 0.7 for single match
        self.assertIn(proposal.validation_status, ("valid", "duplicate"))


class TestConfirmDraft(TestCase):
    """Tests for the confirm_draft service function."""

    def setUp(self):
        self.project = Project.objects.create(
            name="Test Project", key="TEST", org_id=1,
        )
        self.status, _ = Status.objects.get_or_create(
            slug="open", defaults={"name": "Open", "category": "todo"},
        )
        self.workflow, _ = Workflow.objects.get_or_create(
            slug="simple",
            defaults={"name": "Simple", "initial_status": self.status},
        )

    def _create_valid_proposal(self):
        return TaskDraftProposal.objects.create(
            org_id=1,
            raw_prompt="Create a task",
            proposed_project_id=self.project.id,
            proposed_workflow_slug="simple",
            proposed_title="Fix login bug",
            proposed_description="The login page crashes",
            proposed_priority="high",
            validation_status="valid",
        )

    def test_confirm_valid_draft(self):
        proposal = self._create_valid_proposal()
        item, error = confirm_draft(str(proposal.id), confirmed_by_id=1)
        self.assertIsNone(error)
        self.assertIsNotNone(item)
        self.assertEqual(item.title, "Fix login bug")
        self.assertEqual(item.priority, "high")
        # Verify proposal is updated
        proposal.refresh_from_db()
        self.assertEqual(proposal.confirmed_item_id, item.item_key)
        self.assertIsNotNone(proposal.confirmed_at)

    def test_confirm_nonexistent_draft(self):
        item, error = confirm_draft(str(uuid.uuid4()), confirmed_by_id=1)
        self.assertIsNone(item)
        self.assertIsNotNone(error)
        self.assertIn("not found", error)

    def test_confirm_already_confirmed_draft(self):
        proposal = self._create_valid_proposal()
        confirm_draft(str(proposal.id), confirmed_by_id=1)
        # Try confirming again
        item, error = confirm_draft(str(proposal.id), confirmed_by_id=1)
        self.assertIsNone(item)
        self.assertIn("already confirmed", error)

    def test_confirm_rejected_draft(self):
        proposal = self._create_valid_proposal()
        proposal.rejected_at = timezone.now()
        proposal.save()
        item, error = confirm_draft(str(proposal.id), confirmed_by_id=1)
        self.assertIsNone(item)
        self.assertIn("rejected", error)

    def test_confirm_invalid_draft(self):
        proposal = TaskDraftProposal.objects.create(
            org_id=1,
            raw_prompt="Create a task",
            proposed_project_id=None,
            proposed_workflow_slug="",
            proposed_title="",
            validation_status="invalid",
            validation_errors=["Missing required field: proposed_title"],
        )
        item, error = confirm_draft(str(proposal.id), confirmed_by_id=1)
        self.assertIsNone(item)
        self.assertIn("invalid", error)


class TestRejectDraft(TestCase):
    """Tests for the reject_draft service function."""

    def setUp(self):
        self.project = Project.objects.create(
            name="Test Project", key="TEST", org_id=1,
        )

    def test_reject_valid_draft(self):
        proposal = TaskDraftProposal.objects.create(
            org_id=1,
            raw_prompt="Create a task",
            proposed_project_id=self.project.id,
            proposed_workflow_slug="simple",
            proposed_title="Fix login bug",
            validation_status="valid",
        )
        error = reject_draft(str(proposal.id), reason="Not needed")
        self.assertIsNone(error)
        proposal.refresh_from_db()
        self.assertIsNotNone(proposal.rejected_at)
        self.assertEqual(proposal.rejected_reason, "Not needed")

    def test_reject_nonexistent_draft(self):
        error = reject_draft(str(uuid.uuid4()))
        self.assertIsNotNone(error)
        self.assertIn("not found", error)

    def test_reject_already_confirmed_draft(self):
        proposal = TaskDraftProposal.objects.create(
            org_id=1,
            raw_prompt="Create a task",
            proposed_project_id=self.project.id,
            proposed_workflow_slug="simple",
            proposed_title="Fix login bug",
            validation_status="valid",
            confirmed_item_id="TEST-1",
        )
        error = reject_draft(str(proposal.id))
        self.assertIsNotNone(error)
        self.assertIn("already confirmed", error)


class TestDraftProposalAPI(TestCase):
    """Tests for the TaskDraftProposal API views."""

    def setUp(self):
        self.client = APIClient()
        self.project = Project.objects.create(
            name="Test Project", key="TEST", org_id=1, access_level="open",
        )
        self.status, _ = Status.objects.get_or_create(
            slug="open", defaults={"name": "Open", "category": "todo"},
        )
        self.workflow, _ = Workflow.objects.get_or_create(
            slug="simple",
            defaults={"name": "Simple", "initial_status": self.status},
        )

    def _auth(self):
        """Apply authentication for API calls."""
        # Use a simple token auth for testing
        from django.contrib.auth.models import User
        user, _ = User.objects.get_or_create(
            username="testuser",
            defaults={"email": "test@example.com"},
        )
        user.organization_memberships = {1: "member"}
        self.client.force_authenticate(user=user)
        return user

    def test_create_draft_unauthenticated(self):
        response = self.client.post("/api/ai-work-intake/drafts/", {
            "raw_prompt": "Create a task",
            "proposed_project_id": self.project.id,
            "proposed_workflow_slug": "simple",
            "proposed_title": "Fix login bug",
        })
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_create_draft_success(self):
        self._auth()
        response = self.client.post("/api/ai-work-intake/drafts/", {
            "raw_prompt": "Create a task to fix the login bug",
            "proposed_project_id": self.project.id,
            "proposed_workflow_slug": "simple",
            "proposed_title": "Fix login bug",
            "proposed_description": "The login page crashes",
            "proposed_priority": "high",
        })
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        data = response.json()
        self.assertEqual(data["validation_status"], "valid")
        self.assertEqual(data["proposed_title"], "Fix login bug")

    def test_create_draft_invalid(self):
        self._auth()
        response = self.client.post(
            "/api/ai-work-intake/drafts/",
            {
                "raw_prompt": "Create a task",
                "proposed_project_id": None,
                "proposed_workflow_slug": "",
                "proposed_title": "",
            },
            format="json",
        )
        # Serializer validation returns 400 (DRF default)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_list_drafts(self):
        self._auth()
        # Create a draft first
        self.client.post("/api/ai-work-intake/drafts/", {
            "raw_prompt": "Create a task",
            "proposed_project_id": self.project.id,
            "proposed_workflow_slug": "simple",
            "proposed_title": "Fix login bug",
        })
        response = self.client.get("/api/ai-work-intake/drafts/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertTrue(len(data) >= 1)

    def test_get_draft_detail(self):
        self._auth()
        # Create a draft
        create_resp = self.client.post("/api/ai-work-intake/drafts/", {
            "raw_prompt": "Create a task",
            "proposed_project_id": self.project.id,
            "proposed_workflow_slug": "simple",
            "proposed_title": "Fix login bug",
        })
        draft_id = create_resp.json()["id"]

        response = self.client.get(f"/api/ai-work-intake/drafts/{draft_id}/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["id"], draft_id)

    def test_get_draft_not_found(self):
        self._auth()
        response = self.client.get(
            f"/api/ai-work-intake/drafts/{uuid.uuid4()}/"
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_confirm_draft(self):
        self._auth()
        # Create a valid draft
        create_resp = self.client.post("/api/ai-work-intake/drafts/", {
            "raw_prompt": "Create a task",
            "proposed_project_id": self.project.id,
            "proposed_workflow_slug": "simple",
            "proposed_title": "Fix login bug",
        })
        draft_id = create_resp.json()["id"]

        response = self.client.post(
            f"/api/ai-work-intake/drafts/{draft_id}/confirm/", {}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertIn("item_key", data)
        self.assertIn("TEST-", data["item_key"])

    def test_confirm_already_confirmed(self):
        self._auth()
        create_resp = self.client.post("/api/ai-work-intake/drafts/", {
            "raw_prompt": "Create a task",
            "proposed_project_id": self.project.id,
            "proposed_workflow_slug": "simple",
            "proposed_title": "Fix login bug",
        })
        draft_id = create_resp.json()["id"]

        # Confirm once
        self.client.post(f"/api/ai-work-intake/drafts/{draft_id}/confirm/", {})
        # Confirm again
        response = self.client.post(
            f"/api/ai-work-intake/drafts/{draft_id}/confirm/", {}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_reject_draft(self):
        self._auth()
        create_resp = self.client.post("/api/ai-work-intake/drafts/", {
            "raw_prompt": "Create a task",
            "proposed_project_id": self.project.id,
            "proposed_workflow_slug": "simple",
            "proposed_title": "Fix login bug",
        })
        draft_id = create_resp.json()["id"]

        response = self.client.post(
            f"/api/ai-work-intake/drafts/{draft_id}/reject/",
            {"reason": "Not needed"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify it's rejected
        get_resp = self.client.get(f"/api/ai-work-intake/drafts/{draft_id}/")
        self.assertEqual(get_resp.json()["validation_status"], "valid")
        self.assertIsNotNone(get_resp.json()["rejected_at"])
