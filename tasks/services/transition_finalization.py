"""Shared post-transition effects for inline and deferred HTTP execution."""
import logging
from tasks.models import Comment, WorkflowTransition
from .actionable_items import TERMINAL_STATUS_SLUGS

logger = logging.getLogger(__name__)

def finalize_transition(item, *, old_status, user, payload, screen_data, transition_id):
    # Add optional comment
    comment_text = payload.get('comment')
    if comment_text:
        Comment.objects.create(
            item=item,
            author_id=user.id,
            author_email=getattr(user, 'email', ''),
            content=comment_text,
        )

    # PLS-184: completing high-risk work creates/updates a focused QA
    # verification task without blocking the development workflow.
    if old_status != item.status and item.status in TERMINAL_STATUS_SLUGS:
        try:
            from .focused_qa import ensure_focused_qa_for_item
            ensure_focused_qa_for_item(item, trigger='status_transition')
        except Exception:
            logger.warning("PLS-184 focused QA generation failed after transition", exc_info=True)

    # PLS-184: focused QA failures create/link a separate owning-project
    # autonomous-bug instead of reopening or mutating the originating dev
    # task. Reviewed focused-QA tasks use "Request Changes" as the current
    # failure path until a dedicated QA workflow exists; legacy workflows
    # may expose an explicit "QA Failed" transition.
    if old_status != item.status and item.source == 'focused-qa-automation':
        try:
            executed_transition = None
            if transition_id:
                executed_transition = WorkflowTransition.objects.filter(id=transition_id).first()
            transition_name = (getattr(executed_transition, 'name', '') or '').strip().lower()
            failure_like = (
                item.status in {'failed'}
                or str(screen_data.get('resolution', '')).strip().lower() == 'failed'
                or str(screen_data.get('qa_failure', '')).strip().lower() in {'1', 'true', 'yes'}
                or transition_name == 'qa failed'
                or (
                    transition_name == 'request changes'
                    and old_status in {'in_review', 'in_qa'}
                    and item.status == 'in_progress'
                )
            )
            if failure_like:
                from .focused_qa import create_bug_from_qa_failure
                failure_comment = (
                    payload.get('comment')
                    or screen_data.get('comment')
                    or 'Focused QA verification failed; see QA task evidence/comments.'
                )
                bug = create_bug_from_qa_failure(
                    item,
                    title=f"[QA failure] {item.title}",
                    reproduction=failure_comment,
                    evidence_url=screen_data.get('evidence_url') or item.source_url or '',
                )
                Comment.objects.get_or_create(
                    item=item,
                    author_id=0,
                    author_email='qa-automation@shizuha.com',
                    content=(
                        f"Focused QA failure recorded as **{bug.item_key or bug.pk}**. "
                        "The originating development task remains unchanged; engineering should work the linked bug."
                    ),
                )
        except Exception:
            logger.warning("PLS-184 focused QA failure bug creation failed after transition", exc_info=True)
