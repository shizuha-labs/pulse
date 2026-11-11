"""PLAT-10351: carry keyed-chain state onto the ticket at assignment transfer."""

def chain_snapshot(item):
    """Best-effort chain position, receipt hash, and next action.

    Missing fields stay explicit. The handoff must not invent a hash.
    """
    meta = getattr(item, 'metadata', None) or {}
    if not isinstance(meta, dict):
        meta = {}
    position = str(
        meta.get('keyed_chain_position')
        or meta.get('chain_position')
        or getattr(item, 'status', '')
        or 'unknown'
    )
    receipt = 'none'
    for key in ('trail_pointer', 'evidence', 'latest_receipt'):
        blob = meta.get(key)
        if isinstance(blob, dict):
            digest = str(blob.get('content_address') or '').strip()
            if digest:
                receipt = digest
                break
    status = getattr(item, 'status', '') or 'unknown'
    next_action = str(
        meta.get('next_action')
        or f"continue from status {status}; do not re-derive the chain from chat"
    )
    return position, receipt, next_action


def record_keyed_chain_handoff(item, previous_email):
    """Post one lifecycle comment when a live owner is replaced."""
    previous = str(previous_email or '').strip()
    current = str(getattr(item, 'assignee_email', '') or '').strip()
    if not previous or not current or previous.lower() == current.lower():
        return None
    if getattr(item, '_suppress_keyed_chain_handoff', False):
        return None
    position, receipt, next_action = chain_snapshot(item)
    from .mechanical_comments import MechanicalCommentFactory
    from ..models import Comment

    updated = getattr(item, 'updated_at', None)
    stamp = updated.isoformat() if updated is not None else 'unspecified'
    # origin_event_id is capped at 255. Hash the variable tail so long emails
    # cannot fail the factory and poison the assignment write.
    import hashlib
    tail = f"{item.pk}:{previous.lower()}:{current.lower()}:{stamp}"
    digest = hashlib.sha256(tail.encode()).hexdigest()[:32]
    return MechanicalCommentFactory.create(
        item=item,
        origin_kind=Comment.ORIGIN_WORKFLOW_LIFECYCLE,
        origin_event='assignment.keyed_chain_handoff',
        template_version=1,
        structured_payload={
            'previous_email': previous,
            'assignee_email': current,
            'chain_position': position,
            'receipt_hash': receipt,
            'next_action': next_action,
            'status': str(getattr(item, 'status', '') or 'unknown'),
        },
        origin_event_id=f"keyed-chain:{digest}",
    )
