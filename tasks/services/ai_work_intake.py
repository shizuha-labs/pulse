"""
AI Work Intake Pipeline (PLS-306 / PLS-270 Slice C).

Natural-language → structured task draft with duplicate discipline.
Pipeline: parse prompt → retrieve context → search duplicates →
propose fields → validate → confirm/reject lifecycle.
"""
import hashlib
import json
import logging
from typing import Any, Optional

from django.db import transaction
from django.utils import timezone

from tasks.models import TaskDraftProposal, Project, Workflow, Item
from tasks.scheduler import TERMINAL_STATUSES

logger = logging.getLogger(__name__)

# ── Required fields that must be present for a valid draft ──
REQUIRED_FIELDS = [
    'proposed_title',
    'proposed_project_id',
    'proposed_workflow_slug',
]

# ── Valid priority values ──
VALID_PRIORITIES = {'urgent', 'high', 'normal', 'medium', 'low'}


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _normalize_title(title: str) -> str:
    """Normalize a title for duplicate comparison."""
    return ' '.join(title.lower().split())


def _title_overlap(a: str, b: str) -> float:
    """Jaccard-like overlap of word tokens between two titles."""
    tokens_a = set(_normalize_title(a).split())
    tokens_b = set(_normalize_title(b).split())
    if not tokens_a or not tokens_b:
        return 0.0
    return len(tokens_a & tokens_b) / len(tokens_a | tokens_b)


def search_duplicates(
    title: str,
    project_id: Optional[int] = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """
    Search for likely duplicate tasks by title overlap.

    Searches active and deferred tasks in the same project (or across all
    projects if project_id is None). Returns candidates with score >= 0.45.
    """
    candidates: list[dict[str, Any]] = []

    qs = Item.objects.all()
    if project_id:
        qs = qs.filter(project_id=project_id)

    # Exclude terminal statuses but include deferred
    qs = qs.exclude(status__in=TERMINAL_STATUSES)

    for item in qs.select_related('project').iterator():
        score = _title_overlap(title, item.title or '')
        if score >= 0.45:
            candidates.append({
                'task_key': item.item_key,
                'title': item.title,
                'status': item.status or 'unknown',
                'score': round(score, 3),
            })

    candidates.sort(key=lambda c: c['score'], reverse=True)
    return candidates[:limit]


def validate_draft(proposal: TaskDraftProposal) -> list[str]:
    """
    Validate a draft proposal. Returns a list of error messages.
    An empty list means the draft is valid.
    """
    errors: list[str] = []

    # Check required fields
    for field in REQUIRED_FIELDS:
        value = getattr(proposal, field, None)
        if not value:
            errors.append(f"Missing required field: {field}")

    # Validate project exists
    if proposal.proposed_project_id:
        if not Project.objects.filter(id=proposal.proposed_project_id).exists():
            errors.append(f"Project {proposal.proposed_project_id} not found")

    # Validate workflow exists
    if proposal.proposed_workflow_slug:
        wf = Workflow.resolve(proposal.proposed_workflow_slug)
        if not wf:
            errors.append(f"Workflow '{proposal.proposed_workflow_slug}' not found")

    # Validate priority
    if proposal.proposed_priority and proposal.proposed_priority.lower() not in VALID_PRIORITIES:
        errors.append(
            f"Invalid priority '{proposal.proposed_priority}'. "
            f"Valid: {', '.join(sorted(VALID_PRIORITIES))}"
        )

    return errors


def create_draft_proposal(
    org_id: int,
    raw_prompt: str,
    *,
    requester_id: Optional[int] = None,
    requester_email: Optional[str] = None,
    source: str = 'mcp',
    proposed_project_id: Optional[int] = None,
    proposed_workflow_slug: str = '',
    proposed_title: str = '',
    proposed_description: str = '',
    proposed_priority: str = '',
    proposed_assignment_group: str = '',
    proposed_labels: Optional[list[str]] = None,
    proposed_tags: Optional[list[str]] = None,
    proposed_parent_item_id: str = '',
    model_used: str = '',
) -> TaskDraftProposal:
    """
    Create a new task draft proposal.

    Runs duplicate search automatically and validates the draft.
    """
    # Run duplicate search
    dupes = search_duplicates(proposed_title, proposed_project_id)

    proposal = TaskDraftProposal.objects.create(
        org_id=org_id,
        requester_id=requester_id,
        requester_email=requester_email,
        source=source,
        raw_prompt=raw_prompt,
        proposed_project_id=proposed_project_id,
        proposed_workflow_slug=proposed_workflow_slug,
        proposed_title=proposed_title,
        proposed_description=proposed_description,
        proposed_priority=proposed_priority,
        proposed_assignment_group=proposed_assignment_group,
        proposed_labels=proposed_labels or [],
        proposed_tags=proposed_tags or [],
        proposed_parent_item_id=proposed_parent_item_id,
        duplicate_candidates=dupes,
        prompt_hash=_sha256(raw_prompt),
        model_used=model_used,
    )

    # Validate
    errors = validate_draft(proposal)
    if errors:
        proposal.validation_status = 'invalid'
        proposal.validation_errors = errors
        proposal.save(update_fields=['validation_status', 'validation_errors'])
    elif dupes and dupes[0]['score'] >= 0.7:
        # High-confidence duplicate → mark as duplicate
        proposal.validation_status = 'duplicate'
        proposal.save(update_fields=['validation_status'])
    else:
        proposal.validation_status = 'valid'
        proposal.save(update_fields=['validation_status'])

    return proposal


@transaction.atomic
def confirm_draft(
    proposal_id: str,
    confirmed_by_id: int,
    *,
    proposal_queryset=None,
) -> tuple[Optional[Item], Optional[str]]:
    """
    Confirm a draft proposal and create a real task.

    Returns (item, error_message). On success, item is the created Item
    and error_message is None. On failure, item is None and error_message
    describes the problem.
    """
    try:
        queryset = proposal_queryset if proposal_queryset is not None else TaskDraftProposal.objects.all()
        proposal = queryset.select_for_update().get(pk=proposal_id)
    except TaskDraftProposal.DoesNotExist:
        return None, f"Draft proposal {proposal_id} not found"

    if proposal.confirmed_item_id:
        return None, f"Draft proposal {proposal_id} already confirmed (item: {proposal.confirmed_item_id})"

    if proposal.rejected_at:
        return None, f"Draft proposal {proposal_id} was already rejected"

    if proposal.validation_status not in ('valid', 'duplicate'):
        return None, (
            f"Draft proposal {proposal_id} has status '{proposal.validation_status}', "
            f"cannot confirm"
        )

    # Resolve project and workflow
    project = Project.objects.filter(id=proposal.proposed_project_id).first()
    if not project:
        return None, f"Project {proposal.proposed_project_id} not found"

    workflow = Workflow.resolve(proposal.proposed_workflow_slug)
    if not workflow:
        return None, f"Workflow '{proposal.proposed_workflow_slug}' not found"

    parent = None
    if proposal.proposed_parent_item_id:
        # Hierarchy is project-owned.  Resolving a parent by global item key
        # would let an otherwise caller-scoped draft write a cross-tenant
        # relation.  The same-project lookup is both the hierarchy invariant
        # and a non-enumerating denial for foreign/missing keys.
        parent = Item.objects.filter(
            project_id=project.id,
            item_key=proposal.proposed_parent_item_id,
        ).first()
        if parent is None:
            return None, "Parent item not found in draft project"

    # Create the item
    item = Item.objects.create(
        project=project,
        item_key=project.get_next_item_key(),
        workflow_name=workflow.name,
        title=proposal.proposed_title,
        description=proposal.proposed_description,
        status=workflow.initial_status.slug if workflow.initial_status else 'open',
        priority=proposal.proposed_priority.lower() if proposal.proposed_priority else 'normal',
        created_by_id=confirmed_by_id,
        parent_item=parent,
    )

    # Update proposal
    proposal.confirmed_item_id = item.item_key
    proposal.confirmed_at = timezone.now()
    proposal.validation_status = 'valid'
    proposal.save(update_fields=['confirmed_item_id', 'confirmed_at', 'validation_status'])

    return item, None


@transaction.atomic
def reject_draft(proposal_id: str, reason: str = '', *, proposal_queryset=None) -> Optional[str]:
    """
    Reject a draft proposal.

    Returns None on success, or an error message string on failure.
    """
    try:
        queryset = proposal_queryset if proposal_queryset is not None else TaskDraftProposal.objects.all()
        proposal = queryset.select_for_update().get(pk=proposal_id)
    except TaskDraftProposal.DoesNotExist:
        return f"Draft proposal {proposal_id} not found"

    if proposal.confirmed_item_id:
        return f"Draft proposal {proposal_id} already confirmed (item: {proposal.confirmed_item_id})"

    proposal.rejected_at = timezone.now()
    proposal.rejected_reason = reason
    proposal.save(update_fields=['rejected_at', 'rejected_reason'])
    return None
