#!/usr/bin/env bash
# Publish a converged Origin rollout to Pulse's idempotent DeploymentEvent ledger.
# The primary-branch merge commit + Forgejo PR number are the correlation authority;
# Pulse resolves linked task keys from its own stored PR tuples.
set -euo pipefail

usage() {
  echo "usage: $0 <project-key> <namespace> <service> <component> <deployment-target> <image> [qa-surface,...]" >&2
  exit 64
}

[ "$#" -ge 6 ] && [ "$#" -le 7 ] || usage
PROJECT_KEY="$1"
NAMESPACE="$2"
SERVICE="$3"
COMPONENT="$4"
DEPLOYMENT_TARGET="$5"
IMAGE="$6"
QA_SURFACES="${7:-}"
REPO="${GITHUB_REPOSITORY:-}"
SHA="${GITHUB_SHA:-}"
BRANCH="${GITHUB_REF_NAME:-}"
RUN_ID="${GITHUB_RUN_ID:-${GITHUB_RUN_NUMBER:-unknown}}"
PULSE_URL="${PULSE_URL:-http://shizuha-pulse.shizuha.svc.cluster.local:8002}"
ID_LOGIN_URL="${ID_LOGIN_URL:-http://shizuha-id.shizuha.svc.cluster.local:8001/api/auth/login/}"
FORGEJO_URL="${FORGEJO_URL:-http://forgejo-http.origin.svc.cluster.local}"
DRY_RUN="${DEPLOYMENT_EVENT_DRY_RUN:-0}"

[[ "$PROJECT_KEY" =~ ^[A-Z][A-Z0-9]{1,9}$ ]] || {
  echo "::error::project key must be an uppercase Pulse project key" >&2
  exit 2
}
[[ "$REPO" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || {
  echo "::error::GITHUB_REPOSITORY must be owner/repo" >&2
  exit 2
}
[[ "$SHA" =~ ^[0-9a-f]{40}$ ]] || {
  echo "::error::GITHUB_SHA must be a full lowercase merge SHA" >&2
  exit 2
}
[ -n "$NAMESPACE" ] && [ -n "$SERVICE" ] && [ -n "$COMPONENT" ] \
  && [ -n "$DEPLOYMENT_TARGET" ] && [ -n "$IMAGE" ] || usage

if [ "$DRY_RUN" = "1" ]; then
  PR_NUMBER="${DEPLOYMENT_EVENT_PR_NUMBER:-1}"
  DESIRED="${DEPLOYMENT_EVENT_DESIRED_REPLICAS:-2}"
  READY="${DEPLOYMENT_EVENT_READY_REPLICAS:-$DESIRED}"
  UPDATED="${DEPLOYMENT_EVENT_UPDATED_REPLICAS:-$DESIRED}"
  AVAILABLE="${DEPLOYMENT_EVENT_AVAILABLE_REPLICAS:-$DESIRED}"
  GENERATION="${DEPLOYMENT_EVENT_GENERATION:-1}"
  OBSERVED_GENERATION="${DEPLOYMENT_EVENT_OBSERVED_GENERATION:-$GENERATION}"
else
  # The deploy-lane runner has no direct Forgejo PAT: the token is only
  # available where build Jobs mount it (secretKeyRef forgejo-token, key
  # password in the build namespace) or as $FORGEJO_TOKEN on broker agents.
  # This step is defense-in-depth ONLY - Pulse independently correlates the
  # exact merge SHA to linked-PR tuples at ingest (shizuha-tasks#240) and
  # fail-louds on no correlation. So if no token is readable, degrade to
  # PR_NUMBER=0 and let Pulse's authoritative ingest gate rule. (PLAT-5852:
  # run 478 failed hard here on `kubectl -n origin get secret forgejo-token`
  # ModuleNotFound-free but NotFound, red-lighting every connect deploy.)
  FORGEJO_TOKEN_VALUE="${FORGEJO_TOKEN:-}"
  if [ -z "$FORGEJO_TOKEN_VALUE" ]; then
    FORGEJO_TOKEN_VALUE="$(kubectl -n origin get secret forgejo-token -o jsonpath='{.data.password}' 2>/dev/null | base64 -d || true)"
  fi
  if [ -z "$FORGEJO_TOKEN_VALUE" ]; then
    FORGEJO_TOKEN_VALUE="$(kubectl -n build get secret forgejo-token -o jsonpath='{.data.password}' 2>/dev/null | base64 -d || true)"
  fi
  PR_NUMBER=0
  PR_MERGE_SHA=""
  PR_MERGED="false"
  if [ -n "$FORGEJO_TOKEN_VALUE" ]; then
    pull_response="$(curl -fsS \
      --retry 2 --retry-all-errors --retry-delay 2 --retry-max-time 30 \
      --connect-timeout 5 --max-time 10 \
      -H "Authorization: token ${FORGEJO_TOKEN_VALUE}" \
      "${FORGEJO_URL}/api/v1/repos/${REPO}/commits/${SHA}/pull")" || pull_response=""
    read -r PR_NUMBER PR_MERGE_SHA PR_MERGED <<EOF_PULL
$(printf '%s' "${pull_response:-}" | python3 -c '
import json, sys
try:
    row = json.load(sys.stdin)
    print(row.get("number") or 0, row.get("merge_commit_sha") or "", "true" if row.get("merged") else "false")
except Exception:
    print("0", "", "false")
')
EOF_PULL
    unset pull_response
    FORGEJO_TOKEN_VALUE=""
    if [ "$PR_NUMBER" -gt 0 ] && [ "$PR_MERGED" = "true" ] && [ "$PR_MERGE_SHA" = "$SHA" ]; then
      echo "forgejo PR provenance: ${REPO}@${SHA} -> PR ${PR_NUMBER}"
    else
      echo "::warning::Forgejo PR provenance unavailable for ${REPO}@${SHA}; Pulse correlated ingest is authoritative" >&2
      PR_NUMBER=0
    fi
  else
    echo "::warning::Forgejo API token unavailable; skipping inline PR provenance (Pulse-linked-PR ingest is authoritative)" >&2
  fi
  unset FORGEJO_TOKEN_VALUE

  deployment_json="$(kubectl -n "$NAMESPACE" get "deployment/$DEPLOYMENT_TARGET" -o json)"
  read -r DESIRED READY UPDATED AVAILABLE GENERATION OBSERVED_GENERATION <<EOF_DEPLOY
$(printf '%s' "$deployment_json" | python3 -c '
import json, sys
row = json.load(sys.stdin)
spec, status, meta = row.get("spec", {}), row.get("status", {}), row.get("metadata", {})
values = [
    spec.get("replicas") or 0,
    status.get("readyReplicas") or 0,
    status.get("updatedReplicas") or 0,
    status.get("availableReplicas") or 0,
    meta.get("generation") or 0,
    status.get("observedGeneration") or 0,
]
print(*values)
')
EOF_DEPLOY
  unset deployment_json
fi

[ "$DESIRED" -gt 0 ] \
  && [ "$READY" -eq "$DESIRED" ] \
  && [ "$UPDATED" -eq "$DESIRED" ] \
  && [ "$AVAILABLE" -eq "$DESIRED" ] \
  && [ "$OBSERVED_GENERATION" -ge "$GENERATION" ] || {
    echo "::error::Deployment $NAMESPACE/$DEPLOYMENT_TARGET is not converged: desired=$DESIRED ready=$READY updated=$UPDATED available=$AVAILABLE generation=$GENERATION observed=$OBSERVED_GENERATION" >&2
    exit 1
  }

if [ "$DRY_RUN" != "1" ]; then
  # PLAT-7957 (third consumer, sara's triage): the live `pulse-api-token`
  # secret carries only the `token` key after the rotation — prefer the
  # minted token as Bearer directly and keep the username/password ID-login
  # path only as a legacy fallback. The legacy-first reads returned empty on
  # the rotated secret, which failed this emitter fail-loud on every wiki
  # deploy (observed 09-20: run 47770 emitted no event; the stop-bleed was a
  # manual seat ingest).
  PULSE_TOKEN="${PULSE_TOKEN:-$(kubectl -n origin get secret pulse-api-token -o jsonpath='{.data.token}' 2>/dev/null | base64 -d || true)}"
  if [ -z "$PULSE_TOKEN" ]; then
    PULSE_USERNAME="${PULSE_USERNAME:-$(kubectl -n origin get secret pulse-api-token -o jsonpath='{.data.username}' 2>/dev/null | base64 -d || true)}"
    PULSE_PASSWORD="${PULSE_PASSWORD:-$(kubectl -n origin get secret pulse-api-token -o jsonpath='{.data.password}' 2>/dev/null | base64 -d || true)}"
    [ -n "$PULSE_USERNAME" ] && [ -n "$PULSE_PASSWORD" ] || {
      echo "::error::Pulse DeploymentEvent credentials unavailable" >&2
      exit 1
    }
    login_payload="$(PULSE_USERNAME="$PULSE_USERNAME" PULSE_PASSWORD="$PULSE_PASSWORD" python3 -c 'import json,os; print(json.dumps({"username":os.environ["PULSE_USERNAME"],"password":os.environ["PULSE_PASSWORD"]}))')"
    login_response="$(curl -fsS \
      --retry 4 --retry-all-errors --retry-delay 2 --retry-max-time 75 \
      --connect-timeout 5 --max-time 15 \
      -H 'Content-Type: application/json' -d "$login_payload" "$ID_LOGIN_URL")" || {
        echo "::error::ID login failed while minting a fresh Pulse access token" >&2
        exit 1
      }
    PULSE_TOKEN="$(printf '%s' "$login_response" | python3 -c 'import json,sys; print(json.load(sys.stdin)["tokens"]["access"])')"
    unset PULSE_PASSWORD login_payload login_response
    [ -n "$PULSE_TOKEN" ] || {
      echo "::error::Pulse login returned no access token" >&2
      exit 1
    }
  fi
fi

export PROJECT_KEY NAMESPACE SERVICE COMPONENT DEPLOYMENT_TARGET IMAGE QA_SURFACES
export REPO SHA BRANCH RUN_ID PR_NUMBER DESIRED READY UPDATED AVAILABLE
export GENERATION OBSERVED_GENERATION
payload="$(python3 - <<'PY'
import json
import os

surfaces = [value.strip() for value in os.environ["QA_SURFACES"].split(",") if value.strip()]
run_id = os.environ["RUN_ID"]
repo = os.environ["REPO"]
payload = {
    "idempotency_key": ":".join([
        "origin", repo, os.environ["SERVICE"], os.environ["COMPONENT"], os.environ["SHA"],
    ]),
    "project_key": os.environ["PROJECT_KEY"],
    "repo": repo,
    "branch": os.environ["BRANCH"],
    "commit_sha": os.environ["SHA"],
    "service": os.environ["SERVICE"],
    "component": os.environ["COMPONENT"],
    "environment": "live",
    "cluster": "shizuha-k3s",
    "namespace": os.environ["NAMESPACE"],
    "deployment_target": os.environ["DEPLOYMENT_TARGET"],
    "image": os.environ["IMAGE"],
    "pipeline_system": "origin-forgejo",
    "pipeline_run_id": run_id,
    "pipeline_url": os.environ.get(
        "PIPELINE_URL",
        # PLS-1409 / CON-466: public pipeline URL base is env-configurable;
        # default keeps first-party behaviour.
        f"https://origin.shizuha.com/{repo}/actions/runs/{run_id}",
    ),
    "state": "smoke_passed",
    "smoke_result": "passed",
    "pr_numbers": [int(os.environ["PR_NUMBER"])],
    "metadata": {
        "risk_labels": ["infra-control-plane", "requires-live-qa", "user-facing"],
        "qa_surfaces": surfaces,
        "truth_contract": "origin-rollout-deployment-event-v1",
        "replicas_ready": True,
        "desired_replicas": int(os.environ["DESIRED"]),
        "ready_replicas": int(os.environ["READY"]),
        "updated_replicas": int(os.environ["UPDATED"]),
        "available_replicas": int(os.environ["AVAILABLE"]),
        "generation": int(os.environ["GENERATION"]),
        "observed_generation": int(os.environ["OBSERVED_GENERATION"]),
    },
}
print(json.dumps(payload, separators=(",", ":")))
PY
)"

if [ "$DRY_RUN" = "1" ]; then
  printf '%s\n' "$payload"
  exit 0
fi

IDEMPOTENCY_KEY="$(printf '%s' "$payload" | python3 -c 'import json,sys; print(json.load(sys.stdin)["idempotency_key"])')"
response="$(curl -fsS \
  --retry 4 --retry-all-errors --retry-delay 2 --retry-max-time 150 \
  --connect-timeout 5 --max-time 30 -X POST \
  -H "Authorization: Bearer ${PULSE_TOKEN}" \
  -H "Idempotency-Key: ${IDEMPOTENCY_KEY}" \
  -H 'Content-Type: application/json' \
  -d "$payload" \
  "${PULSE_URL}/api/deployment-events/ingest/")"
unset PULSE_TOKEN IDEMPOTENCY_KEY payload
printf '%s' "$response" | python3 -c '
import json, sys
row = json.load(sys.stdin)
tasks = row.get("source_task_keys") or []
prs = row.get("pr_numbers") or []
if not row.get("id") or row.get("state") not in {"smoke_passed", "verified_by_qa"}:
    raise SystemExit("Pulse returned an invalid DeploymentEvent response")
if not tasks:
    raise SystemExit("DeploymentEvent did not correlate to any linked Pulse task")
print("Pulse DeploymentEvent {} state={} pr_numbers={} source_task_keys={} qa_task={}".format(
    row["id"], row["state"], prs, tasks, row.get("qa_task_key")
))
'
