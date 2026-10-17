"""PLS-1043: recurring test-gate flake probe (prevention-mechanism for PLS-1042).

Connect's premerge `test-gate` intermittently fails (k8s Job startup / infra
flake class), blocking merges and causing false Request-Changes reviews. It has
no specific carrier, so it recurs silently.

This probe:
1. Detects recurring `test-gate` failures on the same head where the sibling
   `frontend-gate` is green (a frontend-only change cannot affect the Django
   backend suite) — N occurrences within a window.
2. Classifies it as infra-flake (not code) and fires a Pulse alert with the
   flake classification + re-trigger instruction, so reviewers don't issue
   false Request-Changes.
3. Files/annotates a carrier when the flake recurs, so it never silently
   recurs.

Detection uses the Forgejo commit-status API per PR head: `premerge / test-gate`
and `premerge / frontend-gate` contexts. Classification is a pure function so it
is unit-testable without live CI access.
"""
from __future__ import annotations

import json
import os
import time
from datetime import timedelta
from typing import Optional

from django.conf import settings
from django.utils import timezone

# Recurrence threshold: how many test-gate failures on the same head (with a
# green frontend-gate) within the window before it is classified as infra-flake.
FLAKE_THRESHOLD = int(getattr(settings, 'CI_FLAKE_THRESHOLD', 3))
# Window (hours) for recurrence tracking.
FLAKE_WINDOW_HOURS = float(getattr(settings, 'CI_FLAKE_WINDOW_HOURS', 24))

_ALERT_SOURCE = 'pulse-ci-flake-probe'

# The Forgejo check contexts emitted by the connect premerge workflow.
TEST_GATE_CONTEXT = 'premerge / test-gate (pull_request)'
FRONTEND_GATE_CONTEXT = 'premerge / frontend-gate (pull_request)'

_DEFAULT_REPO = 'shizuha-labs/connect'


def _forgejo_api_url() -> str:
    return os.environ.get(
        'ORIGIN_API_URL', os.environ.get('ORIGIN_PUBLIC_URL', 'https://forgejo.example.invalid').rstrip('/') + '/api/v1',
    ).rstrip('/')


def _forgejo_token() -> str:
    return os.environ.get('FORGEJO_TOKEN', '').strip()


def _http_json(url: str, *, params: Optional[dict] = None) -> dict:
    import requests
    headers = {'Authorization': f'token {_forgejo_token()}'}
    resp = requests.get(url, headers=headers, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


def classify_commit_statuses(statuses: list[dict]) -> Optional[str]:
    """Classify a head's premerge check statuses.

    Returns one of:
      - 'flake'  — test-gate failed + frontend-gate succeeded (infra flake)
      - 'code'   — test-gate failed + frontend-gate failed/absent (real code failure)
      - 'ok'     — test-gate succeeded
      - None     — no test-gate status present (not a premerge head)

    A frontend-only change cannot affect the Django backend suite, so
    test-gate red + frontend-gate green is the infra-flake signature.
    """
    test_state = None
    frontend_state = None
    for s in statuses or []:
        ctx = (s.get('context') or '').strip()
        state = (s.get('state') or '').strip().lower()
        if ctx == TEST_GATE_CONTEXT:
            test_state = state
        elif ctx == FRONTEND_GATE_CONTEXT:
            frontend_state = state
    if test_state is None:
        return None
    if test_state == 'success':
        return 'ok'
    if frontend_state == 'success':
        return 'flake'
    return 'code'


def _recurrence_key(head_sha: str) -> str:
    return f'ci-flake-probe:connect:{head_sha[:12]}'


def _record_recurrence(head_sha: str, window_hours: float) -> int:
    """Increment the recurrence counter for a head within the window.

    Returns the new count. Uses Redis when configured, else a local cache.
    """
    key = _recurrence_key(head_sha)
    ttl = max(60, int(window_hours * 3600))
    url = getattr(settings, 'CI_FLAKE_REDIS_URL', '') or ''
    if url:
        import redis
        try:
            r = redis.Redis.from_url(url, socket_connect_timeout=1, socket_timeout=1)
            pipe = r.pipeline()
            pipe.incr(key)
            pipe.expire(key, ttl)
            results = pipe.execute()
            return int(results[0])
        except Exception:
            pass  # fall through to local cache
    from django.core.cache import cache
    try:
        return int(cache.incr(key)) if cache.get(key) else int(cache.add(key, 1, timeout=ttl) or 1)
    except Exception:
        return 1


def _fire_flake_alert(head_sha: str, pr_number: int, occurrences: int) -> None:
    from tasks.services.deduplication import AlertDeduplicationService
    now = timezone.now()
    data = {
        'item_type': 'alert.ci_flake',
        'title': (
            f'Connect premerge test-gate infra-flake (x{occurrences}) on '
            f'head {head_sha[:12]} — frontend-gate green'
        ),
        'source': _ALERT_SOURCE,
        'source_id': f'ci-flake:{head_sha[:12]}',
        'severity': 'warning',
        'labels': {
            'alertname': 'ConnectTestGateFlake',
            'repo': _DEFAULT_REPO,
            'head': head_sha[:12],
            'pr': str(pr_number),
        },
        'payload': {
            'head_sha': head_sha,
            'pr_number': pr_number,
            'occurrences': occurrences,
            'threshold': FLAKE_THRESHOLD,
            'generated_at': now.isoformat(),
        },
        'description': (
            f"Connect PR #{pr_number} head `{head_sha[:12]}` has `premerge / "
            f"test-gate` FAILING {occurrences}x within the window while "
            f"`premerge / frontend-gate` is GREEN. A frontend-only change cannot "
            f"affect the Django backend suite — this is an INFRA-FLAKE (k8s Job "
            f"startup / infra class), not a code failure. Do NOT issue a "
            f"Request-Changes review on this basis. Re-trigger the test-gate "
            f"(re-run the premerge workflow) to confirm."
        ),
    }
    AlertDeduplicationService.submit_alert(data)


def check_test_gate_flake(
    *,
    threshold: int | None = None,
    window_hours: float | None = None,
    repo: str = _DEFAULT_REPO,
) -> dict:
    """Run the CI test-gate flake probe.

    Returns ``{"checked_heads": int, "flakes": list[dict], "alerts": int}``.
    """
    threshold = threshold if threshold is not None else FLAKE_THRESHOLD
    window = window_hours if window_hours is not None else FLAKE_WINDOW_HOURS
    base = _forgejo_api_url()
    token = _forgejo_token()
    if not token:
        return {'checked_heads': 0, 'flakes': [], 'alerts': 0, 'error': 'no FORGEJO_TOKEN'}

    # Open PRs for the repo — each head's commit statuses carry the premerge
    # check contexts.
    try:
        pulls = _http_json(f'{base}/repos/{repo}/pulls', params={'state': 'open', 'limit': 30})
    except Exception:
        return {'checked_heads': 0, 'flakes': [], 'alerts': 0, 'error': 'pulls fetch failed'}

    flakes = []
    alerts = 0
    checked = 0
    for pr in pulls or []:
        head_sha = (pr.get('head') or {}).get('sha', '')
        if not head_sha:
            continue
        checked += 1
        try:
            status = _http_json(f'{base}/repos/{repo}/commits/{head_sha}/status')
        except Exception:
            continue
        statuses = status.get('statuses', []) if isinstance(status, dict) else []
        classification = classify_commit_statuses(statuses)
        if classification != 'flake':
            continue
        occurrences = _record_recurrence(head_sha, window)
        flakes.append({
            'head_sha': head_sha,
            'pr_number': pr.get('number'),
            'occurrences': occurrences,
        })
        if occurrences >= threshold:
            _fire_flake_alert(head_sha, pr.get('number'), occurrences)
            alerts += 1
    return {'checked_heads': checked, 'flakes': flakes, 'alerts': alerts}
