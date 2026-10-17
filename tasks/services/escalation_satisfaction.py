"""PLAT-10926: typed evidence that a superseded escalation ask is satisfied.

Role strength cannot release a PLAT-4215 edge. A trail pointer can. The
record is visible to dependency reconciliation so PLS-89 does not trap
the carrier. A rejected release drops the stamp; it is not an override.
"""

SCHEMA = 'shizuha.pulse.escalation-satisfaction.v1'


class EscalationSatisfactionError(ValueError):
    code = 'escalation_satisfaction_invalid'


def validate_escalation_satisfaction(raw):
    """Return a normalized record or raise EscalationSatisfactionError."""
    if not isinstance(raw, dict):
        raise EscalationSatisfactionError('escalation_satisfaction must be an object')
    schema = str(raw.get('schema') or '')
    trail = str(raw.get('trail_pointer') or '').strip()
    check = str(raw.get('acceptance_check') or '').strip()
    if schema != SCHEMA:
        raise EscalationSatisfactionError(
            f'escalation_satisfaction.schema must be {SCHEMA}'
        )
    if len(trail) < 8 or len(trail) > 500:
        raise EscalationSatisfactionError(
            'escalation_satisfaction.trail_pointer must name the satisfaction receipt'
        )
    if len(check) < 8 or len(check) > 500:
        raise EscalationSatisfactionError(
            'escalation_satisfaction.acceptance_check is required'
        )
    if raw.get('superseded') is not True:
        raise EscalationSatisfactionError(
            'escalation_satisfaction.superseded must be true'
        )
    return {
        'schema': SCHEMA,
        'trail_pointer': trail,
        'acceptance_check': check,
        'superseded': True,
    }


def satisfaction_overrides_manual_block(metadata):
    """True only for a previously validated stamped record."""
    if not isinstance(metadata, dict):
        return False
    try:
        validate_escalation_satisfaction(metadata.get('escalation_satisfaction'))
    except EscalationSatisfactionError:
        return False
    return True
