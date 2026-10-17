#!/bin/bash
# Local mirror of ci-test-shizuha-pulse (Origin deploy.yml `test` job): the
# tasks suite in the SAME image lineage CI uses, against throwaway pg+redis.
# Agent runtimes use a separate DinD filesystem, so source reaches the test
# container through `docker cp` (never a bind mount). If the node-local Pulse
# image is unreachable from DinD, the gate falls back to the workflow's public
# python:3.12-bookworm base and resolves the frozen lock directly. PostgreSQL
# and Redis already run as sidecars and every locked native dependency has an
# arm64/amd64 wheel, so the fallback deliberately has no apt bootstrap.
# Blocks primary-branch pushes via .githooks/pre-push.
# Speed (2026-07-14): PostgreSQL's package/data cache persists on volume
# pulse-gate-pgdata and the suite uses the shared SHIZUHA_TEST_JOBS budget
# (tblib enables traceback pickling). Test databases are deliberately rebuilt:
# Django's --keepdb preserves the seed-depleted state left by TransactionTestCase
# flushes, making later parallel runs order-dependent. Reset a wedged cache with:
#   docker volume rm pulse-gate-pgdata
set -euo pipefail
TEST_JOBS="${SHIZUHA_TEST_JOBS:-8}"
TEST_TARGET="${SHIZUHA_TEST_TARGET:-tasks}"
[[ "$TEST_JOBS" =~ ^[1-9][0-9]*$ ]] || {
  echo "gate: SHIZUHA_TEST_JOBS must be a positive integer" >&2
  exit 64
}
[[ "$TEST_TARGET" =~ ^[A-Za-z0-9_.]+$ ]] || {
  echo "gate: SHIZUHA_TEST_TARGET must be a dotted Django test label" >&2
  exit 64
}
# Concurrent gate runs share the pulse-gate-pgdata volume: two postgres
# containers on one data dir corrupt it (then every later run wedges in crash
# recovery). flock queues concurrent pushes; 15min cap then fail loud.
exec 9>/tmp/pulse-gate.volume.lock
flock -w 900 9 || { echo "gate: another gate run held the lock >15min — investigate (docker ps | grep pulse-gate)" >&2; exit 1; }
cd "$(git rev-parse --show-toplevel)"
IMG="${SHIZUHA_CI_IMAGE:-}"
if [ -z "$IMG" ] && command -v kubectl >/dev/null 2>&1; then
  IMG=$(kubectl get deploy -n shizuha shizuha-pulse -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || true)
fi
IMG="${IMG:-localhost:30500/shizuha-pulse:shizuha-tasks-a0e57e5}"  # pinned fallback
NET="pulse-gate-$$"; PG="pulse-gate-pg-$$"; RD="pulse-gate-redis-$$"; APP="pulse-gate-app-$$"
cleanup() {
  # A forced stop leaves the large cached cluster replaying WAL/fsyncing for
  # longer than the suite itself on the next push. Ask PostgreSQL to finish a
  # fast, clean shutdown; the long docker-stop fallback is only for a wedged DB.
  docker exec -u postgres "$PG" pg_ctl -D /var/lib/postgresql/data -m fast -w stop >/dev/null 2>&1 \
    || docker stop -t 120 "$PG" >/dev/null 2>&1 \
    || true
  docker rm -f "$APP" "$PG" "$RD" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
}
trap cleanup EXIT
MODE=live
if ! docker image inspect "$IMG" >/dev/null 2>&1 && ! docker pull "$IMG" >/dev/null 2>&1; then
  MODE=scratch
  IMG="${SHIZUHA_CI_FALLBACK_IMAGE:-python:3.12-bookworm}"
  echo "gate: live Pulse image unavailable to Docker; using frozen-lock scratch fallback $IMG" >&2
fi
docker volume create pulse-gate-pgdata >/dev/null
docker network create "$NET" >/dev/null
docker run -d --name "$PG" --network "$NET" -p 127.0.0.1::5432 -v pulse-gate-pgdata:/var/lib/postgresql/data -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=shizuha_pulse_ci postgres:16-alpine \
  -c fsync=off -c synchronous_commit=off -c full_page_writes=off -c autovacuum=off >/dev/null
docker run -d --name "$RD" --network "$NET" -p 127.0.0.1::6379 redis:7-alpine >/dev/null
wait_pg() {
  for _i in $(seq 1 90); do
    docker exec "$PG" psql -U postgres -h 127.0.0.1 -c "SELECT 1" >/dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}
if ! wait_pg; then
  # Cached volume wedged (interrupted WAL recovery etc.) — it is only a cache:
  # recreate it fresh and retry once (next run repays migrations, then warm again).
  echo "gate: postgres not ready — recreating cache volume pulse-gate-pgdata and retrying" >&2
  docker rm -f "$PG" >/dev/null 2>&1
  docker volume rm pulse-gate-pgdata >/dev/null 2>&1
  docker volume create pulse-gate-pgdata >/dev/null
  docker run -d --name "$PG" --network "$NET" -p 127.0.0.1::5432 -v pulse-gate-pgdata:/var/lib/postgresql/data -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=shizuha_pulse_ci postgres:16-alpine \
    -c fsync=off -c synchronous_commit=off -c full_page_writes=off -c autovacuum=off >/dev/null
  wait_pg || { echo "gate: postgres STILL not ready — aborting" >&2; exit 1; }
fi
echo "gate: image=$IMG mode=$MODE (copying source into a throwaway stopped container)"
echo "gate: test workers=$TEST_JOBS"
echo "gate: test target=$TEST_TARGET"
# Publish each test-owned datastore on a random port bound ONLY to the DinD
# daemon's loopback, then share that network namespace with the app container.
# The Django process therefore sees exactly the same loopback-only topology as
# Origin CI. Do not permit the private bridge IPs through the hermetic socket
# guard: that weakens the contract and makes unrelated non-loopback sockets look
# local. The scratch regression receives the Redis bridge IP only as a negative
# control: the guard must reject it.
PG_PORT="$(docker port "$PG" 5432/tcp | awk -F: '$1 == "127.0.0.1" {print $NF; exit}')"
RD_PORT="$(docker port "$RD" 6379/tcp | awk -F: '$1 == "127.0.0.1" {print $NF; exit}')"
[[ "$PG_PORT" =~ ^[1-9][0-9]*$ ]] || { echo "gate: invalid loopback PostgreSQL port: $PG_PORT" >&2; exit 1; }
[[ "$RD_PORT" =~ ^[1-9][0-9]*$ ]] || { echo "gate: invalid loopback Redis port: $RD_PORT" >&2; exit 1; }
RD_IP="$(docker inspect -f '{{range.NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$RD")"
COMMON_ENV=(
  -e POSTGRES_HOST=127.0.0.1 -e POSTGRES_PORT="$PG_PORT" -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=shizuha_pulse_ci \
  -e REDIS_URL=redis://127.0.0.1:$RD_PORT/5 -e CACHE_REDIS_URL=redis://127.0.0.1:$RD_PORT/6 -e CELERY_BROKER_URL=redis://127.0.0.1:$RD_PORT/7 -e CELERY_RESULT_BACKEND=redis://127.0.0.1:$RD_PORT/7 -e SECRET_KEY=ci \
  -e PULSE_SUBSTRATE_CAP_MODE=off \
)
if [ "$MODE" = live ]; then
  # docker cp merges into the image tree and does NOT delete files that exist
  # only in the image (e.g. a test module removed from git still lives in the
  # deployed Pulse image). That gates new checkouts on stale tests. Build a
  # local test-file allowlist and drop any image-only test_*.py before running.
  LOCAL_TEST_LIST=$(mktemp)
  LOCAL_MIGRATION_LIST=$(mktemp)
  ls -1 "$PWD"/tasks/tests/test_*.py 2>/dev/null | xargs -n1 basename >"$LOCAL_TEST_LIST" || true
  ls -1 "$PWD"/tasks/migrations/0*.py 2>/dev/null | xargs -n1 basename >"$LOCAL_MIGRATION_LIST" || true
  docker create --name "$APP" --network host "${COMMON_ENV[@]}" \
    --entrypoint sh "$IMG" -c \
    "tar -xf /tmp/local-source.tar -C /app; \
     uv pip install -q --python /app/.venv/bin/python tblib 2>/dev/null || pip install -q tblib 2>/dev/null; \
     if [ -f /tmp/local-test-list ]; then \
       for f in /app/tasks/tests/test_*.py; do \
         [ -f \"\$f\" ] || continue; \
         base=\$(basename \"\$f\"); \
         grep -qxF \"\$base\" /tmp/local-test-list || rm -f \"\$f\"; \
       done; \
     fi; \
     if [ -f /tmp/local-migration-list ]; then \
       for f in /app/tasks/migrations/0*.py; do \
         [ -f \"\$f\" ] || continue; \
         base=\$(basename \"\$f\"); \
         grep -qxF \"\$base\" /tmp/local-migration-list || rm -f \"\$f\"; \
       done; \
     fi; \
     python manage.py check --settings=taskmanager.test_settings; \
     python manage.py makemigrations --check --dry-run --settings=taskmanager.test_settings; \
     python manage.py test '$TEST_TARGET' --settings=taskmanager.test_settings --noinput --parallel '$TEST_JOBS'" \
    >/dev/null
  LOCAL_SOURCE_ARCHIVE=$(mktemp)
  tar \
    --exclude=.git \
    --exclude=.venv \
    --exclude=frontend/node_modules \
    --exclude=frontend/playwright-report \
    --exclude=frontend/test-results \
    --exclude='*/__pycache__' \
    --exclude=.pytest_cache \
    -cf "$LOCAL_SOURCE_ARCHIVE" .
  docker cp "$LOCAL_SOURCE_ARCHIVE" "$APP:/tmp/local-source.tar"
  docker cp "$LOCAL_TEST_LIST" "$APP:/tmp/local-test-list"
  docker cp "$LOCAL_MIGRATION_LIST" "$APP:/tmp/local-migration-list"
  rm -f "$LOCAL_SOURCE_ARCHIVE" "$LOCAL_TEST_LIST" "$LOCAL_MIGRATION_LIST"
  # Gate-contract tests inspect this file from the source root. Keep the
  # tested bytes aligned with the checkout instead of reading the stale copy
  # baked into the currently deployed image.
  docker cp "$PWD/.ci-local-gate.sh" "$APP:/app/.ci-local-gate.sh"
else
  docker create --name "$APP" --network host "${COMMON_ENV[@]}" \
    -e SHIZUHA_SCRATCH_GATE=1 -e SHIZUHA_SCRATCH_GATE_REDIS_SIDECAR_HOST="$RD_IP" \
    -e DEBUG=True -e JWT_SECRET_KEY=ci -e SHIZUHA_JWT_SIGNING_KEY=ci -e MALLOC_ARENA_MAX=2 \
    "$IMG" bash -lc \
    "set -euo pipefail
     cd /workspace
     timeout 180 python -m pip install --disable-pip-version-check --no-cache-dir 'uv==0.11.26'
     timeout 600 uv sync --frozen --no-dev --no-install-project --no-editable
     export PATH=/workspace/.venv/bin:\$PATH
     uv pip install -q --python /workspace/.venv/bin/python --no-deps 'tblib==1.7.0'
     python scripts/check_hermetic_settings.py --self-test
     python scripts/check_hermetic_settings.py --settings-module taskmanager.test_settings --workflow .forgejo/workflows/deploy.yml
     python manage.py check --settings=taskmanager.test_settings
     python manage.py makemigrations --check --dry-run --settings=taskmanager.test_settings
     timeout --signal=TERM --kill-after=30 1800 python manage.py test '$TEST_TARGET' --settings=taskmanager.test_settings --noinput --parallel '$TEST_JOBS'" \
    >/dev/null
  # `docker cp` streams through the Docker API, so it works when the daemon is
  # a sidecar/DinD container that cannot see the agent's workspace filesystem.
  docker cp "$PWD/." "$APP:/workspace"
fi
docker start -a "$APP"
