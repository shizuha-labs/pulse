"""Pure, versioned builder for organization-isolation condition capsules.

This module is imported by historical data migrations. Keep v1 immutable:
later contract changes must use a new versioned helper so old migrations remain
replayable.
"""

import html
import re


CAPSULE_HEADING = '## Org-safe condition capsule (migration)'
CAPSULE_VERSION = 1
UNKNOWN_TAG = 'migration-condition-unknown'

TERMINAL_STATUSES = {
    'completed', 'done', 'closed', 'cancelled', 'canceled', 'resolved',
    'rejected', 'duplicate', 'wont_fix', 'merged', 'failed', 'expired',
    'deferred', 'silenced', 'applied', 'change_rejected',
    'rfc_approved', 'rfc_implementation_linked', 'rfc_validation',
}

_SENSITIVE_LINE = re.compile(
    r'(?i)\b('
    r'password|passphrase|passwd|credential|secret|token|api[\s_-]*key|'
    r'private[\s_-]*key|client[\s_-]*secret|otp|one[\s_-]*time[\s_-]*password|'
    r'pin|cookie|authorization|bearer|recovery[\s_-]*code|seed[\s_-]*phrase'
    r')\b'
)
_URL = re.compile(r'(?i)\b(?:https?|ftp)://\S+')
_EMAIL = re.compile(r'(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b')
_HIGH_ENTROPY_VALUE = re.compile(
    r'(?<![A-Za-z0-9])(?=[A-Za-z0-9_+/=-]{20,}\b)'
    r'(?=[A-Za-z0-9_+/=-]*[A-Za-z])'
    r'(?=[A-Za-z0-9_+/=-]*\d)'
    r'[A-Za-z0-9_+/=-]+'
)
_HTML_TAG = re.compile(r'<[^>]+>')
_FENCED_BLOCK = re.compile(r'(?ms)```.*?```')
_INLINE_CODE = re.compile(r'`[^`\n]+`')
_MARKDOWN_PREFIX = re.compile(r'^\s*(?:#{1,6}\s*|[-*+]\s+|\d+[.)]\s+)')
_AC_HEADING = re.compile(
    r'(?i)^\s*#{1,6}\s*(?:acceptance|acceptance criteria|verification|'
    r'verification criteria|definition of done|done when)\b'
)
_HEADING = re.compile(r'^\s*#{1,6}\s+')
_EVIDENCE_WORD = re.compile(
    r'(?i)\b('
    r'evidence|document|record|statement|invoice|challan|return|form|report|'
    r'ledger|tally|bank|folder|drive|artifact|backup|filing|certificate'
    r')\b'
)


def _clean_line(line):
    """Return one safe, compact source line or an empty string."""
    line = html.unescape(str(line or ''))
    if _SENSITIVE_LINE.search(line):
        return ''
    line = _URL.sub('[external reference redacted]', line)
    line = _EMAIL.sub('[source identity redacted]', line)
    line = _INLINE_CODE.sub('[inline value redacted]', line)
    line = _HIGH_ENTROPY_VALUE.sub('[opaque value redacted]', line)
    line = _HTML_TAG.sub(' ', line)
    line = _MARKDOWN_PREFIX.sub('', line)
    line = re.sub(r'\s+', ' ', line).strip(' \t:-')
    return line[:500]


def _safe_source_lines(description):
    description = _FENCED_BLOCK.sub('', str(description or ''))
    lines = []
    for raw in description.splitlines():
        cleaned = _clean_line(raw)
        if cleaned:
            lines.append(cleaned)
    return lines


def _verification_lines(description, fallback_summary):
    in_acceptance = False
    criteria = []
    for raw in str(description or '').splitlines():
        if _AC_HEADING.match(raw):
            in_acceptance = True
            continue
        if in_acceptance and _HEADING.match(raw):
            break
        if not in_acceptance:
            continue
        cleaned = _clean_line(raw)
        if cleaned:
            criteria.append(cleaned)
        if len(criteria) >= 8:
            break
    if criteria:
        return criteria
    if fallback_summary:
        return [
            'Confirm from this organization’s own evidence that '
            f'{fallback_summary.rstrip(".")}.'
        ]
    return [
        'Operator or Admin Ops must restate the condition before dependents '
        'may close.'
    ]


def build_capsule(
    *,
    source_key,
    source_title,
    source_description,
    source_status,
    local_owning_team,
    migration_stamp,
):
    """Build structured capsule data without copying secrets or live links."""
    safe_lines = _safe_source_lines(source_description)
    unknown = not safe_lines
    safe_title = _clean_line(source_title)

    if unknown:
        summary = 'UNKNOWN — source had no org-safe extractable condition.'
    else:
        summary_parts = []
        if safe_title:
            summary_parts.append(safe_title)
        summary_parts.extend(safe_lines[:3])
        summary = ' — '.join(summary_parts)[:900]

    criteria = _verification_lines(
        source_description if not unknown else '',
        '' if unknown else summary,
    )
    evidence_hints = []
    for line in safe_lines:
        if _EVIDENCE_WORD.search(line) and line not in evidence_hints:
            evidence_hints.append(line)
        if len(evidence_hints) >= 5:
            break

    normalized_status = str(source_status or 'unknown').strip().lower()
    return {
        'version': CAPSULE_VERSION,
        'legacy_source_key': str(source_key or 'unknown'),
        'condition_summary': summary,
        'verification_criteria': criteria,
        'evidence_hints': evidence_hints,
        'local_owning_team': str(local_owning_team or 'admin-ops'),
        'source_status_at_migration': normalized_status,
        'source_terminal_at_migration': normalized_status in TERMINAL_STATUSES,
        'migration_stamp': str(migration_stamp),
        'extraction_state': 'unknown' if unknown else 'redacted',
    }


def render_capsule(capsule):
    evidence_hints = capsule.get('evidence_hints') or [
        'No non-sensitive evidence hint was extractable; use local evidence only.'
    ]
    criteria = capsule.get('verification_criteria') or [
        'Operator or Admin Ops must restate the condition before dependents may close.'
    ]
    terminal = 'terminal' if capsule.get('source_terminal_at_migration') else 'non-terminal'
    lines = [
        CAPSULE_HEADING,
        '',
        (
            f'- **Legacy source key:** {capsule["legacy_source_key"]} '
            '(foreign organization; do not re-link)'
        ),
        f'- **Condition summary:** {capsule["condition_summary"]}',
        '- **Verification criteria (local evidence only):**',
    ]
    lines.extend(f'  - [ ] {criterion}' for criterion in criteria)
    lines.append('- **Evidence hints (non-sensitive):**')
    lines.extend(f'  - {hint}' for hint in evidence_hints)
    lines.extend([
        f'- **Owning team (local):** {capsule["local_owning_team"]}',
        (
            '- **Source status at migration:** '
            f'{capsule["source_status_at_migration"]} ({terminal})'
        ),
        f'- **Migration stamp:** {capsule["migration_stamp"]}',
        '',
        (
            'This capsule is an organization-local snapshot. Verify it from this '
            'organization’s own evidence; do not access or recreate a live link '
            'to the foreign task.'
        ),
    ])
    return '\n'.join(lines)


def upsert_capsule_description(description, capsule):
    """Append v1 once while preserving all existing human-authored text."""
    current = str(description or '').rstrip()
    if CAPSULE_HEADING in current:
        return current
    rendered = render_capsule(capsule)
    return f'{current}\n\n{rendered}'.strip() if current else rendered


def merge_unknown_tag(tags, unknown):
    """Add the visible unknown marker without disturbing existing CSV tags."""
    existing = [
        value.strip()
        for value in str(tags or '').split(',')
        if value.strip()
    ]
    if unknown and UNKNOWN_TAG not in existing:
        existing.append(UNKNOWN_TAG)
    return ','.join(existing) if existing else None
