import json
import os
from pathlib import Path
import re
import subprocess
import textwrap


WORKFLOW = Path(__file__).resolve().parents[1] / ".forgejo" / "workflows" / "deploy-frontend.yml"


def _workflow_text() -> str:
    return WORKFLOW.read_text()


def _inline_python_script(name: str) -> str:
    text = _workflow_text()
    marker = f"cat > /tmp/{name} <<'PY'\n"
    start = text.index(marker) + len(marker)
    end = text.index("\n          PY", start)
    return textwrap.dedent(text[start:end])


def _convergence_shell_function() -> str:
    text = _workflow_text()
    begin = "# PLS-457-CONVERGENCE-FUNCTION-BEGIN\n"
    end = "# PLS-457-CONVERGENCE-FUNCTION-END"
    start = text.index(begin) + len(begin)
    return textwrap.dedent(text[start:text.index(end, start)])


def _pod(name: str, ip: str, tag: str) -> dict:
    return {
        "metadata": {"name": name},
        "spec": {"containers": [{"image": f"localhost:30500/shizuha-pulse-frontend:{tag}"}]},
        "status": {
            "phase": "Running",
            "podIP": ip,
            "conditions": [{"type": "Ready", "status": "True"}],
        },
    }


def _endpoint_slice(*addresses: str) -> dict:
    return {
        "items": [
            {
                "endpoints": [
                    {"addresses": [address], "conditions": {"ready": True}}
                    for address in addresses
                ]
            }
        ]
    }


def _run_endpoint_convergence(
    tmp_path: Path,
    pods: list[dict],
    slices: dict,
    *,
    expected_replicas: int = 2,
):
    script = tmp_path / "pulse_frontend_endpoint_convergence.py"
    pods_path = tmp_path / "pods.json"
    slices_path = tmp_path / "slices.json"
    script.write_text(_inline_python_script("pulse_frontend_endpoint_convergence.py"))
    pods_path.write_text(json.dumps({"items": pods}))
    slices_path.write_text(json.dumps(slices))
    return subprocess.run(
        [
            "python3",
            str(script),
            "pulse-frontend-deadbee",
            str(expected_replicas),
            str(pods_path),
            str(slices_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def test_deploy_repo_update_precedes_lifecycle_convergence_wait():
    text = _workflow_text()
    update_idx = text.index('update_deploy_values_frontend_tag "${FTAG}"')
    assert_idx = text.index('assert_deploy_values_frontend_tag "${FTAG}"')
    wait_idx = text.index("for attempt in $(seq 1 540)", assert_idx)
    assert update_idx < assert_idx < wait_idx


def test_frontend_deploy_verifies_chart_helm_and_both_ready_workloads():
    text = _workflow_text()
    assert 'CHART_BACKEND_TAG="$(python3 .forgejo/scripts/deploy-values-path.py get' in text
    assert 'CHART_FRONTEND_TAG="$(python3 .forgejo/scripts/deploy-values-path.py get' in text
    assert 'STORED_BACKEND_TAG="$(printf' in text
    assert 'STORED_FRONTEND_TAG="$(printf' in text
    assert 'LIVE_BACKEND="$(kubectl get deploy' in text
    assert '[ "${READY}" = "${DESIRED}" ]' in text
    assert '[ "${BACKEND_READY}" = "${BACKEND_DESIRED}" ]' in text


def test_frontend_deploy_rejects_mixed_frontend_or_backend_revisions():
    text = _workflow_text()
    assert "exact-deployment-pods.py" in text
    assert "      - '.forgejo/scripts/exact-deployment-pods.py'" in text
    assert "      - '.forgejo/scripts/test_exact_deployment_pods.py'" in text
    assert (
        'install -m 0644 "${SRC_DIR}/.forgejo/scripts/exact-deployment-pods.py"'
        " .forgejo/scripts/exact-deployment-pods.py"
    ) in text
    assert "python3 .forgejo/scripts/test_exact_deployment_pods.py" in text
    assert '[ "${EXACT_PODS}" = "${DESIRED}" ]' in text
    assert '[ "${EXACT_READY_PODS}" = "${DESIRED}" ]' in text
    assert '[ "${OLD_PODS}" = 0 ]' in text
    assert '[ "${FOREIGN_PODS}" = 0 ]' in text
    assert '[ "${EXACT_RESTARTS}" = 0 ]' in text
    assert '[ "${BACKEND_EXACT_PODS}" = "${BACKEND_DESIRED}" ]' in text
    assert '[ "${BACKEND_EXACT_READY_PODS}" = "${BACKEND_DESIRED}" ]' in text
    assert '[ "${BACKEND_OLD_PODS}" = 0 ]' in text
    assert '[ "${BACKEND_FOREIGN_PODS}" = 0 ]' in text
    assert '[ "${BACKEND_EXACT_RESTARTS}" = 0 ]' in text


def test_frontend_superseded_waiter_exits_before_live_convergence_checks():
    text = _workflow_text()
    wait_idx = text.index("for attempt in $(seq 1 540)")
    desired_idx = text.index(
        'CHART_FRONTEND_TAG="$(python3 .forgejo/scripts/deploy-values-path.py get',
        wait_idx,
    )
    superseded_idx = text.index(
        'Pulse frontend desired state advanced to ${CHART_FRONTEND_TAG}; '
        'this run is superseded',
        desired_idx,
    )
    live_idx = text.index('LIVE="$(kubectl get deploy', superseded_idx)
    assert wait_idx < desired_idx < superseded_idx < live_idx
    assert (
        '[ -n "${CHART_FRONTEND_TAG}" ] '
        '&& [ "${CHART_FRONTEND_TAG}" != "${FTAG}" ]'
    ) in text[desired_idx:live_idx]
    assert "exit 0" in text[superseded_idx:live_idx]


def test_existing_frontend_tag_dispatches_central_reconciliation():
    text = _workflow_text()
    assert "PROMOTED=false" in text
    assert "PROMOTED=true" in text
    assert "actions/workflows/deploy-id-mail-lifecycle.yml/dispatches" in text
    assert "central lifecycle reconcile dispatched for existing desired tag" in text


def test_frontend_has_cas_promotion_and_no_imperative_release_writer():
    text = _workflow_text()
    assert "for write_attempt in 1 2 3 4 5" in text
    assert "deploy values changed concurrently; retrying fresh read" in text
    assert "kubectl set image" not in text
    assert "kubectl set env" not in text
    assert "helm upgrade shizuha-pulse" not in text
    assert "[ci skip]" not in text


def test_frontend_stages_use_the_dedicated_deploy_lane():
    text = _workflow_text()
    assert text.count("runs-on: deploy-lane") == 2


def test_frontend_build_manifest_verify_and_smoke_jobs_use_short_cleanup_ttl():
    text = _workflow_text()
    for name in (
        r"\$\{JOB\}",
        r"ci-build-pulse-frontend-\$\{SHORT\}-\$\{ARCH\}",
        r"ci-manifest-pulse-frontend-\$\{SHORT\}",
    ):
        assert re.search(
            rf"name: {name}[\s\S]{{0,500}}?ttlSecondsAfterFinished: 300",
            text,
        )
    verify = text[text.index('local JOB="ci-verify-${REPO}-${SHORT}"') :]
    assert "ttlSecondsAfterFinished: 300" in verify
    smoke = text[text.index("render_smoke_job()") :]
    assert "labels: { app: ci-smoke, service: pulse-frontend }" in smoke
    assert "ttlSecondsAfterFinished: 300" in smoke
    assert text.count("image: gcr.io/kaniko-project/executor:v1.23.2") == 2
    assert text.count("- --cleanup") == 2


def test_frontend_multiarch_waits_run_concurrently_and_aggregate_failures():
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


def test_frontend_materializes_both_workflows_for_cross_release_contract():
    text = _workflow_text()
    assert '"${SRC_DIR}/.forgejo/workflows/deploy.yml" .forgejo/workflows/deploy.yml' in text
    assert (
        '"${SRC_DIR}/.forgejo/workflows/deploy-frontend.yml" '
        '.forgejo/workflows/deploy-frontend.yml'
    ) in text
    assert "runs-on: ubuntu-latest" not in text


def test_build_and_deploy_materializes_exact_repo_local_promotion_helpers():
    text = _workflow_text()
    build = text[text.index("  build-and-deploy:") :]
    assert "uses: actions/checkout" not in build
    materialize_idx = build.index("- name: Materialize exact promotion helpers")
    assert "GIT_TOKEN='${{ secrets.CI_PACKAGE_TOKEN }}'" in build
    assert "kubectl get secret -n build forgejo-token" not in build
    assert "fetch --depth=1 origin '${{ github.sha }}'" in build
    assert 'rev-parse HEAD)\" = \'${{ github.sha }}\'' in build
    assert 'install -m 0644 "${SRC_DIR}/.forgejo/scripts/deploy-values-path.py"' in build
    assert 'install -m 0644 "${SRC_DIR}/.forgejo/workflows/deploy-frontend.yml"' in build
    verifier_idx = build.index("python3 .forgejo/scripts/test_deploy_values_path.py")
    promotion_idx = build.index("python3 .forgejo/scripts/deploy-values-path.py get")
    assert materialize_idx < verifier_idx < promotion_idx


def test_deploy_repo_update_is_path_aware_and_retries_cas_conflicts():
    text = _workflow_text()
    start = text.index("update_deploy_values_frontend_tag()")
    end = text.index("assert_deploy_values_frontend_tag()", start)
    update = text[start:end]

    assert "for write_attempt in 1 2 3 4 5" in update
    assert "deploy repo already desires ${IMAGE_TAG}" in update
    assert "deploy values changed concurrently; retrying fresh read" in update
    assert "--accept-codes '^(200|201|409)$' PUT" in update
    assert "deploy values changed concurrently on all five write attempts" in update
    assert "deploy-values-path.py get /tmp/values.yaml frontend.image.tag" in update
    assert "deploy-values-path.py set /tmp/values.yaml frontend.image.tag" in update
    assert 'open("/tmp/values.yaml","wb").write(base64.b64decode' in update
    assert "desired frontend tag ${IMAGE_TAG} before rollout [ci skip]" not in update
    assert "pattern = r'(?ms)" not in update


def test_no_post_smoke_auto_update_path_remains():
    text = _workflow_text()
    service_smoke_idx = text.index('run_smoke_job "ci-smoke-pulse-frontend-${SHORT}"')
    tail = text[service_smoke_idx:]
    assert 'auto-update frontend tag' not in tail
    assert 'desired frontend tag ${IMAGE_TAG} before rollout' in text
    assert 'Verified canonical chart, Helm values, and live frontend image' in tail


def test_pod_first_endpoint_then_service_smoke_order_is_preserved():
    text = _workflow_text()
    pod_smoke = text.index('ci-smoke-pulse-frontend-pods-${SHORT}')
    endpoint_wait = text.index('waiting for shizuha-pulse-frontend EndpointSlices')
    service_smoke = text.index('ci-smoke-pulse-frontend-${SHORT}', pod_smoke + 1)
    final_verify = text.index(
        'wait_for_frontend_endpoint_convergence "final desired-state verification"'
    )
    assert pod_smoke < endpoint_wait < service_smoke < final_verify


def test_endpoint_convergence_recomputes_current_build_across_replacement_churn(tmp_path):
    tag = "pulse-frontend-deadbee"

    before_replacement = _run_endpoint_convergence(
        tmp_path,
        [_pod("frontend-a", "10.42.4.127", tag), _pod("frontend-b", "10.42.8.224", tag)],
        _endpoint_slice("10.42.4.127", "10.42.7.24"),
    )
    assert before_replacement.returncode != 0

    after_replacement = _run_endpoint_convergence(
        tmp_path,
        [_pod("frontend-a", "10.42.4.127", tag), _pod("frontend-c", "10.42.7.24", tag)],
        _endpoint_slice("10.42.4.127", "10.42.7.24"),
    )
    assert after_replacement.returncode == 0, after_replacement.stdout + after_replacement.stderr

    convergence = _convergence_shell_function()
    assert "kubectl get pods" in convergence
    assert "kubectl get endpointslice" in convergence


def test_endpoint_convergence_rejects_old_build_and_underreplicated_sets(tmp_path):
    tag = "pulse-frontend-deadbee"
    current = [_pod("frontend-a", "10.42.4.127", tag), _pod("frontend-c", "10.42.7.24", tag)]

    stale_old_build_endpoint = _run_endpoint_convergence(
        tmp_path,
        current,
        _endpoint_slice("10.42.4.127", "10.42.9.99"),
    )
    assert stale_old_build_endpoint.returncode != 0

    equal_but_underreplicated = _run_endpoint_convergence(
        tmp_path,
        current[:1],
        _endpoint_slice("10.42.4.127"),
    )
    assert equal_but_underreplicated.returncode != 0


def test_endpoint_convergence_rereads_desired_replicas_across_hpa_scale_up(tmp_path):
    tag = "pulse-frontend-deadbee"
    scaled = [
        _pod("frontend-a", "10.42.4.127", tag),
        _pod("frontend-b", "10.42.7.24", tag),
        _pod("frontend-c", "10.42.8.224", tag),
    ]
    endpoints = _endpoint_slice("10.42.4.127", "10.42.7.24", "10.42.8.224")

    frozen_before_scale = _run_endpoint_convergence(
        tmp_path,
        scaled,
        endpoints,
        expected_replicas=2,
    )
    assert frozen_before_scale.returncode != 0

    reread_after_scale = _run_endpoint_convergence(
        tmp_path,
        scaled,
        endpoints,
        expected_replicas=3,
    )
    assert reread_after_scale.returncode == 0, reread_after_scale.stdout + reread_after_scale.stderr

    workflow = _workflow_text()
    convergence = _convergence_shell_function()
    desired_read = "kubectl get deploy -n shizuha shizuha-pulse-frontend -o jsonpath='{.spec.replicas}'"
    assert desired_read in convergence

    first_check = workflow.index(
        'wait_for_frontend_endpoint_convergence "service endpoints" 60'
    )
    service_smoke = workflow.index(
        'run_smoke_job "ci-smoke-pulse-frontend-${SHORT}"',
        first_check,
    )
    final_check = workflow.index(
        'wait_for_frontend_endpoint_convergence "final desired-state verification" 60'
    )
    assert first_check < service_smoke < final_check


def test_final_verification_retries_hpa_churn_after_service_smoke(tmp_path):
    """Execute the workflow function across pass → churn reject → converge."""
    tag = "pulse-frontend-deadbee"
    state_dir = tmp_path / "state"
    fake_bin = tmp_path / "bin"
    state_dir.mkdir()
    fake_bin.mkdir()
    (state_dir / "counter").write_text("0")

    states = [
        (
            2,
            [_pod("frontend-a", "10.42.4.127", tag), _pod("frontend-b", "10.42.7.24", tag)],
            _endpoint_slice("10.42.4.127", "10.42.7.24"),
        ),
        (
            3,
            [_pod("frontend-a", "10.42.4.127", tag), _pod("frontend-b", "10.42.7.24", tag)],
            _endpoint_slice("10.42.4.127", "10.42.7.24"),
        ),
        (
            3,
            [
                _pod("frontend-a", "10.42.4.127", tag),
                _pod("frontend-b", "10.42.7.24", tag),
                _pod("frontend-c", "10.42.8.224", tag),
            ],
            _endpoint_slice("10.42.4.127", "10.42.7.24", "10.42.8.224"),
        ),
    ]
    for index, (desired, pods, slices) in enumerate(states):
        (state_dir / f"desired-{index}").write_text(str(desired))
        (state_dir / f"pods-{index}.json").write_text(json.dumps({"items": pods}))
        (state_dir / f"slices-{index}.json").write_text(json.dumps(slices))

    kubectl = fake_bin / "kubectl"
    kubectl.write_text(textwrap.dedent("""\
        #!/bin/sh
        set -eu
        index="$(cat "${STATE_DIR}/counter")"
        case "$1:$2" in
          get:deploy)
            cat "${STATE_DIR}/desired-${index}"
            ;;
          get:pods)
            cat "${STATE_DIR}/pods-${index}.json"
            ;;
          get:endpointslice)
            cat "${STATE_DIR}/slices-${index}.json"
            case "$*" in
              *"-o json"*) echo $((index + 1)) > "${STATE_DIR}/counter" ;;
            esac
            ;;
          *)
            echo "unexpected kubectl invocation: $*" >&2
            exit 99
            ;;
        esac
    """))
    kubectl.chmod(0o755)

    helper = tmp_path / "pulse_frontend_endpoint_convergence.py"
    helper.write_text(_inline_python_script("pulse_frontend_endpoint_convergence.py"))
    current_pods = tmp_path / "current-pods.json"
    current_slices = tmp_path / "current-slices.json"
    function = _convergence_shell_function().replace(
        "/tmp/pulse_frontend_endpoint_convergence.py", str(helper),
    ).replace(
        "/tmp/pulse_frontend_current_pods.json", str(current_pods),
    ).replace(
        "/tmp/pulse_frontend_current_slices.json", str(current_slices),
    )
    shell = textwrap.dedent(f"""\
        set -euo pipefail
        FTAG={tag}
        {function}
        sleep() {{ :; }}
        wait_for_frontend_endpoint_convergence "service endpoints" 3
        echo service-smoke
        wait_for_frontend_endpoint_convergence "final desired-state verification" 3
    """)
    result = subprocess.run(
        ["bash", "-c", shell],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "STATE_DIR": str(state_dir),
        },
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert (state_dir / "counter").read_text().strip() == "3"
    assert "waiting for final desired-state verification convergence (attempt 1/3)" in output
    first = output.index("service endpoints converged")
    smoke = output.index("service-smoke")
    final = output.index("final desired-state verification converged")
    assert first < smoke < final


def test_frontend_deploy_rechecks_canonical_values_after_smoke():
    text = _workflow_text()
    smoke_idx = text.index('run_smoke_job "ci-smoke-pulse-frontend-${SHORT}"')
    tail = text[smoke_idx:]
    final_assert_idx = tail.index('assert_deploy_values_frontend_tag "${FTAG}"')
    live_check_idx = tail.index('frontend image mismatch after smoke')
    final_log_idx = tail.index('Verified canonical chart, Helm values, and live frontend image')
    assert final_assert_idx < live_check_idx < final_log_idx
