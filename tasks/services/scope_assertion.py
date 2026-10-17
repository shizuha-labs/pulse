"""PLAT-9491 — Hive scope-manifest assertion stamps on dispatch records.

PLAT-9430 child 2 (defense in depth; design: sara comment 826977). When Pulse's
dispatch path confers a capability tag to a seat (``WorkflowService
._seat_satisfies_required_capability``, the consumer of the
``TeamCapabilityProfile`` SoT), Pulse can ask Hive's internal API for a
**scope-manifest assertion** for that (seat, tags) pair and stamp the result on
the dispatch record (``WipSlotReservation.scope_assertions``).

Contract (Hive-side carrier = HIVE-2695 / PLAT-9430 child 1; until that lands,
the producer 404s and every stamp is ``skipped/no_endpoint`` — inert by
construction):

    POST <PULSE_HIVE_SCOPE_ASSERTION_URL>
    {"seat_email": "<email>", "tags": ["<tag>", ...]}
    -> 200 {"result": "ok" | "divergence",
            "tags": {"<tag>": {"advertised_verbs": [...],
                               "actual_scopes": [...],
                               "gap_class": "<slug>"}}}

HARD CONTRACT — this is evidence, never a gate (the gate lives in Hive's
mint path, HIVE-2695):

* the call NEVER blocks dispatch: bounded timeout, and every failure mode
  (disabled, unconfigured, timeout, transport error, non-2xx, malformed body)
  resolves to a ``skipped``/``unknown`` stamp — never an exception;
* a divergence stamp changes nothing about the dispatch decision — it only
  makes mint-time/dispatch-time drift VISIBLE on the record;
* the assertion is skipped entirely when ``PULSE_SCOPE_ASSERTION_ENABLED``
  is not truthy, so existing deployments are untouched until ops opts in.
"""
import logging
from datetime import datetime, timezone as dt_timezone

logger = logging.getLogger(__name__)

ASSERTION_SCHEMA = 'pulse.scope-assertion.v1'

STAMP_OK = 'ok'
STAMP_DIVERGENCE = 'divergence'
STAMP_SKIPPED = 'skipped'
STAMP_UNKNOWN = 'unknown'


def _env(environ, key, default=''):
    try:
        return str((environ if environ is not None else {}).get(key, default) or default)
    except Exception:
        return default


def _truthy(raw):
    return str(raw or '').strip().lower() in ('1', 'true', 'yes', 'on')


def build_scope_assertion_stamp(
    email, tags, *, environ=None, url=None, token=None, requests_mod=None,
    now=None, timeout=1.5,
):
    """Ask Hive for a (seat, tags) scope-manifest assertion. NEVER raises.

    Returns a JSON-serializable stamp dict (see module docstring for the
    contract). Every failure mode resolves to a skipped/unknown stamp with a
    reason slug; the caller persists it verbatim on the dispatch record.
    """
    env = environ if environ is not None else {}
    base = {
        'schema': ASSERTION_SCHEMA,
        'seat_email': str(email or '').strip().lower(),
        'tags': sorted(str(t) for t in (tags or []) if str(t).strip()),
        'asserted_at': (now or datetime.now(dt_timezone.utc)).isoformat(),
    }
    if not _truthy(_env(env, 'PULSE_SCOPE_ASSERTION_ENABLED')):
        return {**base, 'status': STAMP_SKIPPED, 'reason': 'disabled'}
    if not base['tags']:
        return {**base, 'status': STAMP_SKIPPED, 'reason': 'no_tags'}
    endpoint = url or _env(env, 'PULSE_HIVE_SCOPE_ASSERTION_URL') \
        or _env(env, 'PULSE_HIVE_EVIDENCE_URL')
    if not endpoint:
        return {**base, 'status': STAMP_SKIPPED, 'reason': 'unconfigured'}
    auth = token or _env(env, 'PULSE_HIVE_SCOPE_ASSERTION_TOKEN') \
        or _env(env, 'PULSE_HIVE_EVIDENCE_TOKEN')
    if not auth:
        return {**base, 'status': STAMP_SKIPPED, 'reason': 'unconfigured_token'}
    try:
        import requests as _requests
    except Exception:
        return {**base, 'status': STAMP_SKIPPED, 'reason': 'requests_unavailable'}
    req = requests_mod or _requests
    try:
        resp = req.post(
            endpoint,
            json={'seat_email': base['seat_email'], 'tags': base['tags']},
            headers={'Authorization': f'Bearer {auth}'},
            timeout=timeout,
        )
    except req.exceptions.Timeout:
        return {**base, 'status': STAMP_SKIPPED, 'reason': 'timeout'}
    except Exception:
        logger.warning('PLAT-9491: scope assertion transport error (non-blocking)', exc_info=True)
        return {**base, 'status': STAMP_SKIPPED, 'reason': 'transport_error'}
    if resp.status_code == 404:
        # HIVE-2695 (child 1) has not landed its endpoint yet — inert by design.
        return {**base, 'status': STAMP_SKIPPED, 'reason': 'no_endpoint'}
    if resp.status_code != 200:
        return {
            **base, 'status': STAMP_UNKNOWN,
            'reason': f'producer_status_{resp.status_code}',
        }
    try:
        body = resp.json()
        result = str(body.get('result') or '').strip().lower()
        if result not in (STAMP_OK, STAMP_DIVERGENCE):
            return {**base, 'status': STAMP_UNKNOWN, 'reason': 'malformed_result'}
        return {
            **base,
            'status': result,
            'verdicts': body.get('tags') or {},
        }
    except Exception:
        logger.warning('PLAT-9491: malformed scope-assertion body (non-blocking)', exc_info=True)
        return {**base, 'status': STAMP_UNKNOWN, 'reason': 'malformed_body'}
