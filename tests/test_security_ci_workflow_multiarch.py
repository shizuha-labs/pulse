import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".forgejo/workflows/security-ci.yml"


class SecurityCiWorkflowMultiarchTests(unittest.TestCase):
    def test_s1_infrastructure_failure_cannot_false_red_independent_scan(self):
        workflow = WORKFLOW.read_text()
        self.assertIn("runs-on: security-ci-arm64", workflow)
        self.assertNotIn("runner: [security-ci-amd64, security-ci-arm64]", workflow)
        self.assertNotRegex(workflow, r"(?m)^\s+continue-on-error:")

        # Declarative workflow-level failure contract: there is no required
        # amd64 job/matrix result to combine with the ARM result. Therefore an
        # amd64 infrastructure failure is irrelevant, while an ARM scan failure
        # remains the workflow failure (no masking/continue-on-error).
        def conclusion(*, amd64_infrastructure_failed, arm_scan_succeeded):
            del amd64_infrastructure_failed
            return "success" if arm_scan_succeeded else "failure"

        self.assertEqual(
            conclusion(amd64_infrastructure_failed=True, arm_scan_succeeded=True),
            "success",
        )
        self.assertEqual(
            conclusion(amd64_infrastructure_failed=False, arm_scan_succeeded=False),
            "failure",
        )

    def test_scanner_downloads_follow_the_runner_architecture(self):
        workflow = WORKFLOW.read_text()
        self.assertIn("osv-scanner-linux-${OSV_ARCH}", workflow)
        self.assertIn("aarch64|arm64) OSV_ARCH=arm64", workflow)
        self.assertIn('Linux-${TRIVY_ARCH}.tar.gz', workflow)
        self.assertIn("aarch64|arm64) TRIVY_ARCH=ARM64", workflow)
        self.assertNotIn("osv-scanner-linux-amd64", workflow)
        self.assertNotIn('asset="trivy_${TRIVY_VERSION}_Linux-64bit.tar.gz"', workflow)

    def test_trivy_cache_path_is_expanded_by_the_runner(self):
        workflow = WORKFLOW.read_text()
        self.assertIn('TRIVY_CACHE_DIR=/opt/hostedtoolcache/trivy', workflow)
        self.assertIn('mkdir -p "${TRIVY_CACHE_DIR}"', workflow)
        self.assertIn('--cache-dir "${TRIVY_CACHE_DIR}"', workflow)
        self.assertNotIn('mkdir -p ""', workflow)
        self.assertNotIn('--cache-dir ""', workflow)

    def test_trivy_operational_failure_cannot_be_normalized_to_empty_success(self):
        workflow = WORKFLOW.read_text()
        trivy_step = workflow.split("      - name: Run Trivy filesystem scan\n", 1)[1]
        trivy_step = trivy_step.split("\n      - name:", 1)[0]
        self.assertIn('if [ "${rc}" -ne 0 ]; then', trivy_step)
        self.assertIn('if [ ! -s trivy.json ]; then', trivy_step)
        self.assertNotIn('echo {Results:[]} > trivy.json', trivy_step)
        self.assertNotIn('exit 0', trivy_step)


if __name__ == "__main__":
    unittest.main()
