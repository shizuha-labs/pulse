"""
Op-typed change_spec contract for propose_workflow_change (PLAT-218).

Implements the architecture contract "Op-Typed change_spec for
propose_workflow_change" (ENG wiki d328824d-055c-4a2f-a7ec-488fba708254,
PLAT-252). Tier derives from declared OP TYPES, not prose token-sniffing:
naming a structural object in an op's address fields is targeting, not
mutation (fixes the CR #1072 over-rejection class), while the op table is
deny-by-default (unknown op -> risky; legacy free text -> risky) so the
PLAT-189 evasion class is dead structurally — `rationale` is display-only
and provably never inspected by classification.

Pure functions with injected I/O (workflow fetcher / api caller) so the
whole contract is unit-testable without the MCP server or Django.

v1.1 (PR #27 review): §3.6 every op.workflow must equal the CR's
target_workflow (approval-display and execution must be the same object);
§3.7 anti-TOCTOU spec seal — sha256 over the canonical ops, recorded in an
append-only comment at proposal and re-verified (plus re-classified) at
apply. Item metadata/description are editable post-approval and can never
be the trust anchor. What's approved must be what executes, byte-for-byte.
"""
import json
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from tasks.typed_change_integrity import (
    CONTRACT_REF,
    SEAL_MARKER,
    canonical_ops_json,
    format_seal,
    latest_approval_time,
    ops_digest,
    parse_seal,
    select_trusted_seal,
)

# ── §2.1 ROUTINE ops: additive/textual on EXISTING objects ──────────────────
ROUTINE_OPS = {
    'set_knowledge_article': {'workflow', 'transition', 'content'},
    'append_knowledge_article': {'workflow', 'transition', 'content'},
    'edit_transition_description': {'workflow', 'transition', 'text'},
    'edit_status_description': {'workflow', 'status', 'text'},
    'edit_dod': {'workflow', 'target', 'text'},
    'set_reminder': {'workflow', 'transition', 'text'},
    'edit_label': {'workflow', 'target', 'label'},
}

# ── §2.2 RISKY ops: structural / permission / executable / destructive ──────
RISKY_OPS = {
    'add_status': {'workflow', 'name', 'slug', 'category'},
    'add_transition': {'workflow', 'name', 'from_status', 'to_status'},
    'modify_transition': {'workflow', 'transition', 'changes'},
    'remove_status': {'workflow', 'status'},
    'remove_transition': {'workflow', 'transition'},
    'set_validator': {'workflow', 'transition', 'validator', 'value'},
    'set_post_function': {'workflow', 'transition', 'post_function', 'value'},
    'set_condition': {'workflow', 'transition', 'condition'},
    'set_allowed_roles': {'workflow', 'transition', 'allowed_roles'},
    'set_assignee_only': {'workflow', 'transition', 'assignee_only'},
    'change_permission': {'workflow', 'transition', 'permission'},
    'migrate': {'description', 'scope'},
    'backfill': {'description', 'scope'},
}

# §3.4 sanity caps — over caps the CR classifies RISKY (not rejected).
MAX_OPS = 20
MAX_CONTENT_BYTES = 8 * 1024

# Ops the v1 applier refuses to auto-execute (destructive / data ops are
# manual-only per §2.2 "never auto-applied").
MANUAL_ONLY_OPS = {'remove_status', 'remove_transition', 'migrate', 'backfill', 'change_permission'}

def version_error(envelope):
    """§1 version strictness (PLAT-253 #3): the contract is version:1 — any
    other value must REJECT with an actionable error rather than silently
    parse as typed (or silently fall through to the legacy prose path).
    Called at BOTH gates: proposal (on the parsed envelope) and apply (on
    the metadata.typed_change_spec dict, which is never re-parsed)."""
    if not isinstance(envelope, dict):
        return None
    v = envelope.get('version')
    # Guard before the equality check: Python bools are ints, so `True == 1`
    # would accept `{"version": true}` without the isinstance guard.
    if type(v) is int and v == 1:
        return None
    return (f"typed change_spec declares version={v!r} but this applier "
            f"implements contract version 1 only — set \"version\": 1 "
            f"({CONTRACT_REF} §1).")


def parse_envelope(change_spec):
    """§1: parse the typed envelope. Returns the envelope dict when
    `change_spec` is JSON with a `version` and a non-empty `operations`
    list of dicts; otherwise None (-> legacy free-text path, risky).
    Version strictness is enforced separately via version_error() so a
    typed-looking envelope with a wrong version gets an actionable REJECT
    instead of silently degrading to the prose path."""
    if not isinstance(change_spec, str):
        return None
    try:
        env = json.loads(change_spec)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(env, dict):
        return None
    ops = env.get('operations')
    if 'version' not in env or not isinstance(ops, list) or not ops:
        return None
    if not all(isinstance(o, dict) and isinstance(o.get('op'), str) for o in ops):
        return None
    return env


def _missing_fields(op, required):
    return sorted(
        f for f in required
        if op.get(f) in (None, '') and op.get(f) is not False
    )


def _content_over_cap(op):
    for f in ('content', 'text'):
        v = op.get(f)
        if isinstance(v, str) and len(v.encode('utf-8', 'replace')) > MAX_CONTENT_BYTES:
            return True
    return False


def validate_and_classify(envelope, fetch_workflow=None, target_workflow=None):
    """§1 tier rule + §3 validation, fail-closed.

    Returns {'tier': 'routine'|'risky', 'reject': str|None}.
      - reject is an ACTIONABLE error message -> the proposal must NOT be
        created (missing required field §3.2, routine-op target not found
        §3.3, op/header workflow mismatch §3.6, non-boolean assignee_only).
      - tier ignores `rationale` entirely (never read here — §1).

    fetch_workflow(slug) -> workflow detail dict (with 'statuses' and
    'transitions' lists) or None; injected so callers/tests control I/O.
    Existence checks (§3.3) run only for ROUTINE ops (an edit can never
    create) and are skipped when no fetcher is supplied.

    target_workflow (§3.6, v1.1): when given, every op carrying a `workflow`
    field must address exactly this workflow — a CR may not display one
    workflow in its header/tag while its stored ops mutate another.
    """
    ops = envelope['operations']
    if len(ops) > MAX_OPS:
        return {'tier': 'risky', 'reject': None}

    tier = 'routine'
    for i, op in enumerate(ops):
        name = op['op']
        # §3.6 (v1.1): op/header workflow coherence — checked for EVERY op
        # that addresses a workflow, before anything else.
        if target_workflow is not None and op.get('workflow') is not None \
                and _norm(op.get('workflow')) != _norm(target_workflow):
            return {'tier': 'risky', 'reject': (
                f"operation[{i}] '{name}' addresses workflow "
                f"'{op.get('workflow')}' but the CR's target_workflow is "
                f"'{target_workflow}' — every op must address the declared "
                f"target (§3.6, {CONTRACT_REF}).")}
        if name in ROUTINE_OPS:
            missing = _missing_fields(op, ROUTINE_OPS[name])
            if missing:
                return {'tier': 'risky', 'reject': (
                    f"operation[{i}] '{name}' is missing required field(s): "
                    f"{', '.join(missing)} — see the {CONTRACT_REF}.")}
            if _content_over_cap(op):
                tier = 'risky'  # §3.4: over caps -> RISKY, not reject
        elif name in RISKY_OPS:
            missing = _missing_fields(op, RISKY_OPS[name])
            if missing:
                return {'tier': 'risky', 'reject': (
                    f"operation[{i}] '{name}' is missing required field(s): "
                    f"{', '.join(missing)} — see the {CONTRACT_REF}.")}
            # v1.1: assignee_only is a PERMISSION value — a string "false"
            # would apply truthy and FLIP the gate. JSON booleans only.
            if name == 'set_assignee_only' and not isinstance(op.get('assignee_only'), bool):
                return {'tier': 'risky', 'reject': (
                    f"operation[{i}] 'set_assignee_only'.assignee_only must be a JSON "
                    f"boolean (true/false), got {type(op.get('assignee_only')).__name__} "
                    f"{op.get('assignee_only')!r} — see the {CONTRACT_REF}.")}
            # PLAT-253 #2: allowed_roles gates transition permissions and the
            # backend checks `membership.role in allowed_roles` — a STRING here
            # would substring-match roles and an object/scalar would wipe or
            # corrupt the gate. JSON list of non-empty role strings only.
            if name == 'set_allowed_roles':
                err = _allowed_roles_error(op.get('allowed_roles'))
                if err:
                    return {'tier': 'risky', 'reject': (
                        f"operation[{i}] 'set_allowed_roles'.allowed_roles {err} "
                        f"— see the {CONTRACT_REF}.")}
            # PLAT-253 #1: conditions is a JSON-Logic OBJECT on the backend —
            # a scalar/list/empty value would PATCH a malformed or no-op
            # condition onto a permission gate. Reject pre-apply.
            if name == 'set_condition':
                err = _condition_error(op.get('condition'))
                if err:
                    return {'tier': 'risky', 'reject': (
                        f"operation[{i}] 'set_condition'.condition {err} "
                        f"— see the {CONTRACT_REF}.")}
            tier = 'risky'
        else:
            # §3.1 unknown op -> CR classifies RISKY (forward-compat).
            tier = 'risky'

    # §3.3 routine targets must EXIST (addressing, never creating).
    if fetch_workflow is not None:
        wf_cache = {}
        for i, op in enumerate(ops):
            if op['op'] not in ROUTINE_OPS:
                continue
            slug = str(op.get('workflow') or '').strip()
            if slug not in wf_cache:
                wf_cache[slug] = fetch_workflow(slug)
            detail = wf_cache[slug]
            if not detail:
                return {'tier': 'risky', 'reject': (
                    f"operation[{i}] '{op['op']}': target not found: "
                    f"workflow '{slug}'.")}
            err = _routine_target_missing(op, detail)
            if err:
                return {'tier': 'risky', 'reject': f"operation[{i}] '{op['op']}': {err}"}

    return {'tier': tier, 'reject': None}


def _norm(s):
    return str(s or '').strip().lower()


def _allowed_roles_error(value):
    """PLAT-253 #2: allowed_roles must be a JSON list of non-empty strings.
    Returns an error fragment or None.  [] is allowed — it CLEARS the gate
    (migration 0022: "Empty = all roles") — caller must log that loudly."""
    if not isinstance(value, list):
        return (f"must be a JSON list of role strings, got "
                f"{type(value).__name__} {value!r}")
    # [] is valid: clearing the gate is a legitimate operator intent (migration 0022).
    # Don't reject it — the applier's result message must state "CLEARED" so the
    # operator sees the effect.
    if value:
        bad = [r for r in value if not isinstance(r, str) or not r.strip()]
        if bad:
            return f"must contain only non-empty role strings; got invalid entries {bad!r}"
    return None


def _condition_error(value):
    """PLAT-253 #1: the transition `conditions` field is a JSON-Logic OBJECT
    (backend JSONField, 'JSON Logic conditions'). Returns an error fragment
    for non-dict values, else None.  {} is allowed — it CLEARS the gate."""
    if not isinstance(value, dict):
        return (f"must be a JSON-Logic object (dict), got "
                f"{type(value).__name__} {value!r}")
    # {} is valid: WorkflowService only evaluates conditions under `if transition.conditions`,
    # so empty dict clears the gate. Don't reject — applier result must state "CLEARED".
    return None


def _workflow_status_refs(detail):
    """Return (refs, slugs) for statuses in a workflow.

    Routine/text ops such as edit_status_description historically address an
    existing status by either display name ("In Review") or slug
    ("in_review").  Structural transition PATCHes must stay slug-only, so
    keep the two sets separate instead of using the slug-only set for every
    caller.
    """
    slugs = {_norm(s.get('status_slug')) for s in detail.get('statuses', [])}
    refs = slugs | {_norm(s.get('status_name')) for s in detail.get('statuses', [])}
    refs |= {_norm(s.get('slug')) for s in detail.get('statuses', [])}
    refs |= {_norm(s.get('name')) for s in detail.get('statuses', [])}
    slugs.discard('')
    refs.discard('')
    return refs, slugs


def _workflow_names(detail):
    status_refs, _status_slugs = _workflow_status_refs(detail)
    transitions = {
        _norm(t.get('name')) for t in detail.get('transitions', [])
    } | {
        _norm(t.get('slug')) for t in detail.get('transitions', [])
    }
    transitions.discard('')
    return status_refs, transitions


def _routine_target_missing(op, detail):
    """Returns an error string when the op's addressed object doesn't exist."""
    statuses, transitions = _workflow_names(detail)
    if 'transition' in ROUTINE_OPS[op['op']]:
        if _norm(op.get('transition')) not in transitions:
            return f"target not found: transition '{op.get('transition')}' in workflow '{op.get('workflow')}'."
    if 'status' in ROUTINE_OPS[op['op']]:
        if _norm(op.get('status')) not in statuses:
            return f"target not found: status '{op.get('status')}' in workflow '{op.get('workflow')}'."
    if 'target' in ROUTINE_OPS[op['op']]:
        tgt = _norm(op.get('target'))
        if tgt not in transitions and tgt not in statuses:
            return (f"target not found: '{op.get('target')}' is neither a transition "
                    f"nor a status in workflow '{op.get('workflow')}'.")
    return None


# ── §3.5 applier: execute ops 1:1 via the §2 tool mapping ────────────────────

def get_all_pages(api, path):
    """Drain a DRF-paginated list endpoint (PAGE_SIZE=10 — a single page-1
    read false-rejects valid CRs whose targets sit on later pages; v1.1 P2).
    Returns the accumulated results list, or the error dict from page 1."""
    out = []
    page = 1
    while True:
        sep = '&' if '?' in path else '?'
        res = api('GET', f"{path}{sep}page={page}", None)
        if isinstance(res, list):
            return out + res  # non-paginated endpoint
        if not isinstance(res, dict) or 'error' in res:
            return res if not out else out
        out.extend(res.get('results', []))
        if not res.get('next'):
            return out
        page += 1


def _find_transition(detail, ref):
    ref_n = _norm(ref)
    for t in detail.get('transitions', []):
        if _norm(t.get('name')) == ref_n or _norm(t.get('slug')) == ref_n or str(t.get('id')) == str(ref):
            return t
    return None


def _set_titled_article(articles, title, content):
    """Replace the article with `title` (case-insensitive) or append it."""
    out = [a for a in (articles or []) if _norm(a.get('title')) != _norm(title)]
    out.append({'type': 'inline', 'title': title, 'content': content})
    return out


def apply_operation(op, api, fetch_workflow):
    """Execute ONE typed op via the §2 mapped endpoints. Zero prose
    interpretation — the typed spec IS the apply plan.

    api(method, path, data) -> response dict (an 'error' key signals failure).
    Returns (ok: bool, message: str)."""
    name = op['op']
    if name in MANUAL_ONLY_OPS:
        return False, (f"'{name}' is manual-only in the v1 applier (destructive/data "
                       f"op — never auto-applied per the contract §2.2). Execute it "
                       f"via the dedicated admin path with the approved CR id.")

    detail = fetch_workflow(str(op.get('workflow') or '').strip())
    if not detail:
        return False, f"workflow '{op.get('workflow')}' not found"
    wf_id = detail.get('id')

    def patch_transition(ref, data):
        t = _find_transition(detail, ref)
        if not t:
            return False, f"transition '{ref}' not found in workflow '{op.get('workflow')}'"
        res = api('PATCH', f"/transitions/{t.get('id')}/", data)
        if 'error' in res:
            return False, str(res.get('error'))
        return True, f"transition '{t.get('name')}' updated"

    if name == 'set_knowledge_article':
        # Replace semantics (§2.1): the supplied article becomes the list.
        return patch_transition(op['transition'], {'knowledge_articles': [
            {'type': 'inline', 'title': op.get('title') or 'Knowledge Article',
             'content': op['content']}]})
    if name == 'append_knowledge_article':
        t = _find_transition(detail, op['transition'])
        if not t:
            return False, f"transition '{op['transition']}' not found in workflow '{op.get('workflow')}'"
        articles = list(t.get('knowledge_articles') or [])
        title = op.get('title') or 'Knowledge Article'
        # PLAT-253 #5 rerun idempotency: the apply loop stops at the first
        # failed op and tells the operator to re-run; on re-run every earlier
        # SUCCEEDED op re-executes. set_* ops replace (idempotent) but a
        # naive append DUPLICATES the article on each re-run. Skip when an
        # identical (title, content) article is already present.
        for a in articles:
            if _norm(a.get('title')) == _norm(title) and (a.get('content') or '') == op['content']:
                return True, (f"identical article '{title}' already present on "
                              f"transition '{t.get('name')}' — skipped (rerun-safe)")
        articles.append({'type': 'inline', 'title': title, 'content': op['content']})
        return patch_transition(op['transition'], {'knowledge_articles': articles})
    if name == 'edit_transition_description':
        return patch_transition(op['transition'], {'description': op['text']})
    if name == 'edit_status_description':
        ref = _norm(op['status'])
        res = get_all_pages(api, '/statuses/')
        statuses = res if isinstance(res, list) else []
        for s in statuses:
            if _norm(s.get('slug')) == ref or _norm(s.get('name')) == ref:
                r2 = api('PATCH', f"/statuses/{s.get('id')}/", {'description': op['text']})
                if 'error' in r2:
                    return False, str(r2.get('error'))
                return True, f"status '{s.get('name')}' description updated"
        return False, f"status '{op['status']}' not found"
    if name in ('edit_dod', 'set_reminder'):
        # The codebase's DoD/reminder mechanism IS a titled knowledge article
        # on the target transition (cf. CR #1072's reminder gates).
        title = 'Definition of Done' if name == 'edit_dod' else 'Reminder'
        ref = op.get('target') or op.get('transition')
        t = _find_transition(detail, ref)
        if not t:
            return False, f"transition '{ref}' not found in workflow '{op.get('workflow')}' (DoD/reminder applies to transitions)"
        articles = _set_titled_article(t.get('knowledge_articles'), title, op['text'])
        return patch_transition(ref, {'knowledge_articles': articles})
    if name == 'edit_label':
        return False, ("'edit_label' has no non-structural apply target in v1 "
                       "(transition/status display names are structural via "
                       "modify_transition). Apply manually or use "
                       "edit_transition_description.")
    if name == 'add_status':
        res = api('POST', '/statuses/', {
            'name': op['name'], 'slug': op['slug'], 'category': op['category'],
            'description': op.get('description', ''),
        })
        if 'error' in res:
            return False, str(res.get('error'))
        r2 = api('POST', f"/workflows/{wf_id}/add_status/", {
            'status_slug': op['slug'],
            'position': op.get('position', len(detail.get('statuses', []))),
        })
        if 'error' in r2:
            return False, str(r2.get('error'))
        return True, f"status '{op['slug']}' created and added to workflow"
    if name == 'add_transition':
        data = {'workflow': wf_id, 'from_status': op['from_status'],
                'to_status': op['to_status'], 'name': op['name'],
                'description': op.get('description', '')}
        for opt in ('post_functions', 'knowledge_articles', 'allowed_roles', 'screen_fields'):
            val = op.get(opt)
            if val is None:
                continue
            # PLAT-519: op values must reach the backend as lists, not JSON strings.
            if isinstance(val, str):
                try:
                    val = json.loads(val)
                except (json.JSONDecodeError, ValueError):
                    return False, (f"add_transition op: '{opt}' must be a JSON array, "
                                   f"got invalid JSON string: {val!r}")
            data[opt] = val
        res = api('POST', f"/workflows/{wf_id}/add_transition/", data)
        if 'error' in res:
            return False, str(res.get('error'))
        return True, f"transition '{op['name']}' created ({op['from_status']} -> {op['to_status']})"
    if name == 'modify_transition':
        changes = op.get('changes') or {}
        # v1.1: `slug` dropped — the transition PATCH serializer doesn't
        # expose it; accept-and-silently-fail is worse than reject.
        allowed = {'from_status', 'to_status', 'name'}
        bad = set(changes) - allowed
        if bad:
            return False, f"modify_transition.changes only accepts {sorted(allowed)}; got {sorted(bad)}"
        # PLAT-253 #4: a changed from_status/to_status must address a status
        # that EXISTS in this workflow — the PATCH would otherwise point the
        # transition at a dangling slug and strand items (from_status='*' is
        # the documented global wildcard and stays valid).
        _status_refs, statuses = _workflow_status_refs(detail)
        for fld in ('from_status', 'to_status'):
            v = changes.get(fld)
            if v is None or (fld == 'from_status' and str(v).strip() == '*'):
                continue
            if _norm(v) not in statuses:
                return False, (f"modify_transition.changes.{fld}='{v}' is not a status in "
                               f"workflow '{op.get('workflow')}' — use the status slug, not the display name. "
                               f"Existing slugs: {sorted(statuses)}")
        return patch_transition(op['transition'], dict(changes))
    if name == 'set_post_function':
        t = _find_transition(detail, op['transition'])
        if not t:
            return False, f"transition '{op['transition']}' not found"
        existing = t.get('post_functions') or []
        # PLAT-519: if a prior write stored post_functions as a JSON string, parse it
        # back to a list before appending — list(string) would yield characters.
        if isinstance(existing, str):
            try:
                existing = json.loads(existing)
            except (json.JSONDecodeError, ValueError):
                existing = []
        pfs = list(existing)
        pfs.append({'type': op['post_function'], 'value': op['value']})
        return patch_transition(op['transition'], {'post_functions': pfs})
    if name == 'set_allowed_roles':
        # PLAT-253 #2: validation rejects upstream; the applier defends
        # independently (same pattern as set_assignee_only below) — the
        # backend checks `membership.role in allowed_roles`, so a string
        # would substring-match and a scalar/object would corrupt the gate.
        err = _allowed_roles_error(op.get('allowed_roles'))
        if err:
            return False, f"allowed_roles {err}"
        roles = op['allowed_roles']
        note = " [CLEARED — transition now open to all roles (migration 0022 empty=all)]" if not roles else ""
        ok, msg = patch_transition(op['transition'], {'allowed_roles': roles})
        return ok, msg + note if ok else msg
    if name == 'set_assignee_only':
        # v1.1: permission value — JSON boolean ONLY (a string "false" coerced
        # with bool() would silently FLIP the gate). Validation rejects this
        # upstream; the applier defends independently.
        if not isinstance(op.get('assignee_only'), bool):
            return False, (f"assignee_only must be a JSON boolean, got "
                           f"{type(op.get('assignee_only')).__name__} {op.get('assignee_only')!r}")
        return patch_transition(op['transition'], {'assignee_only': op['assignee_only']})
    if name == 'set_validator':
        # PLAT-3406: upsert the NAMED validator only — a bare list replace
        # silently dropped every other validator on the transition (the
        # #14196 apply removed min_seconds_in_review from Approve & Schedule).
        t = _find_transition(detail, op['transition'])
        if not t:
            return False, f"transition '{op['transition']}' not found"
        existing = t.get('validators') or []
        # PLAT-519: a prior write may have stored the list as a JSON string.
        if isinstance(existing, str):
            try:
                existing = json.loads(existing)
            except (json.JSONDecodeError, ValueError):
                existing = []
        validators = [v for v in existing
                      if not (isinstance(v, dict) and _norm(v.get('type')) == _norm(op['validator']))]
        replaced = len(validators) != len(existing)
        validators.append({'type': op['validator'], 'value': op['value']})
        ok, msg = patch_transition(op['transition'], {'validators': validators})
        if ok:
            action = 'updated existing' if replaced else 'added'
            msg += f" [{action} validator '{op['validator']}'; {len(validators) - 1} other validator(s) preserved]"
        return ok, msg
    if name == 'set_condition':
        # PLAT-253 #1: applier-side defense — conditions is a JSON-Logic
        # object; a scalar/list/empty value would PATCH a malformed or no-op
        # rule onto a permission gate.
        err = _condition_error(op.get('condition'))
        if err:
            return False, f"condition {err}"
        cond = op['condition']
        note = " [CLEARED — no JSON-Logic gate on this transition]" if not cond else ""
        ok, msg = patch_transition(op['transition'], {'conditions': cond})
        return ok, msg + note if ok else msg
    return False, f"unknown op '{name}' — not in the contract op tables"
