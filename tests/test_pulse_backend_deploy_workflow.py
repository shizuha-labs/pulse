import re
from pathlib import Path


WORKFLOW = Path(__file__).resolve().parents[1] / ".forgejo" / "workflows" / "deploy.yml"


def _workflow_text() -> str:
    return WORKFLOW.read_text()


def test_backend_test_orchestration_uses_the_dedicated_release_lane():
    text = _workflow_text()
    test_job = text[text.index("  test:") : text.index("  build:")]
    assert "runs-on: deploy-lane" in test_job
    assert "runs-on: ubuntu-latest" not in test_job
    assert "left\n    # this required gate unscheduled" in test_job


def test_backend_gate_matches_the_parallel_local_recipe():
    text = _workflow_text()
    test_job = text[text.index("  test:") : text.index("  build:")]
    assert "--python /workspace/src/.venv/bin/python --no-deps 'tblib==1.7.0'" in test_job
    assert "export TEST_JOBS=8" in test_job
    assert '--noinput --failfast --keepdb --parallel "\\${TEST_JOBS}"' in test_job


def test_green_backend_release_supersedes_obsolete_deploy_lane_owner():
    text = _workflow_text()
    deploy_job = text[text.index("  deploy:") :]
    assert "needs: [test, build]" in deploy_job
    assert "group: deploy-shizuha-pulse-build-${{ github.ref }}" in deploy_job
    assert "cancel-in-progress: true" in deploy_job
    assert "cancel-in-progress: false" not in deploy_job


def test_backend_build_removes_only_superseded_backend_jobs():
    text = _workflow_text()
    assert "shizuha.io/build-owner: pulse-backend" in text
    assert 'shizuha.io/source-sha: "${SHORT}"' in text
    assert 'shizuha.io/build-arch: "${ARCH}"' in text
    assert "activeDeadlineSeconds: 2100" in text
    assert (
        "^ci-build-shizuha-pulse-([0-9a-f]{7})-(amd64|arm64)$" in text
    )
    assert '[ "${BASH_REMATCH[1]}" != "${SHORT}" ]' in text
    assert 'kubectl delete job -n build "${JOB_NAME}" --wait=false' in text
    assert "ci-build-shizuha-pulse-frontend" not in text


def test_backend_build_manifest_and_verify_jobs_use_short_cleanup_ttl():
    text = _workflow_text()
    for name in (
        r"ci-build-shizuha-pulse-\$\{SHORT\}-\$\{ARCH\}",
        r"ci-manifest-shizuha-pulse-\$\{SHORT\}",
    ):
        assert re.search(
            rf"name: {name}[\s\S]{{0,1500}}?ttlSecondsAfterFinished: 300",
            text,
        )
    verify = text[text.index('local JOB="ci-verify-${REPO}-${SHORT}"') :]
    assert "ttlSecondsAfterFinished: 300" in verify
    assert text.count("image: gcr.io/kaniko-project/executor:v1.23.2") == 1
    assert text.count("- --cleanup") == 1


def test_backend_multiarch_waits_run_concurrently_and_aggregate_failures():
    text = _workflow_text()
    expected = (
        "wait_job amd64 &\n"
        "          AMD64_WAIT_PID=$!\n"
        "          wait_job arm64 &\n"
        "          ARM64_WAIT_PID=$!\n"
        "          WAIT_STATUS=0\n"
        '          wait "$AMD64_WAIT_PID" || WAIT_STATUS=$?\n'
        '          wait "$ARM64_WAIT_PID" || WAIT_STATUS=$?\n'
        '          [ "$WAIT_STATUS" -eq 0 ] || exit "$WAIT_STATUS"'
    )
    assert expected in text
    assert "wait_job amd64\n          wait_job arm64" not in text


def test_backend_build_wait_polls_complete_or_failed_and_handles_reaped_job():
    """PLAT-6970: the build wait_job must not use the broken `kubectl wait
    --for=condition=complete` (which NEVER returns on a FAILED job and hangs
    forever once ttlSecondsAfterFinished reaps it). It must poll Complete OR
    Failed, treat a reaped (NotFound) job as failure, and enforce a deadline."""
    text = _workflow_text()
    build_job = text[text.index("  build:") : text.index("  deploy:")]
    # The broken construct must be gone from the wait_job() function body.
    wait_fn = build_job[build_job.index("wait_job()") : build_job.index("wait_job amd64 &")]
    assert "kubectl wait" not in wait_fn
    # The proven Complete-OR-Failed poll loop is present.
    assert 'COMPLETE="$(kubectl get job -n build "${J}" -o jsonpath=\'{.status.conditions[?(@.type=="Complete")].status}\'' in build_job
    assert 'FAILED="$(kubectl get job -n build "${J}" -o jsonpath=\'{.status.conditions[?(@.type=="Failed")].status}\'' in build_job
    assert '[ "${COMPLETE}" = "True" ] && break' in build_job
    # A reaped (NotFound) job is treated as failure, not silently waited on.
    assert "was reaped (NotFound) before completion" in build_job
    assert "! kubectl get job -n build \"${J}\" >/dev/null 2>&1" in build_job
    # A hard deadline prevents an indefinite hang.
    assert "kaniko $1 build timed out after 1800s" in build_job
    # The concurrent wait structure is preserved.
    assert "wait_job amd64 &\n          AMD64_WAIT_PID=$!" in build_job


def test_deploy_repo_backend_update_precedes_lifecycle_convergence_wait():
    text = _workflow_text()
    update_idx = text.index('update_deploy_values_backend_tag "${TAG}"')
    assert_idx = text.index('assert_deploy_values_backend_tag "${TAG}"')
    wait_idx = text.index("for attempt in $(seq 1 540)", assert_idx)
    assert update_idx < assert_idx < wait_idx


def test_desired_tag_bump_checks_registry_manifest_before_put():
    text = _workflow_text()
    fn = text[text.index("update_deploy_values_backend_tag() {") : text.index("assert_deploy_values_backend_tag() {")]
    get_idx = fn.index("/v2/shizuha-pulse/manifests/${IMAGE_TAG}")
    put_idx = fn.index("forgejo_contents_curl --accept-codes '^(200|201|409)$' PUT")
    assert get_idx < put_idx
    assert "refusing to bump desired tag" in fn
    assert "require_multiarch_manifest.py" in fn
    script = open(".forgejo/scripts/require_multiarch_manifest.py", encoding="utf-8").read()
    assert "linux/amd64" in script and "linux/arm64" in script


def test_backend_values_update_is_fail_closed_not_warning_only():
    text = _workflow_text()
    update_idx = text.index('update_deploy_values_backend_tag "${TAG}"')
    wait_idx = text.index("for attempt in $(seq 1 540)", update_idx)
    pre_wait = text[update_idx:wait_idx]
    assert 'desired backend tag ${IMAGE_TAG} before rollout' in text
    assert 'WARNING: values.yaml update failed' not in pre_wait
    assert '|| echo' not in pre_wait


def test_backend_deploy_verifies_chart_helm_and_both_ready_workloads():
    text = _workflow_text()
    assert 'CHART_BACKEND_TAG="$(python3 .forgejo/scripts/deploy-values-path.py get' in text
    assert 'CHART_FRONTEND_TAG="$(python3 .forgejo/scripts/deploy-values-path.py get' in text
    assert 'STORED_BACKEND_TAG="$(printf' in text
    assert 'STORED_FRONTEND_TAG="$(printf' in text
    assert 'LIVE_FRONTEND="$(kubectl get deploy' in text
    assert '[ "${OBSERVED}" -ge "${GENERATION}" ]' in text
    assert '[ "${UPDATED}" = "${DESIRED}" ]' in text
    assert '[ "${AVAILABLE}" = "${DESIRED}" ]' in text
    assert '[ "${READY}" = "${DESIRED}" ]' in text
    assert '[ "${FRONTEND_OBSERVED}" -ge "${FRONTEND_GENERATION}" ]' in text
    assert '[ "${FRONTEND_UPDATED}" = "${FRONTEND_DESIRED}" ]' in text
    assert '[ "${FRONTEND_AVAILABLE}" = "${FRONTEND_DESIRED}" ]' in text
    assert '[ "${FRONTEND_READY}" = "${FRONTEND_DESIRED}" ]' in text


def test_backend_deploy_rejects_mixed_revision_aggregate_status():
    text = _workflow_text()
    assert "exact-deployment-pods.py" in text
    assert (
        'install -m 0644 "${SRC_DIR}/.forgejo/scripts/exact-deployment-pods.py"'
        " .forgejo/scripts/exact-deployment-pods.py"
    ) in text
    assert "python3 .forgejo/scripts/test_exact_deployment_pods.py" in text
    assert (
        "app.kubernetes.io/component=backend,"
        "app.kubernetes.io/instance=shizuha-pulse,"
        "app.kubernetes.io/name=shizuha-pulse"
    ) in text
    assert '[ "${EXACT_PODS}" = "${DESIRED}" ]' in text
    assert '[ "${EXACT_READY_PODS}" = "${DESIRED}" ]' in text
    assert '[ "${OLD_PODS}" = 0 ]' in text
    assert '[ "${FOREIGN_PODS}" = 0 ]' in text
    assert '[ "${EXACT_RESTARTS}" = 0 ]' in text
    assert '[ "${FRONTEND_EXACT_PODS}" = "${FRONTEND_DESIRED}" ]' in text
    assert '[ "${FRONTEND_EXACT_READY_PODS}" = "${FRONTEND_DESIRED}" ]' in text
    assert '[ "${FRONTEND_OLD_PODS}" = 0 ]' in text
    assert '[ "${FRONTEND_FOREIGN_PODS}" = 0 ]' in text
    assert '[ "${FRONTEND_EXACT_RESTARTS}" = 0 ]' in text


def test_backend_refreshes_frontend_tag_from_chart_inside_each_wait_iteration():
    text = _workflow_text()
    wait_idx = text.index("for attempt in $(seq 1 540)")
    sibling_idx = text.index(
        'CHART_FRONTEND_TAG="$(python3 .forgejo/scripts/deploy-values-path.py get',
        wait_idx,
    )
    live_idx = text.index('LIVE_FRONTEND="$(kubectl get deploy', sibling_idx)
    assert wait_idx < sibling_idx < live_idx


def test_backend_superseded_waiter_exits_before_live_convergence_checks():
    text = _workflow_text()
    wait_idx = text.index("for attempt in $(seq 1 540)")
    desired_idx = text.index(
        'CHART_BACKEND_TAG="$(python3 .forgejo/scripts/deploy-values-path.py get',
        wait_idx,
    )
    superseded_idx = text.index(
        'Pulse backend desired state advanced to ${CHART_BACKEND_TAG}; '
        'this run is superseded',
        desired_idx,
    )
    live_idx = text.index('LIVE="$(kubectl get deploy', superseded_idx)
    assert wait_idx < desired_idx < superseded_idx < live_idx
    assert (
        '[ -n "${CHART_BACKEND_TAG}" ] '
        '&& [ "${CHART_BACKEND_TAG}" != "${TAG}" ]'
    ) in text[desired_idx:live_idx]
    assert "exit 0" in text[superseded_idx:live_idx]


def test_existing_backend_tag_dispatches_central_reconciliation():
    text = _workflow_text()
    assert "PROMOTED=false" in text
    assert "PROMOTED=true" in text
    assert "actions/workflows/deploy-id-mail-lifecycle.yml/dispatches" in text
    assert "central lifecycle reconcile dispatched for existing desired tag" in text


def test_backend_has_cas_promotion_and_no_imperative_release_writer():
    text = _workflow_text()
    assert "for write_attempt in 1 2 3 4 5" in text
    assert "deploy values changed concurrently; retrying fresh read" in text
    assert "kubectl set image" not in text
    assert "kubectl set env" not in text
    assert "helm upgrade shizuha-pulse" not in text
    assert "[ci skip]" not in text
