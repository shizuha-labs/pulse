from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("deploy-values-path.py")
ROOT = SCRIPT.parents[2]
SPEC = importlib.util.spec_from_file_location("deploy_values_path", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class DeployValuesPathTests(unittest.TestCase):
    def test_lifecycle_is_the_only_pulse_release_writer(self) -> None:
        backend = (ROOT / ".forgejo/workflows/deploy.yml").read_text()
        frontend = (ROOT / ".forgejo/workflows/deploy-frontend.yml").read_text()
        self.assertIn(
            'deploy-values-path.py set /tmp/values.yaml image.tag "${IMAGE_TAG}"',
            backend,
        )
        self.assertIn(
            'deploy-values-path.py set /tmp/values.yaml frontend.image.tag "${IMAGE_TAG}"',
            frontend,
        )
        self.assertIn("desired backend tag ${IMAGE_TAG} before rollout", backend)
        self.assertIn("desired frontend tag ${IMAGE_TAG} before rollout", frontend)
        self.assertIn("central lifecycle did not converge Pulse release", backend)
        self.assertIn("central lifecycle did not converge Pulse frontend", frontend)
        self.assertIn("for attempt in $(seq 1 540)", backend)
        self.assertIn("for attempt in $(seq 1 540)", frontend)
        self.assertIn('[ "${OBSERVED}" -ge "${GENERATION}" ]', backend)
        self.assertIn('[ "${FRONTEND_OBSERVED}" -ge "${FRONTEND_GENERATION}" ]', backend)
        self.assertIn('[ "${UPDATED}" = "${DESIRED}" ]', backend)
        self.assertIn('[ "${AVAILABLE}" = "${DESIRED}" ]', backend)
        self.assertIn('[ "${FRONTEND_UPDATED}" = "${FRONTEND_DESIRED}" ]', backend)
        self.assertIn('[ "${FRONTEND_AVAILABLE}" = "${FRONTEND_DESIRED}" ]', backend)
        self.assertIn('[ "${EXACT_PODS}" = "${DESIRED}" ]', backend)
        self.assertIn('[ "${EXACT_READY_PODS}" = "${DESIRED}" ]', backend)
        self.assertIn('[ "${OLD_PODS}" = 0 ]', backend)
        self.assertIn('[ "${EXACT_RESTARTS}" = 0 ]', backend)
        self.assertIn(
            '[ "${FRONTEND_EXACT_PODS}" = "${FRONTEND_DESIRED}" ]',
            backend,
        )
        self.assertIn('[ "${OBSERVED}" -ge "${GENERATION}" ]', frontend)
        self.assertIn('[ "${BACKEND_OBSERVED}" -ge "${BACKEND_GENERATION}" ]', frontend)
        self.assertIn('[ "${UPDATED}" = "${DESIRED}" ]', frontend)
        self.assertIn('[ "${AVAILABLE}" = "${DESIRED}" ]', frontend)
        self.assertIn('[ "${BACKEND_UPDATED}" = "${BACKEND_DESIRED}" ]', frontend)
        self.assertIn('[ "${BACKEND_AVAILABLE}" = "${BACKEND_DESIRED}" ]', frontend)
        self.assertIn('[ "${EXACT_PODS}" = "${DESIRED}" ]', frontend)
        self.assertIn('[ "${EXACT_READY_PODS}" = "${DESIRED}" ]', frontend)
        self.assertIn('[ "${OLD_PODS}" = 0 ]', frontend)
        self.assertIn('[ "${EXACT_RESTARTS}" = 0 ]', frontend)
        self.assertIn(
            '[ "${BACKEND_EXACT_PODS}" = "${BACKEND_DESIRED}" ]',
            frontend,
        )
        self.assertIn('STORED_FRONTEND_TAG="$(printf', backend)
        self.assertIn('STORED_BACKEND_TAG="$(printf', frontend)
        self.assertNotIn("helm upgrade shizuha-pulse", backend)
        self.assertNotIn("helm upgrade shizuha-pulse", frontend)
        self.assertNotIn("kubectl set image", backend)
        self.assertNotIn("kubectl set image", frontend)
        self.assertNotIn("kubectl set env", backend)
        self.assertNotIn("kubectl set env", frontend)
        self.assertNotIn("[ci skip]", backend)
        self.assertNotIn("[ci skip]", frontend)

    def test_split_workflows_use_path_aware_cas_and_redispatch_contract(self) -> None:
        backend = (ROOT / ".forgejo/workflows/deploy.yml").read_text()
        frontend = (ROOT / ".forgejo/workflows/deploy-frontend.yml").read_text()
        dispatch_path = "actions/workflows/deploy-id-mail-lifecycle.yml/dispatches"
        self.assertIn("for write_attempt in 1 2 3 4 5", backend)
        self.assertIn("for write_attempt in 1 2 3 4 5", frontend)
        self.assertIn("deploy values changed concurrently; retrying fresh read", backend)
        self.assertIn("deploy values changed concurrently; retrying fresh read", frontend)
        self.assertIn("PROMOTED=false", backend)
        self.assertIn("PROMOTED=true", backend)
        self.assertIn(dispatch_path, backend)
        self.assertIn("PROMOTED=false", frontend)
        self.assertIn("PROMOTED=true", frontend)
        self.assertIn(dispatch_path, frontend)
        self.assertIn('CURRENT_BACKEND_TAG="$(python3 .forgejo/scripts/deploy-values-path.py get', backend)
        self.assertIn('CURRENT_FRONTEND_TAG="$(python3 .forgejo/scripts/deploy-values-path.py get', frontend)
        self.assertNotIn("print(base64.b64decode", frontend)

    def test_backend_refreshes_sibling_frontend_expectation_inside_wait(self) -> None:
        backend = (ROOT / ".forgejo/workflows/deploy.yml").read_text()
        wait = backend.index("for attempt in $(seq 1 540)")
        sibling = backend.index(
            'CHART_FRONTEND_TAG="$(python3 .forgejo/scripts/deploy-values-path.py get',
            wait,
        )
        live = backend.index('LIVE_FRONTEND="$(kubectl get deploy', sibling)
        self.assertLess(wait, sibling)
        self.assertLess(sibling, live)

    def test_updates_only_the_requested_nested_tag(self) -> None:
        source = (
            "image:\n"
            "  tag: pulse-old\n"
            "frontend:\n"
            "  image:\n"
            "    repository: localhost:30500/shizuha-pulse-frontend\n"
            "    tag: pulse-frontend-old\n"
            "relay:\n"
            "  image:\n"
            "    tag: relay-old\n"
        )
        lines = source.splitlines(keepends=True)
        index, match, old = MODULE.find_scalar(lines, ("frontend", "image", "tag"))
        self.assertEqual(old, "pulse-frontend-old")
        lines[index] = (
            f'{match.group("prefix")}{match.group("key")}:'
            f'{match.group("separator")}pulse-frontend-new{match.group("newline")}'
        )
        updated = "".join(lines)
        self.assertIn("  tag: pulse-old\n", updated)
        self.assertIn("    tag: pulse-frontend-new\n", updated)
        self.assertIn("    tag: relay-old\n", updated)

    def test_missing_path_fails_closed_instead_of_crossing_mappings(self) -> None:
        source = (
            "frontend:\n"
            "  enabled: true\n"
            "relay:\n"
            "  image:\n"
            "    tag: relay-old\n"
        )
        with self.assertRaisesRegex(SystemExit, "could not locate frontend.image.tag"):
            MODULE.find_scalar(source.splitlines(keepends=True), ("frontend", "image", "tag"))

    def test_cli_preserves_file_shape(self) -> None:
        source = "frontend:\n  image:\n    tag: old\n\nother: value\n"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "values.yaml"
            path.write_text(source)
            lines = path.read_text().splitlines(keepends=True)
            index, match, _ = MODULE.find_scalar(lines, ("frontend", "image", "tag"))
            lines[index] = (
                f'{match.group("prefix")}{match.group("key")}:'
                f'{match.group("separator")}new{match.group("newline")}'
            )
            path.write_text("".join(lines))
            self.assertEqual(path.read_text(), source.replace("    tag: old", "    tag: new"))


if __name__ == "__main__":
    unittest.main()
