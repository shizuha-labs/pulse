"""PLS-305: Agent coordination read model (PLS-270 Slice B).

Projects the agent coordination console from data Pulse already owns — Item
status/assignee/priority, ItemActivity, ItemLink blockers, PR links stored in
``Item.metadata.pull_requests``, the DB-backed TeamRoster, and
AgentAvailability — WITHOUT introducing a second coordination source of
truth. Every field here is derivable from those canonical rows; nothing is
written back.
"""
from datetime import timedelta

from django.utils import timezone

from ..models import AgentAvailability, Item, ItemLink, WorkflowTransition

# Mirrors the fleet WIP cap used by the capacity queue (PLAT-236).
WIP_CAP = 10
# An agent holding WIP whose items saw no activity in this window is flagged
# stale — the console's "silently wedged" signal.
STALE_AFTER_HOURS = 24
# Cap per-agent workstream detail so one hoarder can't balloon the payload.
MAX_WIP_ITEMS = 15

_TERMINAL = {
    'completed', 'cancelled', 'canceled', 'closed', 'done',
    'resolved', 'rejected', 'duplicate', 'wont_fix', 'merged',
    'failed', 'expired', 'deferred',
}
_WAITING = {'blocked', 'scheduled'}


def _norm(s):
    return str(s or '').strip().lower()


def _roster_teams():
    """Live team -> ordered unique member emails from WorkflowService (DB
    TeamRoster overlaid on code defaults)."""
    from .workflow_service import WorkflowService
    teams = {}
    for team, members in WorkflowService._pod_roster().items():
        seen, emails = set(), []
        for email in WorkflowService._flat_roster(members):
            e = _norm(email)
            if e and e not in seen:
                seen.add(e)
                emails.append(e)
        teams[team] = emails
    return teams


def _availability_map():
    out = {}
    for row in AgentAvailability.objects.all():
        out[_norm(row.email)] = {
            'state': 'active' if row.active else (row.status or 'inactive'),
            'reason': row.reason or '',
        }
    return out


def _active_blockers(item_ids):
    """item_id -> [{key, status}] of ACTIVE (non-terminal) blocking items,
    from both directions of the blocks/is_blocked_by pair."""
    out = {}
    links = (ItemLink.objects
             .filter(link_type='is_blocked_by', source_item_id__in=item_ids) |
             ItemLink.objects
             .filter(link_type='blocks', target_item_id__in=item_ids))
    links = links.select_related('source_item', 'target_item')
    for link in links:
        if link.link_type == 'is_blocked_by' and link.source_item_id in item_ids:
            blocked_id, blocker = link.source_item_id, link.target_item
        elif link.link_type == 'blocks' and link.target_item_id in item_ids:
            blocked_id, blocker = link.target_item_id, link.source_item
        else:
            continue
        if _norm(blocker.status) in _TERMINAL:
            continue
        entry = {'key': blocker.item_key or str(blocker.id), 'status': blocker.status}
        if entry not in out.setdefault(blocked_id, []):
            out[blocked_id].append(entry)
    return out


def _pull_requests(item):
    """Open/most recent PR links from the canonical metadata.pull_requests list."""
    prs = (item.metadata or {}).get('pull_requests') or []
    if not isinstance(prs, list):
        return []
    out = []
    for pr in prs:
        if isinstance(pr, dict) and pr.get('url'):
            out.append({'url': pr['url'], 'status': pr.get('status') or 'open'})
    return out


def _next_transitions_map(items):
    """(workflow_name, status) -> [transition names] from the workflow tables.

    Advisory display data only — actual execution still goes through the
    transition API with its validators/permissions; the console never offers
    a bypass.
    """
    pairs = {(_norm(i.workflow_name), _norm(i.status)) for i in items if i.workflow_name}
    if not pairs:
        return {}
    # workflow_name may hold either the slug ("autonomous-bug") or the display
    # name ("Simple Workflow") — same duality WorkflowService resolves. The
    # transition table is small, so fetch once and match in python.
    transitions = (WorkflowTransition.objects
                   .filter(workflow__isnull=False)
                   .select_related('workflow')
                   .only('name', 'from_status', 'to_status', 'workflow__slug', 'workflow__name'))
    out = {}
    for t in transitions:
        frm = _norm(t.from_status)
        wf_keys = {_norm(t.workflow.slug), _norm(t.workflow.name)}
        for wf, status in pairs:
            if (wf in wf_keys or wf.replace(' ', '-') in wf_keys) and (frm == status or frm == '*'):
                names = out.setdefault((wf, status), [])
                if t.name not in names:
                    names.append(t.name)
    return out


def _item_row(item, blockers, transitions_map):
    status = _norm(item.status)
    return {
        'key': item.item_key or str(item.id),
        'title': item.title,
        'status': item.status,
        'priority': item.priority,
        'workflow': item.workflow_name,
        'waiting': status in _WAITING,
        'blockers': blockers.get(item.id, []),
        'pull_requests': _pull_requests(item),
        'next_transitions': transitions_map.get(
            (_norm(item.workflow_name), status), []),
        'updated_at': item.updated_at.isoformat() if item.updated_at else None,
    }


def agent_activity_snapshot(team=None, project_key=None, now=None):
    """The GET /api/items/agent_activity read model.

    Returns roster lanes (incl. review/merge ownership), and per agent:
    availability, WIP items with blockers/PRs/next transitions, WIP-cap
    pressure, and a stale flag for silently-wedged agents.
    """
    now = now or timezone.now()
    stale_cutoff = now - timedelta(hours=STALE_AFTER_HOURS)
    all_teams = _roster_teams()
    teams = all_teams
    if team:
        teams = {t: m for t, m in all_teams.items() if _norm(t) == _norm(team)}
    availability = _availability_map()

    emails = sorted({e for members in teams.values() for e in members})
    items_qs = (Item.objects
                .filter(mode='task', assignee_email__in=emails)
                .exclude(status__in=_TERMINAL)
                .order_by('-updated_at'))
    if project_key:
        items_qs = items_qs.filter(project__key__iexact=project_key)
    items = list(items_qs)
    by_agent = {}
    for item in items:
        by_agent.setdefault(_norm(item.assignee_email), []).append(item)

    blockers = _active_blockers({i.id for i in items})
    transitions_map = _next_transitions_map(items)

    agents = []
    for email in emails:
        wip = by_agent.get(email, [])
        avail = availability.get(email, {'state': 'active', 'reason': ''})
        newest = max((i.updated_at for i in wip if i.updated_at), default=None)
        working = [i for i in wip if _norm(i.status) not in _WAITING]
        agents.append({
            'email': email,
            'teams': sorted(t for t, m in teams.items() if email in m),
            'availability': avail,
            'wip_count': len(working),
            'waiting_count': len(wip) - len(working),
            'wip_cap': WIP_CAP,
            'wip_pressure': round(len(working) / WIP_CAP, 2),
            'stale': bool(
                wip
                and avail['state'] == 'active'
                and (newest is None or newest < stale_cutoff)
            ),
            'last_item_activity_at': newest.isoformat() if newest else None,
            'items': [_item_row(i, blockers, transitions_map) for i in wip[:MAX_WIP_ITEMS]],
            'items_truncated': max(0, len(wip) - MAX_WIP_ITEMS),
        })

    return {
        'generated_at': now.isoformat(),
        'lanes': {
            'review': all_teams.get('review', []),
            'merge': all_teams.get('merge', []),
        },
        'teams': {t: m for t, m in teams.items()},
        'agents': agents,
    }
