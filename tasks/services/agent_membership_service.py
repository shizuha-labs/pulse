"""
AgentProjectMembership service (EVOL-20 Ph0a — HLD §2, page ab8fc292 v2).

All membership mutations go through here so every change emits an
AgentProjectMembershipActivity audit row in the SAME transaction — memberships
are org decisions, never silent state (HLD §2). The model is inert in Ph0;
Ph1 (queue ordering) and Ph2 (resolver) read it later.

The SIGNED cap (<= 4 active memberships/agent) is enforced via Model.full_clean()
in create/activate paths.
"""
import logging

from django.db import transaction
from django.utils import timezone

from ..models import AgentProjectMembership, AgentProjectMembershipActivity

logger = logging.getLogger(__name__)


def _record(membership, action, *, actor_user_id=None, actor_email='',
            reason='', field_name='', old_value='', new_value=''):
    """Write one audit row for a membership change."""
    return AgentProjectMembershipActivity.objects.create(
        membership=membership,
        action=action,
        field_name=field_name,
        old_value='' if old_value is None else str(old_value),
        new_value='' if new_value is None else str(new_value),
        actor_user_id=actor_user_id,
        actor_email=actor_email or '',
        reason=reason or '',
    )


@transaction.atomic
def create_membership(project, *, agent_user_email, agent_user_id=None,
                      tier='primary', is_lead=False, reason='',
                      actor_user_id=None, actor_email=''):
    """
    Create an active membership (cap-checked) and write its 'created' audit row.

    Raises django.core.exceptions.ValidationError if the agent is already at the
    active-membership cap (HLD §2).
    """
    membership = AgentProjectMembership(
        project=project,
        agent_user_id=agent_user_id,
        agent_user_email=agent_user_email,
        tier=tier,
        is_lead=is_lead,
        active=True,
        created_by_id=actor_user_id,
        created_by_email=actor_email or '',
        reason=reason or '',
    )
    membership.full_clean()  # enforces the <=4 active cap + field validation
    membership.save()
    _record(membership, 'created', actor_user_id=actor_user_id,
            actor_email=actor_email, reason=reason,
            new_value=f"{agent_user_email}@{project.key}:{tier}{'/lead' if is_lead else ''}")
    logger.info("EVOL-20: created agent membership %s @ %s (%s)",
                agent_user_email, project.key, tier)
    return membership


@transaction.atomic
def expire_membership(membership, *, reason='', actor_user_id=None, actor_email=''):
    """Deactivate + stamp expires_at, writing an 'expired' audit row. Idempotent."""
    if not membership.active:
        return membership
    membership.active = False
    membership.expires_at = timezone.now()
    membership.save(update_fields=['active', 'expires_at', 'updated_at'])
    _record(membership, 'expired', actor_user_id=actor_user_id,
            actor_email=actor_email, reason=reason,
            field_name='active', old_value='True', new_value='False')
    logger.info("EVOL-20: expired agent membership %s @ %s",
                membership.agent_user_email, membership.project_id)
    return membership


@transaction.atomic
def update_membership(membership, *, actor_user_id=None, actor_email='', reason='', **changes):
    """
    Apply field changes (tier, is_lead, active, agent_user_email) and write one
    audit row per changed field. Re-activating runs full_clean() so the cap still
    binds. agent_user_email is mutable so a derived row whose stored email went
    stale after identity drift can be re-pointed at the canonical address; the
    numeric agent_user_id is intentionally NOT mutable here (it backs the
    (project, agent_user_id) unique constraint).
    """
    field_action = {'tier': 'tier_changed', 'is_lead': 'lead_changed',
                    'active': None,  # active resolved to activated/deactivated below
                    'agent_user_email': 'email_changed',
                    'expires_at': 'expiry_cleared'}
    dirty = []
    for field, new in changes.items():
        if field not in field_action:
            raise ValueError(f"update_membership: unsupported field {field!r}")
        old = getattr(membership, field)
        if old == new:
            continue
        setattr(membership, field, new)
        dirty.append((field, old, new))

    # Reactivation must clear a stale past expires_at (Codex P2): expire_membership()
    # stamps expires_at=now() on deactivation, so reviving a row via active=True
    # without nulling it leaves the active membership marked already-expired — an
    # expiry sweep/operator review would flap it straight back to inactive. Auto-null
    # on every active→True (unless the caller is explicitly setting expires_at).
    if (any(f == 'active' and v is True for f, _, v in dirty)
            and 'expires_at' not in changes and membership.expires_at is not None):
        dirty.append(('expires_at', membership.expires_at, None))
        membership.expires_at = None

    if not dirty:
        return membership

    # Re-validate the cap on re-activation OR an identity rekey (Codex P2):
    # changing agent_user_email is identity-changing — repointing an active row to
    # a canonical email that already holds MAX_ACTIVE memberships would otherwise
    # save a cap-exceeding row, since the email is the cap key.
    if any((f == 'active' and v is True) or f == 'agent_user_email' for f, _, v in dirty):
        membership.full_clean()  # re-activation / rekey must respect the cap

    membership.save()
    for field, old, new in dirty:
        if field == 'active':
            action = 'activated' if new else 'deactivated'
        else:
            action = field_action[field]
        _record(membership, action, actor_user_id=actor_user_id,
                actor_email=actor_email, reason=reason,
                field_name=field, old_value=old, new_value=new)
    return membership
