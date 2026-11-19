"""PLAT-13562: team-authority helpers and the out-of-team mutation detective.

Two legs of the gen-13 game-day remediation:

- ``actor_on_item_team`` — the shared team-authority predicate. A seat may
  transition an item when it is the item's assignee, a roster member of the
  item's ``assignment_group`` (org-scoped), or staff (operator path). Empty
  ``assignment_group`` has no team boundary — the project-permission check
  governs. The transition route (tasks.views.TransitionExecuteView) enforces
  this as a 403 gate.

- ``maybe_alert_out_of_team_transition`` — the detective control. The gate
  blocks the HTTP route, but any mutation that reaches the workflow engine
  from a NON-route path (service-level callers, future regressions, bypasses)
  without team authority is by definition anomalous: this raises a deduped
  anomaly alert so the class stays detectable even when a gate is bypassed.
  Fail-open by contract — alerting must never break a committed transition.
"""
import logging

logger = logging.getLogger(__name__)

# System actors (celery sweeps, sync tasks, backfills) act on items across
# teams by design; they are userless or carry *.local service emails.
_SYSTEM_EMAIL_SUFFIXES = ('.shizuha.local', '@shizuha.local')


def actor_on_item_team(item, actor_email):
    """True when ``actor_email`` holds team authority over ``item``.

    Authority = the item's assignee, or a roster member of the item's
    ``assignment_group`` bound to the item's project org. An empty
    ``assignment_group`` has no team boundary (returns True — the project
    visibility check governs there).
    """
    team = str(getattr(item, 'assignment_group', '') or '').strip().lower()
    if not team:
        return True
    actor = str(actor_email or '').strip().lower()
    if not actor:
        return False
    if actor == str(getattr(item, 'assignee_email', '') or '').strip().lower():
        return True
    from .permissions import _team_roster_bindings_for_email
    org_id = getattr(getattr(item, 'project', None), 'org_id', None)
    bindings = _team_roster_bindings_for_email(actor)
    return (org_id, team) in bindings or (
        org_id is None and any(name == team for _, name in bindings)
    )


def maybe_alert_out_of_team_transition(item, user, old_status):
    """Detective control: anomaly-alert an out-of-team workflow mutation.

    Called AFTER a committed status change. Alerts only when the actor is a
    real (non-system, non-staff) seat that holds NO team authority over the
    item — such a transition can no longer come from the HTTP route (the
    PLAT-13562 gate refuses it there with 403), so its presence means a
    bypass or a service-level caller that must be surfaced. Fail-open:
    any error is logged and swallowed.
    """
    try:
        if old_status == item.status:
            return None  # no mutation to detect
        if user is None or getattr(user, 'id', None) in (None, 0):
            return None  # system actor
        email = str(getattr(user, 'email', '') or '')
        if not email or email.lower().endswith(_SYSTEM_EMAIL_SUFFIXES):
            return None  # service identity
        if getattr(user, 'is_staff', False):
            return None  # operator path is by design, not an anomaly
        if actor_on_item_team(item, email):
            return None  # same-team or assignee — normal
        from .deduplication import AlertDeduplicationService

        item_label = getattr(item, 'item_key', None) or str(getattr(item, 'pk', '?'))
        team = str(getattr(item, 'assignment_group', '') or '')
        description = (
            'PLAT-13562 detective: an out-of-team workflow mutation was '
            'committed.\n\n'
            f'- item: `{item_label}`\n'
            f'- status: `{old_status}` -> `{item.status}`\n'
            f'- actor: `{email}`\n'
            f'- item team: `{team}`\n\n'
            'The HTTP transition route refuses cross-team transitions with '
            '403 (PLAT-13562 gate), so this mutation bypassed the route or '
            'came from a service-level caller. Investigate as an '
            'authorization anomaly; revert if unintended.'
        )
        alert, _is_new = AlertDeduplicationService.submit_alert({
            'item_type': 'alert.error',
            'title': f'Cross-team workflow mutation on {item_label}',
            'description': description,
            'source': 'pulse-workflow-authz',
            'source_id': f'out-of-team-transition:{getattr(item, "pk", "?")}:{email.lower()}',
            'severity': 'warning',
            'priority': 'high',
            'assignment_group': 'security',
            'labels': {
                'service': 'pulse-workflow-authz',
                'finding': 'PLAT-13562',
                'item': str(item_label),
                'actor': email.lower(),
                'team': team,
            },
            'payload': {
                'item': str(item_label),
                'old_status': old_status,
                'new_status': item.status,
                'actor': email.lower(),
                'team': team,
            },
        })
        if _is_new:
            try:
                from .. import tasks as pulse_tasks
                pulse_tasks.send_alert_notification.delay(alert.id)
            except Exception:
                logger.exception(
                    'PLAT-13562 detective: failed to notify anomaly alert for %s',
                    item_label,
                )
        logger.warning(
            'PLAT-13562 out-of-team transition committed item=%s team=%s actor=%s',
            item_label, team, email,
        )
        return alert
    except Exception:
        logger.exception(
            'PLAT-13562 detective control failed (fail-open) for item=%s',
            getattr(item, 'item_key', getattr(item, 'pk', '?')),
        )
        return None
