"""PLAT-10524: page an executor-capable rung when a priority PR sits unreviewed.

A page to a capability-void review lead is a silent drop. The alarm must
reach a capable rung inside the same decision, or it is a mechanism failure.
"""

from __future__ import annotations

THRESHOLDS_HOURS = {
    'urgent': 4,
    'high': 12,
}

DEFAULT_RUNGS = (
    'review-lead',
    'merge-lane',
    'operator-oob',
)


def stale_pr_review_decision(
    *,
    priority: str,
    ci_green: bool,
    review_count: int,
    age_hours: float,
    rungs: list[dict] | None = None,
) -> dict:
    """Decide whether a head needs a routed page.

    ``rungs`` is an ordered list of ``{'name', 'capable'}``. The first capable
    rung is the page target. If the episode qualifies and no rung is capable,
    ``mechanism_failure`` is true (paged-but-unroutable).
    """
    priority = (priority or '').strip().lower()
    threshold = THRESHOLDS_HOURS.get(priority)
    qualifies = bool(
        threshold is not None
        and ci_green
        and review_count == 0
        and age_hours >= threshold
    )
    chain = rungs if rungs is not None else [
        {'name': name, 'capable': True} for name in DEFAULT_RUNGS
    ]
    target = None
    if qualifies:
        for rung in chain:
            if rung.get('capable'):
                target = rung.get('name')
                break
    return {
        'qualifies': qualifies,
        'threshold_hours': threshold,
        'target': target,
        'mechanism_failure': qualifies and target is None,
    }


def dispatch_stale_pr_review_alarms(candidates, page, rungs=None):
    """Page once per qualifying head. ``page(target, candidate, decision)`` is the sink.

    A qualifying head with no capable rung is returned as a failure and is
    not passed to ``page`` — a void page is not delivery.
    """
    paged = []
    failures = []
    for candidate in candidates:
        decision = stale_pr_review_decision(
            priority=candidate.get('priority', ''),
            ci_green=bool(candidate.get('ci_green')),
            review_count=int(candidate.get('review_count') or 0),
            age_hours=float(candidate.get('age_hours') or 0),
            rungs=rungs if rungs is not None else candidate.get('rungs'),
        )
        if decision['mechanism_failure']:
            failures.append({'candidate': candidate, 'decision': decision})
            continue
        if not decision['qualifies']:
            continue
        page(decision['target'], candidate, decision)
        paged.append({'target': decision['target'], 'candidate': candidate})
    return {'paged': paged, 'failures': failures}


_REVIEW_STATES = {'approved', 'change_requested', 'changes_requested', 'request_changes'}


def review_record_count(reviews) -> int:
    """Count APPROVED / CHANGE_REQUESTED records. Comment-only reviews do not count."""
    count = 0
    for review in reviews or []:
        state = str((review or {}).get('state') or '').strip().lower().replace(' ', '_')
        if state in _REVIEW_STATES:
            count += 1
    return count


def ci_is_all_green(status_payload) -> bool:
    """Combined commit status must be success. Missing or pending is not green."""
    if not isinstance(status_payload, dict):
        return False
    return str(status_payload.get('state') or '').strip().lower() == 'success'


def age_hours_since(head_changed_at, now) -> float:
    if head_changed_at is None or now is None:
        return 0.0
    delta = now - head_changed_at
    return max(0.0, delta.total_seconds() / 3600.0)


def candidate_from_facts(*, priority, repo, number, head, ci_green, reviews, head_changed_at, now, rungs):
    return {
        'priority': priority,
        'repo': repo,
        'number': number,
        'head': head,
        'ci_green': ci_green,
        'review_count': review_record_count(reviews),
        'age_hours': age_hours_since(head_changed_at, now),
        'rungs': rungs,
    }
