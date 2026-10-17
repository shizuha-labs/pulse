"""PLS-634: harden Accept — Capacity-Held transitions (idempotent).

Ensures every Accept — Capacity-Held transition has:
  - accept_capacity_held validator
  - required plan screen fields
  - allowed_roles=['architecture']
  - accept_capacity_held post-function (no empty plan_hash constants)
"""
from django.db import migrations


VALIDATOR = {
    'type': 'accept_capacity_held',
    'message': (
        'Accept — Capacity-Held gate (PLS-481): the item does not '
        'satisfy the acceptance criteria. A current PublicationBinding '
        'with Architecture + Security role proofs, digest-verified '
        'plan_document, and capacity_reason is required.'
    ),
}

SCREEN_FIELDS = [
    {
        'field': 'plan_hash',
        'label': 'Content-addressed plan hash',
        'required': True,
    },
    {
        'field': 'plan_document',
        'label': 'Immutable plan document (JSON)',
        'required': True,
    },
    {
        'field': 'capacity_reason',
        'label': 'Capacity hold reason',
        'required': True,
    },
]


def _is_accept_capacity_held(transition):
    name = (transition.name or '').strip().lower().replace('—', '-').replace('–', '-')
    to_status = (transition.to_status or '').strip().lower().replace('-', '_')
    if to_status != 'accepted_capacity_held':
        return False
    if 'accept' in name and 'capacity' in name:
        return True
    for pf in list(transition.post_functions or []):
        if isinstance(pf, dict) and pf.get('type') == 'accept_capacity_held':
            return True
    return False


def harden_accept_capacity_held(apps, schema_editor):
    Status = apps.get_model('tasks', 'Status')
    WorkflowTransition = apps.get_model('tasks', 'WorkflowTransition')

    # Ensure Not-WIP category on the held status.
    Status.objects.filter(slug='accepted_capacity_held').update(category='backlog')

    for transition in WorkflowTransition.objects.all():
        if not _is_accept_capacity_held(transition):
            continue
        changed = []

        validators = list(transition.validators or [])
        if not any(
            isinstance(v, dict) and v.get('type') == VALIDATOR['type']
            for v in validators
        ):
            validators.append(dict(VALIDATOR))
            transition.validators = validators
            changed.append('validators')

        # Replace empty plan_hash PF constants with a bare type marker.
        pfs = list(transition.post_functions or [])
        new_pfs = []
        pf_changed = False
        saw_accept = False
        for pf in pfs:
            if isinstance(pf, dict) and pf.get('type') == 'accept_capacity_held':
                saw_accept = True
                cleaned = {'type': 'accept_capacity_held'}
                if pf != cleaned:
                    pf_changed = True
                new_pfs.append(cleaned)
            else:
                new_pfs.append(pf)
        if not saw_accept:
            new_pfs.append({'type': 'accept_capacity_held'})
            pf_changed = True
        if pf_changed:
            transition.post_functions = new_pfs
            changed.append('post_functions')

        # Required screen fields for plan identity.
        existing_fields = {
            (sf.get('field') if isinstance(sf, dict) else None)
            for sf in list(transition.screen_fields or [])
        }
        if not {'plan_hash', 'capacity_reason', 'plan_document'}.issubset(existing_fields):
            transition.screen_fields = list(SCREEN_FIELDS)
            changed.append('screen_fields')

        roles = list(transition.allowed_roles or [])
        roles_norm = {str(r).strip().lower() for r in roles}
        if 'architecture' not in roles_norm:
            transition.allowed_roles = list(roles) + ['architecture']
            changed.append('allowed_roles')

        if changed:
            transition.save(update_fields=list(dict.fromkeys(changed)))

    # Remove any residual unfenced activation edges from earlier S1 drafts.
    WorkflowTransition.objects.filter(
        name__in=('Activate Implementation', 'Return to Held'),
        from_status__in=('accepted_capacity_held', 'implementation_active'),
        to_status__in=('implementation_active', 'accepted_capacity_held'),
    ).delete()


def reverse_harden_accept_capacity_held(apps, schema_editor):
    """Best-effort reverse: strip Accept hardening this migration applied."""
    WorkflowTransition = apps.get_model('tasks', 'WorkflowTransition')
    for transition in WorkflowTransition.objects.all():
        name = (transition.name or '').strip().lower().replace('—', '-').replace('–', '-')
        to_status = (transition.to_status or '').strip().lower().replace('-', '_')
        if to_status != 'accepted_capacity_held':
            continue
        if not ('accept' in name and 'capacity' in name):
            continue
        validators = [
            v for v in list(transition.validators or [])
            if not (isinstance(v, dict) and v.get('type') == VALIDATOR['type'])
        ]
        pfs = [
            pf for pf in list(transition.post_functions or [])
            if not (isinstance(pf, dict) and pf.get('type') == 'accept_capacity_held')
        ]
        transition.validators = validators
        transition.post_functions = pfs
        # Leave screen_fields/roles intact if other authorities share them.
        transition.save(update_fields=['validators', 'post_functions'])


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0144_pls634_seed_accepted_capacity_held_statuses'),
    ]

    operations = [
        migrations.RunPython(
            harden_accept_capacity_held,
            reverse_harden_accept_capacity_held,
        ),
    ]
