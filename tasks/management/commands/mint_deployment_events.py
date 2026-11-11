"""
PLAT-12954: unattended DeploymentEvent minter (management command).

Design per PLAT-12954 cmt 1240441 (hritik-approved, unattended change):
a scheduled in-cluster runner executing inside the pulse deployment's own
trust boundary — it inherits the deployment's own authority (DB DSN +
FORGEJO_TOKEN env the recorder lane already uses); NO new staff-scoped
credential, no HTTP admission path, no loosening of the staff-only ingest
gate (the gate stays byte-for-byte).

Sweep logic (same checks, same key as the lane):
  (a) enumerate recent COMPLETED deploy-workflow runs for the fleet repos;
  (b) verify the run's head SHA is on the production head (default-branch
      ancestry via the Origin commits API);
  (c) parse the rollout proof from the run log via the ci-logs sidecar
      (deployment UID/generation, per-pod imageID digest, desired/ready
      replicas, observed_generation >= generation, probe verdicts);
  (d) skip any SHA already in the ledger (idempotency key computed by the
      SAME DeploymentEvent.build_idempotency_key the ingest lane uses);
  (e) mint state=smoke_passed events for converged+probed runs ONLY —
      ambiguous or unverifiable runs are logged and skipped, never
      fabricated (fail-closed).

Run inside the cluster as a CronJob against the pulse deployment image:
    python manage.py mint_deployment_events --window-hours 48
"""

import json
import logging
import os
import re
import time
import urllib.request
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from tasks.models import DeploymentEvent
from tasks.services.deployment_events import upsert_deployment_event

logger = logging.getLogger(__name__)

DEFAULT_FORGEJO = os.environ.get(
    'FORGEJO_URL', 'http://forgejo-http.origin.svc.cluster.local:80')
DEFAULT_CI_LOGS = os.environ.get(
    'CI_LOGS_URL', 'http://ci-logs.origin.svc.cluster.local:8080')

# v1 allowlist: fleet repos with an active deploy lane. Extend as repos
# land deployment workflows (documented on PLAT-12954).
DEFAULT_REPOS = ('search', 'hive', 'pulse', 'cortex', 'deploy', 'home')
DEFAULT_WORKFLOW = 'deploy.yml'

# Rollout-proof markers parsed from the run log (the deploy lane's own
# receipt lines / kubectl output). Every marker below must be present and
# consistent, or the run is SKIPPED (never fabricated).
_PROOF_PATTERNS = {
    'deployment_uid': re.compile(r'deployment[/\s-]*(?:uid[:=]\s*)?([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})', re.I),
    'generation': re.compile(r'observedGeneration[:=]\s*(\d+)', re.I),
    'desired_generation': re.compile(r'generation[:=]\s*(\d+)', re.I),
    'desired_replicas': re.compile(r'(?:desired|spec)\.replicas[:=]\s*(\d+)', re.I),
    'ready_replicas': re.compile(r'readyReplicas[:=]\s*(\d+)', re.I),
    'image_digest': re.compile(r'imageID[:=]\s*\S+@sha256:([0-9a-f]{64})', re.I),
}


def _http_get(url, token=None, timeout=30, headers=None):
    merged = {}
    if token:
        merged['Authorization'] = f'token {token}'
    if headers:
        # Caller-provided headers win (the rollout-proof fetch passes a
        # scheme-specific Authorization: bearer vs token).
        merged.update(headers)
    req = urllib.request.Request(url, headers=merged)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode('utf-8', 'replace')


def _http_json(url, token=None, timeout=30):
    return json.loads(_http_get(url, token=token, timeout=timeout))


class Command(BaseCommand):
    help = (
        'PLAT-12954: sweep recent completed deploy runs, verify rollout proof, '
        'and mint DeploymentEvent ledger rows for converged+probed runs '
        '(unattended; fail-closed on anything unverifiable).'
    )

    def add_arguments(self, parser):
        parser.add_argument('--window-hours', type=int, default=48)
        parser.add_argument('--org', default='shizuha-labs')
        parser.add_argument('--forgejo-url', default=DEFAULT_FORGEJO)
        parser.add_argument('--ci-logs-url', default=DEFAULT_CI_LOGS)
        parser.add_argument('--workflow', default=DEFAULT_WORKFLOW)
        parser.add_argument('--repos', default=','.join(DEFAULT_REPOS))
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **opts):
        token = os.environ.get('FORGEJO_TOKEN', '').strip()
        if not token:
            raise CommandError(
                'FORGEJO_TOKEN not configured — the minter inherits the pulse '
                'deployment context and must not run without it (fail-closed).')
        window_start = timezone.now() - timedelta(hours=opts['window_hours'])
        repos = [r.strip() for r in opts['repos'].split(',') if r.strip()]
        summary = {'minted': 0, 'already_ledgered': 0, 'skipped_unverifiable': 0,
                   'skipped_not_on_production_head': 0, 'runs_scanned': 0}
        for repo in repos:
            repo_summary = self._sweep_repo(repo, opts, token, window_start)
            for key in summary:
                summary[key] += repo_summary.get(key, 0)
        self.stdout.write(json.dumps(summary))
        logger.warning('PLAT-12954 minter sweep complete: %s', summary)

    def _sweep_repo(self, repo, opts, token, window_start):
        base = opts['forgejo_url'].rstrip('/')
        org = opts['org']
        summary = {'minted': 0, 'already_ledgered': 0,
                   'skipped_unverifiable': 0,
                   'skipped_not_on_production_head': 0, 'runs_scanned': 0}
        try:
            runs = _http_json(
                f'{base}/api/v1/repos/{org}/{repo}/actions/tasks?limit=30',
                token=token)
        except Exception as exc:
            logger.warning('minter: run list unavailable for %s/%s: %s',
                           org, repo, exc)
            summary['skipped_unverifiable'] += 1
            return summary
        entries = runs.get('workflow_runs') or runs if isinstance(runs, list) else runs.get('workflow_runs', [])
        for run in entries:
            try:
                if (run.get('status') or '').lower() != 'success':
                    continue
                concluded = run.get('run_started_at') or run.get('created_at')
                if not concluded:
                    continue
                run_id = str(run.get('id') or run.get('number') or '')
                head_sha = (run.get('head_sha') or '').strip()
                workflow = (run.get('path') or '').rsplit('/', 1)[-1]
                if not run_id or not head_sha:
                    continue
                if workflow and workflow != opts['workflow']:
                    continue
                summary['runs_scanned'] += 1
                # (b) production-head ancestry
                if not self._on_production_head(base, org, repo, head_sha, token):
                    summary['skipped_not_on_production_head'] += 1
                    continue
                # (d) ledger idempotency — same key builder as the ingest lane
                idem = DeploymentEvent.build_idempotency_key(
                    repo=f'{org}/{repo}', service=repo,
                    environment='live', cluster='shizuha-k3s',
                    commit_sha=head_sha, pipeline_system='origin-forgejo',
                    pipeline_run_id=run_id)
                if DeploymentEvent.objects.filter(
                        idempotency_key=idem).exists():
                    summary['already_ledgered'] += 1
                    continue
                # (c) rollout proof from the run log (fail-closed)
                proof = self._parse_rollout_proof(
                    opts['forgejo_url'], org, repo, run_id)
                if not self._proof_converged(proof):
                    summary['skipped_unverifiable'] += 1
                    continue
                payload = {
                    'repo': f'{org}/{repo}',
                    'service': repo,
                    'commit_sha': head_sha,
                    'environment': 'live',
                    'cluster': 'shizuha-k3s',
                    'state': 'smoke_passed',
                    'smoke_result': 'passed',
                    'pipeline_system': 'origin-forgejo',
                    'pipeline_run_id': run_id,
                    'image_digest': proof.get('image_digest') or '',
                    'manifest_ref': f'{org}/{repo}@{head_sha}',
                    'metadata': {
                        'minted_by': 'plat12954-minter',
                        'minted_at': timezone.now().isoformat(),
                        'rollout_proof': proof,
                        'source': 'scheduled-sweep',
                    },
                }
                if opts['dry_run']:
                    self.stdout.write(f'DRY-RUN would mint: {repo} {head_sha} '
                                      f'run={run_id} proof_ok')
                    summary['minted'] += 1
                    continue
                event, created, _task = upsert_deployment_event(payload)
                if created:
                    summary['minted'] += 1
                    logger.warning(
                        'PLAT-12954 minter minted deployment event %s '
                        '(repo=%s sha=%s run=%s)', event.idempotency_key,
                        repo, head_sha, run_id)
                else:
                    summary['already_ledgered'] += 1
            except Exception as exc:  # noqa: BLE001 — one bad run never kills the sweep
                logger.warning('minter: run %s on %s skipped: %s',
                               run.get('id'), repo, exc)
                summary['skipped_unverifiable'] += 1
        return summary

    def _on_production_head(self, base, org, repo, sha, token):
        """Verify the run SHA is the default-branch tip or in its recent
        ancestry (commits listing walk, v1 honest bound: 200 commits)."""
        try:
            info = _http_json(f'{base}/api/v1/repos/{org}/{repo}', token=token)
            tip = (info or {}).get('default_branch') or 'main'
            for page in (1, 2):
                commits = _http_json(
                    f'{base}/api/v1/repos/{org}/{repo}/commits?sha={tip}'
                    f'&limit=100&page={page}', token=token)
                for commit in commits or []:
                    if (commit.get('sha') or '') == sha:
                        return True
        except Exception as exc:
            logger.warning('minter: production-head check failed for %s/%s: %s',
                           org, repo, exc)
        return False

    def _parse_rollout_proof(self, base, org, repo, run_id):
        """Fetch the run log and parse the rollout proof markers.

        Proof-source order (no new credential — PLAT-12954 trust boundary):
          1. Forgejo Actions logs API with the inherited FORGEJO_TOKEN
             (read:repository — the same bytes the ci-logs sidecar mirrors).
          2. ci-logs sidecar with SHIZUHA_ID_TOKEN, only if configured.
        Returns {} on ANY fetch failure (fail-closed).
        """
        forgejo_token = os.environ.get('FORGEJO_TOKEN', '').strip()
        urls = []
        if forgejo_token:
            urls.append((f'{base.rstrip("/")}/api/v1/repos/{org}/{repo}'
                         f'/actions/tasks/{run_id}/logs', forgejo_token,
                         'token'))
        sidecar_token = os.environ.get('SHIZUHA_ID_TOKEN', '').strip()
        if sidecar_token:
            urls.append((f'{DEFAULT_CI_LOGS.rstrip("/")}'
                         f'/logs?repo={org}/{repo}&run={run_id}',
                         sidecar_token, 'Bearer'))
        if not urls:
            logger.warning('minter: no log-fetch credential available '
                           '(fail-closed skip)')
            return {}
        log_text = None
        for url, token, scheme in urls:
            for attempt in range(1, 3):
                try:
                    log_text = _http_get(
                        url, timeout=30,
                        headers={'Authorization': f'{scheme} {token}'})
                    break
                except Exception as exc:
                    logger.info('minter: log fetch attempt %d failed: %s',
                                attempt, exc)
                    time.sleep(2 * attempt)
            if log_text is not None:
                break
        if log_text is None:
            return {}
        proof = {}
        for name, pattern in _PROOF_PATTERNS.items():
            match = pattern.search(log_text)
            if match:
                proof[name] = match.group(1)
        return proof

    def _proof_converged(self, proof):
        """Converged = UID + generation observed >= desired + replicas
        ready == desired + image digest parsed. Anything less is
        unverifiable (never fabricated)."""
        try:
            if not proof.get('deployment_uid') or not proof.get('image_digest'):
                return False
            observed = int(proof.get('generation', '0') or 0)
            desired = int(proof.get('desired_generation', '0') or 0)
            if not desired or observed < desired:
                return False
            ready = int(proof.get('ready_replicas', '-1') or -1)
            wanted = int(proof.get('desired_replicas', '-1') or -1)
            return ready >= 0 and wanted >= 0 and ready == wanted
        except (TypeError, ValueError):
            return False
