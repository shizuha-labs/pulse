"""Deployment-event ledger services (PLS-183/PLS-186)."""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, Tuple

import requests
from django.db import transaction
from django.utils import timezone

from tasks.models import Comment, DeploymentEvent, Item, Project
from tasks.origin_provenance import merge_origin_observations, normalize_origin_observation


logger = logging.getLogger(__name__)


_EVENT_FIELDS = {
    'project', 'repo', 'branch', 'commit_sha', 'service', 'component', 'environment',
    'cluster', 'namespace', 'deployment_target', 'image', 'image_tag', 'image_digest',
    'manifest_ref', 'pipeline_system', 'pipeline_run_id', 'pipeline_url', 'logs_url',
    'state', 'smoke_result', 'started_at', 'rollout_started_at', 'rolled_out_at',
    'smoke_started_at', 'smoke_finished_at', 'pr_numbers', 'source_task_keys', 'links',
    'metadata', 'rollback_of', 'superseded_by', 'failure_task', 'qa_task',
}
_REQUIRED_IDENTITY_FIELDS = ('repo', 'service', 'commit_sha')
_FAILURE_KIND_BY_STATE = {
    'build_failed': 'build',
    'manifest_failed': 'manifest',
    'rollout_stalled': 'rollout',
    'smoke_failed': 'smoke',
}
_TERMINAL_STATUSES = {
    'completed', 'cancelled', 'canceled', 'closed', 'done', 'resolved', 'rejected',
    'duplicate', 'wont_fix', 'merged', 'failed', 'expired', 'deferred',
}


def _clean(value: Any, default: str = '') -> str:
    if value is None:
        return default
    return str(value).strip()


def _provider_token_env():
    """Return (github_token, forgejo_token, origin_url) from the runtime env."""
    github_token = os.environ.get('GITHUB_TOKEN', '')
    forgejo_token = (
        os.environ.get('FORGEJO_TOKEN')
        or os.environ.get('ORIGIN_TOKEN')
        or os.environ.get('GITEA_TOKEN')
        or os.environ.get('TEA_TOKEN')
        or ''
    )
    origin_url = (
        os.environ.get('ORIGIN_URL')
        or os.environ.get('FORGEJO_URL')
        or 'https://origin.shizuha'
    ).rstrip('/')
    return github_token, forgejo_token, origin_url


def _split_deployment_repo(repo: str):
    """Return (provider, owner, repo_name) for a deployment repo string.

    Deployment repos are normally ``owner/repo`` on Origin (``shizuha-labs/x``).
    GitHub mirror repos are expressed as ``github.com/owner/repo`` or a bare
    ``owner/repo`` that resolves on api.github.com.  Anything else (no slash, an
    unknown host) is not a usable provider repo and returns ``(None, None, None)``
    so correlation stays fail-closed.
    """
    raw = _clean(repo).lower().rstrip('/')
    for prefix in ('https://', 'http://'):
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
            break
    provider = 'forgejo'
    if raw.startswith('github.com/'):
        raw = raw[len('github.com/'):]
        provider = 'github'
    elif raw.startswith('origin.shizuha'):
        _, sep, raw = raw.partition('/')
        if not sep:
            return None, None, None
    if raw.endswith('.git'):
        raw = raw[:-4]
    parts = [part for part in raw.split('/') if part]
    if len(parts) != 2:
        return None, None, None
    return provider, parts[0], parts[1]


def _provider_merged_pulls_for_commit(repo: str, commit_sha: str) -> list[dict]:
    """Best-effort provider lookup of the merged PRs that shipped ``commit_sha``.

    PLS-961: a primary-branch deploy starts immediately after merge, while the
    periodic ``sync_linked_pr_statuses`` sweep may not yet have populated the
    linked-PR merge attestation.  Ask the provider itself which PRs are
    associated with this exact commit, and keep only provider-confirmed merged
    PRs whose provider ``merge_commit_sha`` exactly equals the event commit.
    Anything else — open/closed PRs, a different merge result, an unrelated
    repo, an empty response, or a transport error — is omitted, so correlation
    can never be derived from commit prose or incidental containment.

    Returns a list of dicts with ``provider``, ``repo`` (``owner/repo``),
    ``number``, ``merge_commit_sha``, ``head_sha`` and ``merged_at``.
    """
    normalized_sha = _clean(commit_sha).lower()
    if len(normalized_sha) < 40:
        return []
    provider, owner, repo_name = _split_deployment_repo(repo)
    if not provider or not owner or not repo_name:
        return []
    github_token, forgejo_token, origin_url = _provider_token_env()
    try:
        if provider == 'github':
            if not github_token:
                return []
            url = (
                f'https://api.github.com/repos/{owner}/{repo_name}'
                f'/commits/{normalized_sha}/pulls'
            )
            headers = {
                'Authorization': f'Bearer {github_token}',
                'Accept': 'application/vnd.github+json',
                'X-GitHub-Api-Version': '2022-11-28',
            }
            resp = requests.get(url, headers=headers, timeout=10)
        else:
            if not forgejo_token:
                return []
            base = origin_url.rstrip('/')
            url = (
                f'{base}/api/v1/repos/{owner}/{repo_name}'
                f'/commits/{normalized_sha}/pulls'
            )
            resp = requests.get(
                url, headers={'Authorization': f'token {forgejo_token}'}, timeout=10,
            )
        if resp.status_code != 200:
            return []
        payload = resp.json()
        if not isinstance(payload, list):
            return []
    except Exception:
        # Provider reconciliation is additive evidence: a lookup failure must
        # never fail ingestion nor manufacture correlation.
        logger.debug(
            'provider commit→PR lookup failed for %s@%s',
            repo, normalized_sha, exc_info=True,
        )
        return []

    from tasks.services.workflow_service import WorkflowService
    from tasks.tasks import _linked_pr_status_from_payload

    results = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        try:
            normalized = _linked_pr_status_from_payload(provider, entry)
        except Exception:
            continue
        if (normalized or {}).get('status') != 'merged':
            continue
        merge_sha = _clean(normalized.get('merge_commit_sha')).lower()
        if not WorkflowService._sha_matches(merge_sha, normalized_sha):
            continue
        try:
            number = int(entry.get('number') or 0)
        except (TypeError, ValueError):
            continue
        if number <= 0:
            continue
        results.append({
            'provider': provider,
            'repo': f'{owner}/{repo_name}',
            'number': number,
            'merge_commit_sha': merge_sha,
            'head_sha': _clean(normalized.get('head_sha')).lower(),
            'merged_at': _clean(normalized.get('merged_at')),
        })
    return results


def _provider_correlated_task_keys(
    *, normalized_repo: str, normalized_sha: str, wanted_numbers: set[int],
) -> set[str]:
    """Correlate a deployment event through live provider merge identity.

    PLS-961 closes the PR-sync ordering race.  The exact metadata pass looks at
    linked PRs that ALREADY read merged; when the periodic sweep has not run yet
    they still read ``open`` and correlation finds nothing.  Here we correlate
    against provider-confirmed merged PRs for this exact commit and backfill the
    linked-PR tuple with the authoritative merge identity, so the next ingest
    correlates through the metadata pass again (no duplicate provider round-trip).

    Fail-closed: only items that link the exact (repo, PR number) tuple the
    provider named ship this commit are correlated; unrelated repos/SHAs are never
    used and task keys are never derived from commit prose.
    """
    provider_pulls = _provider_merged_pulls_for_commit(normalized_repo, normalized_sha)
    if not provider_pulls:
        return set()
    wanted_by_number = {
        pull['number']: pull
        for pull in provider_pulls
        if not wanted_numbers or pull['number'] in wanted_numbers
    }
    if not wanted_by_number:
        return set()

    from tasks.origin_provenance import parse_linked_pr_identity
    from tasks.services.workflow_service import WorkflowService

    matches = set()
    candidates = Item.objects.filter(
        metadata__pull_requests__isnull=False,
    ).only('item_key', 'metadata')
    for item in candidates.iterator(chunk_size=500):
        meta = dict(item.metadata or {})
        prs = meta.get('pull_requests') or []
        dirty = False
        for index, pr in enumerate(prs):
            if not isinstance(pr, dict):
                continue
            try:
                number = int(pr.get('number') or 0)
            except (TypeError, ValueError):
                number = 0
            pull = wanted_by_number.get(number)
            if pull is None:
                continue
            repo = _clean(pr.get('repo')).lower().rstrip('/')
            if repo and repo != pull['repo']:
                continue
            if not repo:
                identity = parse_linked_pr_identity(pr.get('url'))
                if not identity or f'{identity[0]}/{identity[1]}' != pull['repo']:
                    continue
            if item.item_key:
                matches.add(item.item_key)
            # Backfill/refresh the provider-confirmed merge identity whenever it
            # differs from the stored tuple (this also corrects a stale merge SHA
            # from a re-opened/re-merged PR). The values mirror exactly what the
            # periodic sweep would have recorded, so the next ingest correlates
            # through the metadata pass without another provider round-trip.
            if (
                str(pr.get('status') or '').lower() != 'merged'
                or _clean(pr.get('merge_commit_sha')).lower() != pull['merge_commit_sha']
                or _clean(pr.get('merged_at')) != pull['merged_at']
                or (
                    pull['head_sha']
                    and _clean(pr.get('head_sha')).lower() != pull['head_sha']
                )
            ):
                pr['status'] = 'merged'
                pr['merged_at'] = pull['merged_at'] or pr.get('merged_at')
                pr['merge_commit_sha'] = (
                    pull['merge_commit_sha'] or pr.get('merge_commit_sha')
                )
                if pull['head_sha']:
                    pr['head_sha'] = pull['head_sha']
                dirty = True
        if dirty:
            item.metadata = meta
            item.save(update_fields=['metadata', 'updated_at'])
    return matches


def _linked_task_keys_for_deployment(
    *, repo: str, commit_sha: str, pr_numbers: list[int] | None = None,
) -> list[str]:
    """Resolve tasks from exact linked-PR deployment provenance.

    CI knows the primary-branch merge commit and Forgejo PR number, but it must
    not invent Pulse task keys from commit-message prose.  Pulse already owns
    the linked-PR tuples, so enrich the event at ingestion by matching the
    exact owner/repo plus an exact merge/head SHA (and PR number when supplied).
    This also correlates follow-up verification tasks that link the same PR but
    are not named in its title/body.
    """
    normalized_repo = _clean(repo).lower().rstrip('/')
    normalized_sha = _clean(commit_sha).lower()
    if not normalized_repo or len(normalized_sha) < 40:
        return []
    wanted_numbers = {
        int(value) for value in (pr_numbers or [])
        if str(value).strip().isdigit() and int(value) > 0
    }

    # Import lazily: workflow_service owns the linked-PR normalization contract.
    from tasks.services.workflow_service import WorkflowService

    matches = set()
    candidates = Item.objects.filter(
        metadata__pull_requests__isnull=False,
    ).only('item_key', 'metadata')
    for item in candidates.iterator(chunk_size=500):
        for identity in WorkflowService._merged_pr_heads(item):
            identity_repo = _clean(identity.get('repo')).lower().rstrip('/')
            if identity_repo != normalized_repo:
                continue
            if not WorkflowService._sha_matches(
                identity.get('sha'), normalized_sha,
            ):
                continue
            if wanted_numbers and int(identity.get('number') or 0) not in wanted_numbers:
                continue
            if item.item_key:
                matches.add(item.item_key)
            break
    if not matches:
        # PLS-961: a primary-branch deploy starts immediately after merge, before
        # the periodic PR-sync sweep may have populated the linked-PR merge
        # attestation. Close that ordering gap by correlating against the provider's
        # own merged-PR identity for this exact commit. Best-effort and fail-closed:
        # a lookup failure or an unrelated commit simply leaves correlation empty.
        matches.update(_provider_correlated_task_keys(
            normalized_repo=normalized_repo,
            normalized_sha=normalized_sha,
            wanted_numbers=wanted_numbers,
        ))
    return sorted(matches)


def reingest_events_for_merge(
    *, repo: str, merge_commit_sha: str, item_key: str,
) -> int:
    """Re-ingest existing deployment events so a fresh merge attestation correlates.

    PLS-1303 (PLAT-9027 class): CI producers may emit the deploy event for a
    merge commit BEFORE the linked-PR sweep has attested the merge (or before
    the item even carried the linked PR). Such events are stored with an empty
    or partial ``source_task_keys`` and nothing ever revisits them, so the
    merged-PR task strands non-terminal at ``awaiting_deploy`` forever even
    though a truthful, successful deploy event exists for its exact commit.

    This is the merge-time mirror of the PLS-961 ingest-side correlation:
    when a merge attestation is recorded, re-ingest every existing event for
    (repo, merge_commit_sha) whose correlation does not yet name the task.
    ``upsert_deployment_event`` re-runs ``_linked_task_keys_for_deployment``
    with the attested merge identity and merges producer metadata (PLS-952),
    so the replay is idempotent and can never manufacture correlation that
    the linked-PR tuples do not support — the live gate's success/smoke
    requirements are untouched. Fail-closed: unknown repo/SHA or no events is
    a no-op, never an invented event.
    """
    normalized_repo = _clean(repo).lower().rstrip('/')
    normalized_sha = _clean(merge_commit_sha).lower()
    key = _clean(item_key)
    if not normalized_repo or len(normalized_sha) < 40 or not key:
        return 0
    events = DeploymentEvent.objects.filter(
        repo__iexact=normalized_repo,
        commit_sha__iexact=normalized_sha,
    )
    reingested = 0
    for event in events:
        stored = {str(value) for value in (event.source_task_keys or [])}
        if key in stored or key.lower() in {value.lower() for value in stored}:
            continue
        # Rebuild the payload from the FULL stored row: _deployment_payload
        # applies defaults for absent fields (state='created', service='',
        # pipeline_system='origin-forgejo'), so a minimal payload would
        # clobber the event's truthful state/identity on update. Passing the
        # stored values back keeps the upsert a pure correlation refresh —
        # the only intentional delta is source_task_keys.
        payload = {
            'idempotency_key': event.idempotency_key,
            'repo': normalized_repo,
            'commit_sha': normalized_sha,
            'source_task_keys': sorted(stored | {key}),
        }
        for field in ('service', 'state', 'smoke_result', 'environment', 'cluster',
                      'branch', 'component', 'namespace', 'deployment_target',
                      'image', 'image_tag', 'image_digest', 'manifest_ref',
                      'pipeline_system', 'pipeline_run_id', 'pipeline_url',
                      'logs_url'):
            value = getattr(event, field, None)
            if value not in (None, ''):
                payload[field] = value
        if event.metadata:
            payload['metadata'] = dict(event.metadata)
        upsert_deployment_event(payload)
        reingested += 1
    return reingested


def _terminal_status_slugs() -> frozenset:
    """Canonical terminal-slug set (WorkflowService.TERMINAL_STATUS_SLUGS)."""
    from tasks.services.workflow_service import WorkflowService

    return getattr(WorkflowService, 'TERMINAL_STATUS_SLUGS', frozenset())


def backfill_merge_correlated_events(*, limit: int = 50) -> dict:
    """Periodic healer: correlate stored deploy events for attested merges.

    The at-least-once guarantee (PLAT-8994 shape) for the merge→deploy-event
    correlation leg. The merge-time on_commit schedule covers the normal
    path; this sweep recovers schedules lost to worker death and merges
    attested before the fix shipped (the stranded cohort). Bounded and
    fail-closed: only items whose attested merge commit has an EXISTING
    deployment event lacking the task key are touched — no event, no action
    (repos without deploy producers are the separate emitter-gap family).
    """
    from tasks.models import DeploymentEvent, Item

    healed = {'scanned': 0, 'reingested': 0, 'events': 0}
    # Scan non-terminal items oldest-first: the stranded cohort (PLAT-9027)
    # dates to July/August, so newest-first would never reach it. Fresh merges
    # are covered by the merge-time on_commit schedule; this sweep only needs
    # the backlog. Non-terminal statuses bound the scan to the actual
    # stranding signature (deploy gates / merge gates / verification).
    items = (
        Item.objects.filter(metadata__pull_requests__isnull=False)
        .exclude(status__in=_terminal_status_slugs())
        .only('item_key', 'metadata')
        .order_by('updated_at')[:500]
    )
    for item in items.iterator(chunk_size=200):
        if healed['reingested'] >= limit:
            break
        meta = dict(item.metadata or {})
        key = item.item_key
        if not key:
            continue
        for pr in meta.get('pull_requests') or []:
            if not isinstance(pr, dict):
                continue
            if str(pr.get('status') or '').lower() != 'merged':
                continue
            repo = _clean(pr.get('repo')).lower().rstrip('/')
            sha = _clean(pr.get('merge_commit_sha')).lower()
            if not repo or len(sha) < 40:
                continue
            healed['scanned'] += 1
            exists = DeploymentEvent.objects.filter(
                repo__iexact=repo, commit_sha__iexact=sha,
            ).exists()
            if not exists:
                continue
            count = reingest_events_for_merge(
                repo=repo, merge_commit_sha=sha, item_key=key,
            )
            if count:
                healed['reingested'] += 1
                healed['events'] += count
    return healed


def _resolve_project(payload: Dict[str, Any]):
    project = payload.get('project')
    if isinstance(project, Project):
        return project
    project_id = payload.get('project_id') or payload.get('project')
    if project_id:
        try:
            return Project.objects.filter(pk=int(project_id)).first()
        except (TypeError, ValueError):
            pass
    project_key = _clean(payload.get('project_key')).upper()
    if project_key:
        return Project.objects.filter(key__iexact=project_key, is_active=True).first()
    # Project key is often the first path segment in shizuha-labs/<service>, but
    # keep this conservative; falling back to repo/service-specific ledger rows is OK.
    return None


def _idempotency_key(payload: Dict[str, Any]) -> str:
    explicit = _clean(payload.get('idempotency_key'))
    if explicit:
        return explicit
    return DeploymentEvent.build_idempotency_key(
        repo=payload.get('repo'),
        service=payload.get('service'),
        environment=payload.get('environment') or 'live',
        cluster=payload.get('cluster') or 'shizuha-k3s',
        commit_sha=payload.get('commit_sha'),
        manifest_ref=payload.get('manifest_ref'),
        pipeline_system=payload.get('pipeline_system') or 'origin-forgejo',
        image_digest=payload.get('image_digest'),
        image_tag=payload.get('image_tag'),
        image=payload.get('image'),
        pipeline_run_id=payload.get('pipeline_run_id'),
    )


def _deployment_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    missing = [field for field in _REQUIRED_IDENTITY_FIELDS if not _clean(payload.get(field))]
    if missing:
        raise ValueError(f"Missing required deployment identity field(s): {', '.join(missing)}")
    data = {k: v for k, v in payload.items() if k in _EVENT_FIELDS}
    data['project'] = _resolve_project(payload)
    data['repo'] = _clean(data.get('repo'))
    data['service'] = _clean(data.get('service'))
    data['commit_sha'] = _clean(data.get('commit_sha'))
    data['environment'] = _clean(data.get('environment'), 'live') or 'live'
    data['cluster'] = _clean(data.get('cluster'), 'shizuha-k3s') or 'shizuha-k3s'
    data.setdefault('state', 'created')
    data.setdefault('smoke_result', 'unknown')
    for field in ('branch', 'component', 'namespace', 'deployment_target', 'image', 'image_tag',
                  'image_digest', 'manifest_ref', 'pipeline_system', 'pipeline_run_id',
                  'pipeline_url', 'logs_url'):
        if field in data:
            data[field] = _clean(data[field])
    if not data.get('pipeline_system'):
        data['pipeline_system'] = 'origin-forgejo'
    for field, default in (('pr_numbers', []), ('source_task_keys', []), ('links', {}), ('metadata', {})):
        if data.get(field) is None:
            data[field] = default
    explicit_task_keys = {
        _clean(value) for value in data.get('source_task_keys', [])
        if _clean(value)
    }
    explicit_task_keys.update(_linked_task_keys_for_deployment(
        repo=data['repo'],
        commit_sha=data['commit_sha'],
        pr_numbers=data.get('pr_numbers') or [],
    ))
    data['source_task_keys'] = sorted(explicit_task_keys)
    return data


@transaction.atomic
def upsert_deployment_event(payload: Dict[str, Any]) -> Tuple[DeploymentEvent, bool, Item | None]:
    """Create/update a deployment event and ensure failure automation.

    Returns ``(event, created, devops_task_or_none)``. The idempotency key is
    stable across CI retries, so retries update the same row/task instead of
    creating noise.
    """
    data = _deployment_payload(dict(payload))
    key = _idempotency_key({**payload, **data})
    event, created = DeploymentEvent.objects.select_for_update().get_or_create(
        idempotency_key=key,
        defaults=data,
    )
    if not created:
        dirty = []
        for field, value in data.items():
            if field == 'metadata' and isinstance(value, dict):
                # PLS-952: merge producer metadata instead of replacing it. A
                # partial upsert (e.g. a DevOps carrier recording
                # replicas_ready/image_digest evidence) must not destroy typed
                # fields the producer wrote (e.g. risk_labels) just because the
                # payload did not restate them — silent data loss on a
                # sanctioned repair path. Incoming keys win on conflict.
                merged = {**(event.metadata or {}), **value}
                if merged != (event.metadata or {}):
                    event.metadata = merged
                    dirty.append(field)
                continue
            if getattr(event, field) != value:
                setattr(event, field, value)
                dirty.append(field)
        if dirty:
            event.save(update_fields=dirty + ['updated_at'])
    failure_task = ensure_failed_deployment_task(event) if event.is_failure else None
    if event.is_failure:
        # PLAT-1210 review P1: a rollout_started event may already have created
        # a QA regression watchlist; when the SAME event progresses to a
        # failure state, retire that watchlist — QA must not re-verify
        # surfaces against a rollout that did not pass.
        from tasks.services.regression_watchlist import maybe_retire_watchlist
        maybe_retire_watchlist(event)
    else:
        # PLAT-1210: a successful rollout re-verifies the service's
        # recently-fixed bug surfaces (PLAT-287 -> BKS-44 escape class).
        from tasks.services.regression_watchlist import maybe_create_watchlist
        maybe_create_watchlist(event)
    # PLS-184: a successful/routine deploy stays ledger-only, but if the event
    # references high-risk source tasks (or carries explicit risk_labels metadata),
    # create focused QA work idempotently. Run ONLY on event creation: re-ingest
    # (CI retry / same idempotency key) must not re-mint focused-QA tasks for the
    # same deploy — the dedupe only matches OPEN tasks, so re-running on update
    # would create a fresh round of QA-of-QA tasks every time the event row is
    # touched (observed as an infinite focused-QA loop on dojo UX closes).
    if created:
        try:
            from tasks.services.focused_qa import ensure_focused_qa_for_deployment_event
            ensure_focused_qa_for_deployment_event(event, trigger='deployment_event')
        except Exception:
            # Deployment ingestion must not fail just because QA task generation did;
            # the ledger row is the source of truth and the next retry can regenerate.
            # PLS-1217: never swallow silently — a silent no-op here is indistinguishable
            # from gating (process-meta origin, kill switch, label mismatch) and cost a
            # full canary arc to diagnose. The log line is the diagnostic surface.
            logger.warning(
                "focused-QA spawn failed for deployment event %s (service=%s sha=%s)",
                event.idempotency_key,
                event.service,
                event.commit_sha,
                exc_info=True,
            )
    return event, created, failure_task


def _failure_kind(event: DeploymentEvent) -> str:
    if event.state in _FAILURE_KIND_BY_STATE:
        return _FAILURE_KIND_BY_STATE[event.state]
    if event.smoke_result == 'failed':
        return 'smoke'
    return 'deployment'


def _failure_title(event: DeploymentEvent, failure_kind: str) -> str:
    service = event.service or event.repo
    return f"[Deploy {failure_kind} failed] {service} → {event.environment} ({event.commit_sha[:12]})"[:400]


def _failure_description(event: DeploymentEvent, failure_kind: str) -> str:
    bits = [
        f"CI reported a **{failure_kind}** deployment failure for `{event.service}`.",
        '',
        f"- repo: `{event.repo}`",
        f"- branch: `{event.branch or 'unknown'}`",
        f"- commit: `{event.commit_sha}`",
        f"- environment: `{event.environment}`",
        f"- cluster: `{event.cluster}`",
        f"- namespace/target: `{event.namespace or 'unknown'}` / `{event.deployment_target or 'unknown'}`",
        f"- image: `{event.image_digest or event.image_tag or event.image or 'unknown'}`",
        f"- state: `{event.state}`; smoke: `{event.smoke_result}`",
    ]
    if event.pipeline_url:
        bits.append(f"- pipeline: {event.pipeline_url}")
    if event.logs_url:
        bits.append(f"- logs: {event.logs_url}")
    bits.extend([
        '',
        'Acceptance:',
        '- [ ] diagnose the deploy/smoke failure from logs/live state',
        '- [ ] apply a durable fix or roll back safely',
        '- [ ] verify the next deployment event is successful',
        '',
        f"DeploymentEvent: `{event.id}` (idempotency `{event.idempotency_key}`)",
    ])
    return '\n'.join(bits)


def _origin_observation(event: DeploymentEvent, failure_kind: str) -> dict[str, str]:
    """Translate the deployment ledger identity into task-facing CI evidence."""
    if event.pipeline_system not in {'origin-forgejo', 'forgejo', 'gitea-actions'}:
        return {}
    values = {
        'provider': 'origin-forgejo',
        'repo': event.repo,
        'ref': event.branch,
        'workflow': (event.metadata or {}).get('workflow'),
        'check': f'deployment-{failure_kind}',
        'run_id': event.pipeline_run_id,
        'run_url': event.pipeline_url,
        'logs_url': event.logs_url,
        'image': event.image_tag or event.image,
        'image_digest': event.image_digest,
        'status': event.state,
        'observed_at': event.updated_at.isoformat() if event.updated_at else '',
    }
    commit_sha = (event.commit_sha or '').strip().lower()
    if 7 <= len(commit_sha) <= 64 and all(c in '0123456789abcdef' for c in commit_sha):
        values['commit_sha'] = commit_sha
    try:
        return normalize_origin_observation({k: v for k, v in values.items() if v})
    except ValueError as exc:
        # Provenance is additive evidence. A malformed legacy pipeline URL/repo
        # must never suppress the urgent deployment-failure task itself.
        logger.warning(
            'Skipping invalid Origin provenance for DeploymentEvent %s: %s',
            event.id, exc,
        )
        return {}


def _find_existing_failure_task(event: DeploymentEvent, failure_kind: str) -> Item | None:
    if event.failure_task_id and event.failure_task.status not in _TERMINAL_STATUSES:
        return event.failure_task
    return (
        Item.objects
        .filter(
            source='deployment-event',
            metadata__deployment_event_id=str(event.id),
            metadata__deployment_failure_kind=failure_kind,
        )
        .exclude(status__in=_TERMINAL_STATUSES)
        .order_by('-created_at')
        .first()
    )


@transaction.atomic
def ensure_failed_deployment_task(event: DeploymentEvent) -> Item:
    """Create/update one urgent DevOps task for this event+failure kind."""
    failure_kind = _failure_kind(event)
    existing = _find_existing_failure_task(event, failure_kind)
    title = _failure_title(event, failure_kind)
    description = _failure_description(event, failure_kind)
    metadata = {
        'deployment_event_id': str(event.id),
        'deployment_event_key': event.idempotency_key,
        'deployment_failure_kind': failure_kind,
        'repo': event.repo,
        'service': event.service,
        'environment': event.environment,
        'cluster': event.cluster,
        'commit_sha': event.commit_sha,
    }
    observation = _origin_observation(event, failure_kind)
    if observation:
        metadata['origin_observations'] = merge_origin_observations(
            (existing.metadata or {}).get('origin_observations') if existing else [],
            [observation],
        )
    if existing:
        existing.title = title
        existing.description = description
        existing.priority = 'urgent'
        existing.assignment_group = 'devops'
        existing.metadata = {**(existing.metadata or {}), **metadata}
        existing.source_url = event.logs_url or event.pipeline_url or existing.source_url
        existing.save(update_fields=['title', 'description', 'priority', 'assignment_group', 'metadata', 'source_url', 'updated_at'])
        event.failure_task = existing
        event.save(update_fields=['failure_task', 'updated_at'])
        Comment.objects.get_or_create(
            item=existing,
            author_email='ci@shizuha.com',
            author_id=0,
            content=f"DeploymentEvent `{event.id}` retried/updated; keeping this as the single DevOps task for {failure_kind} failure.",
        )
        return existing

    project = event.project or Project.objects.filter(key__iexact='PLS', is_active=True).first() or Project.objects.filter(is_active=True).order_by('id').first()
    item_key = project.get_next_item_key() if project else None
    task = Item.objects.create(
        project=project,
        item_key=item_key,
        mode='task',
        item_type='task.deployment_failure',
        title=title,
        description=description,
        source='deployment-event',
        source_id=str(event.id),
        source_url=event.logs_url or event.pipeline_url or None,
        status='open',
        priority='urgent',
        assignment_group='devops',
        workflow_name='autonomous-bug',
        created_by_id=0,
        created_by_email='ci@shizuha.com',
        labels={'source': 'deployment-event', 'service': event.service, 'environment': event.environment},
        tags='deployment-events,ci,devops',
        metadata=metadata,
    )
    try:
        from tasks.services.workflow_service import WorkflowService
        uid, email = WorkflowService._resolve_team_assignee(
            'devops', exclude_email=None, enforce_cap=True,
            project_key=(project.key if project else None), item=task,
        )
        if uid or email:
            task.assign({'id': uid, 'email': email}, save=True)
    except Exception:
        # Group assignment is the durable route; concrete assignment is best-effort.
        pass
    event.failure_task = task
    event.save(update_fields=['failure_task', 'updated_at'])
    Comment.objects.create(
        item=task,
        author_email='ci@shizuha.com',
        author_id=0,
        content=f"Auto-created from DeploymentEvent `{event.id}`; CI retries for the same idempotency key update this task instead of creating duplicates.",
    )
    return task
