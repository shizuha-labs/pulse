import importlib.util
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


vendor = load_script("security_ci_vendor", ROOT / "scripts/security-ci-vendor.py")
checker = load_script("security_ci_checker", ROOT / "scripts/check-security-ci-contract.py")
cascade = load_script("security_ci_cascade", ROOT / "scripts/security-ci-cascade.py")


class SecurityCiFleetTests(unittest.TestCase):
    def setUp(self):
        self.manifest = vendor.load_manifest()

    def test_frozen_inventory_is_complete_and_project_owned(self):
        expected = {
            "connect", "hive", "wiki", "id", "drive", "mail", "notes",
            "cortex", "search", "books", "finance", "inventory", "dojo",
            "admin", "home", "shizuha", "browser", "forge", "kura-beta",
            "kotlin", "deploy", "shizuha-tasks",
        }
        rows = self.manifest["consumers"]
        self.assertEqual({row["repo"] for row in rows}, expected)
        self.assertEqual(len(rows), 22)
        self.assertTrue(all(row["project_id"] > 0 for row in rows))
        self.assertTrue(all(row["default_branch"] in {"main", "master"} for row in rows))

    def test_every_generated_bundle_matches_manifest_and_contract(self):
        self.assertEqual(vendor.verify(self.manifest), [])
        for row in self.manifest["consumers"]:
            bundle = vendor.render_bundle(self.manifest, row["repo"])
            self.assertEqual(
                vendor.bundle_sha256(bundle),
                row["expected_bundle_sha256"],
                row["repo"],
            )
            with tempfile.TemporaryDirectory() as directory:
                workflow = Path(directory) / "security-ci.yml"
                workflow.write_bytes(bundle[".forgejo/workflows/security-ci.yml"])
                self.assertEqual(checker.validate(workflow), [], row["repo"])
                rendered = workflow.read_text()
                self.assertIn('mkdir -p "${TRIVY_CACHE_DIR}"', rendered, row["repo"])
                self.assertIn('--cache-dir "${TRIVY_CACHE_DIR}"', rendered, row["repo"])
                self.assertNotIn('mkdir -p ""', rendered, row["repo"])
                self.assertNotIn('--cache-dir ""', rendered, row["repo"])
            parity = bundle[".forgejo/workflows/security-ci-parity.yml"].decode()
            self.assertNotIn("__EXPECTED_WORKFLOW_SHA256__", parity)
            self.assertNotIn("__EXPECTED_BUNDLE_SHA256__", parity)
            self.assertIn(
                vendor.sha256(bundle[".forgejo/workflows/security-ci.yml"]),
                parity,
            )

    def test_consumer_bundle_hash_rejects_normalizer_and_checker_drift(self):
        bundle = vendor.render_bundle(self.manifest, "hive")
        parity = bundle[".forgejo/workflows/security-ci-parity.yml"].decode()
        inline = parity.split("python3 - <<'PY'\n", 1)[1].split("\n          PY", 1)[0]
        check = textwrap.dedent(inline)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for path, data in bundle.items():
                destination = root / path
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(data)

            pristine = subprocess.run(
                [sys.executable, "-c", check], cwd=root, capture_output=True, text=True
            )
            self.assertEqual(pristine.returncode, 0, pristine.stderr)

            for path in (
                "scripts/security-ci-to-pulse.py",
                "scripts/check-security-ci-contract.py",
            ):
                destination = root / path
                original = destination.read_bytes()
                destination.write_bytes(original + b"\n# unauthorized drift\n")
                drifted = subprocess.run(
                    [sys.executable, "-c", check], cwd=root, capture_output=True, text=True
                )
                self.assertNotEqual(drifted.returncode, 0, path)
                self.assertIn("bundle sha256 mismatch", drifted.stderr)
                destination.write_bytes(original)

    def test_timeout_hierarchy_keeps_job_kill_as_true_backstop(self):
        workflow = ROOT / ".forgejo/workflows/security-ci.yml"
        self.assertEqual(checker.validate(workflow), [])
        text = workflow.read_text().replace("timeout-minutes: 70", "timeout-minutes: 40")
        literal_budgets = [
            int(value)
            for value in re.findall(
                r"\btimeout\s+-k\s+\d+\s+(\d+)\b", text
            )
        ]
        # The old checker passed this 40-minute ceiling because it counted each
        # retry-wrapped command only once.  Prove the fixture specifically spans
        # that blind spot rather than merely choosing an obviously tiny ceiling.
        self.assertLessEqual(
            sum(literal_budgets) + checker.TIMEOUT_FINALIZATION_MARGIN_SECONDS,
            40 * 60,
        )
        with tempfile.TemporaryDirectory() as directory:
            inverted = Path(directory) / "security-ci.yml"
            inverted.write_text(text)
            errors = checker.validate(inverted)
            self.assertTrue(any("timeout hierarchy inverted" in error for error in errors))
            self.assertTrue(any("retry attempt budgets 1560s" in error for error in errors))
            self.assertTrue(any("retry backoff 60s" in error for error in errors))

    def test_lock_drift_step_runs_only_for_root_uv_projects(self):
        workflow = (ROOT / ".forgejo/workflows/security-ci.yml").read_text()
        lock_step = workflow.split(
            "      - name: Check Python dependency lock drift\n", 1
        )[1].split("\n      - name:", 1)[0]
        script = textwrap.dedent(lock_step.split("        run: |\n", 1)[1])

        fixtures = (
            ("project-present", True, "lock --check --no-progress"),
            ("project-absent", False, None),
        )
        for name, has_project, expected_call in fixtures:
            with self.subTest(fixture=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                bin_dir = root / "bin"
                bin_dir.mkdir()
                call_log = root / "uv-calls.log"
                fake_uv = bin_dir / "uv"
                fake_uv.write_text(
                    "#!/bin/sh\n"
                    'printf "%s\\n" "$*" >> "$UV_CALL_LOG"\n'
                )
                fake_uv.chmod(0o755)
                if has_project:
                    (root / "pyproject.toml").write_text("[project]\nname='fixture'\n")
                    (root / "uv.lock").write_text("version = 1\n")

                env = os.environ.copy()
                env.update(
                    {
                        "PATH": f"{bin_dir}:{env['PATH']}",
                        "RUNNER_TEMP": str(root / "runner-temp"),
                        "UV_CALL_LOG": str(call_log),
                    }
                )
                result = subprocess.run(
                    ["bash", "-c", script],
                    cwd=root,
                    env=env,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                calls = call_log.read_text().strip() if call_log.exists() else None
                self.assertEqual(calls, expected_call)
                if not has_project:
                    self.assertIn("Skipping uv lock drift check", result.stdout)

    def test_contract_rejects_an_unconditional_uv_lock_check(self):
        text = (ROOT / ".forgejo/workflows/security-ci.yml").read_text()
        text = text.replace(
            "          if [ -f pyproject.toml ] && [ -f uv.lock ]; then\n",
            "",
            1,
        ).replace(
            "          else\n"
            "            echo \"Skipping uv lock drift check: pyproject.toml and uv.lock are not both present.\"\n"
            "          fi\n",
            "",
            1,
        )
        with tempfile.TemporaryDirectory() as directory:
            unconditional = Path(directory) / "security-ci.yml"
            unconditional.write_text(text)
            errors = checker.validate(unconditional)
            self.assertIn("uv lock project guard", errors)
            self.assertIn("uv lock non-project skip", errors)

    def test_contract_rejects_blank_cache_and_synthetic_trivy_success(self):
        original = (ROOT / ".forgejo/workflows/security-ci.yml").read_text()
        broken = original.replace('mkdir -p "${TRIVY_CACHE_DIR}"', 'mkdir -p ""').replace(
            '--cache-dir "${TRIVY_CACHE_DIR}"', '--cache-dir ""'
        ).replace(
            'if [ ! -s trivy.json ]; then\n'
            '            echo "::error::Trivy completed without a non-empty JSON result; refusing synthetic empty findings" >&2\n'
            '            exit 1\n'
            '          fi',
            'test -s trivy.json || echo {Results:[]} > trivy.json',
        )
        with tempfile.TemporaryDirectory() as directory:
            workflow = Path(directory) / "security-ci.yml"
            workflow.write_text(broken)
            errors = checker.validate(workflow)
        self.assertIn("literal trivy cache mkdir", errors)
        self.assertIn("literal trivy cache argument", errors)
        self.assertIn("blank trivy cache path", errors)
        self.assertIn("trivy empty-result hard failure", errors)
        self.assertIn("synthetic empty trivy result", errors)

    def test_trivy_step_fails_loud_without_manufacturing_empty_results(self):
        workflow = (ROOT / ".forgejo/workflows/security-ci.yml").read_text()
        step = workflow.split("      - name: Run Trivy filesystem scan\n", 1)[1]
        step = step.split("\n      - name:", 1)[0]
        script = textwrap.dedent(step.split("        run: |\n", 1)[1])

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            helpers = {
                "curl": """#!/bin/sh
out=''
while [ "$#" -gt 0 ]; do
  if [ "$1" = -o ]; then out=$2; shift 2; else shift; fi
done
[ -z "$out" ] || : > "$out"
""",
                "sha256sum": "#!/bin/sh\ncat > /dev/null\nexit 0\n",
                "tar": "#!/bin/sh\nexit 0\n",
                "chmod": "#!/bin/sh\nexit 0\n",
                "mkdir": "#!/bin/sh\nexit 0\n",
                "timeout": """#!/bin/sh
if [ "$1" = -k ]; then shift 2; fi
shift
command=$1
shift
if [ "$command" = trivy ]; then
  output=''
  while [ "$#" -gt 0 ]; do
    if [ "$1" = --output ]; then output=$2; shift 2; else shift; fi
  done
  if [ "${FAKE_TRIVY_RC:-0}" -eq 0 ] && [ -n "$output" ]; then
    printf '%s\n' '{"Results":[]}' > "$output"
  fi
  exit "${FAKE_TRIVY_RC:-0}"
fi
exec "$command" "$@"
""",
            }
            for name, body in helpers.items():
                helper = bin_dir / name
                helper.write_text(body)
                helper.chmod(0o755)

            env = os.environ.copy()
            env.update({"PATH": f"{bin_dir}:{env['PATH']}", "FAKE_TRIVY_RC": "42"})
            failed = subprocess.run(
                ["bash", "-c", script], cwd=root, env=env, capture_output=True, text=True
            )
            self.assertEqual(failed.returncode, 42, failed.stderr)
            self.assertIn("refusing synthetic empty findings", failed.stderr)
            self.assertFalse((root / "trivy.json").exists())

            env["FAKE_TRIVY_RC"] = "0"
            succeeded = subprocess.run(
                ["bash", "-c", script], cwd=root, env=env, capture_output=True, text=True
            )
            self.assertEqual(succeeded.returncode, 0, succeeded.stderr)
            self.assertEqual((root / "trivy.json").read_text(), '{"Results":[]}\n')

    def test_cascade_is_event_driven_and_never_targets_primary(self):
        workflow = (ROOT / ".forgejo/workflows/security-ci-cascade.yml").read_text()
        script = (ROOT / "scripts/security-ci-cascade.py").read_text()
        self.assertIn("push:", workflow)
        self.assertNotIn("schedule:", workflow)
        self.assertIn("ci-failure-notifier", workflow)
        self.assertIn("DMs the Engineering lead", workflow)
        self.assertIn('branch = f"automation/plat-5093-security-ci-v{version}"', script)
        self.assertIn('"branch": branch', script)
        self.assertNotIn('"branch": base', script)
        self.assertIn("open_or_find_pr", script)

    def test_cascade_already_current_fleet_is_write_free(self):
        class CurrentFleetClient:
            def __init__(self, bundles, branches):
                self.bundles = bundles
                self.branches = branches
                self.writes = []

            def request(self, method, path, payload=None):
                if method != "GET":
                    self.writes.append((method, path, payload))
                    raise AssertionError(f"unexpected write: {method} {path}")
                repo = path.split("/")[3]
                return {"default_branch": self.branches[repo]}

            def optional(self, method, path):
                self.assert_read(method)
                repo = path.split("/")[3]
                file_path = path.split("/contents/", 1)[1].split("?", 1)[0]
                data = self.bundles[repo][file_path]
                return {
                    "content": base64.b64encode(data).decode(),
                    "sha": "already-current",
                }

            @staticmethod
            def assert_read(method):
                if method != "GET":
                    raise AssertionError(f"unexpected optional write: {method}")

        rows = [
            row
            for row in self.manifest["consumers"]
            if row["repo"] != self.manifest["source"]["repo"]
        ]
        client = CurrentFleetClient(
            {
                row["repo"]: vendor.render_bundle(self.manifest, row["repo"])
                for row in rows
            },
            {row["repo"]: row["default_branch"] for row in rows},
        )
        branch = f"automation/plat-5093-security-ci-v{self.manifest['contract_version']}"

        for row in rows:
            changed, pr = cascade.sync_repo(
                client,
                vendor,
                self.manifest,
                row,
                "shizuha-labs",
                branch,
                self.manifest["contract_version"],
            )
            self.assertFalse(changed, row["repo"])
            self.assertIsNone(pr, row["repo"])

        self.assertEqual(client.writes, [])

    def test_cascade_finds_existing_pull_request_beyond_first_page(self):
        branch = "automation/plat-5093-security-ci-v3"
        base = "master"
        target_url = "https://origin.shizuha.com/shizuha-labs/deploy/pulls/156"

        class PaginatedPullClient:
            def __init__(self):
                self.reads = []

            def request(self, method, path, payload=None):
                if method != "GET":
                    raise AssertionError(f"unexpected duplicate PR write: {method} {path}")
                self.reads.append(path)
                if path.endswith("&page=1"):
                    return [
                        {
                            "head": {"ref": f"unrelated-{number}"},
                            "base": {"ref": base},
                            "html_url": f"https://example.invalid/pulls/{number}",
                        }
                        for number in range(50)
                    ]
                if path.endswith("&page=2"):
                    return [
                        {
                            "head": {"ref": branch},
                            "base": {"ref": base},
                            "html_url": target_url,
                        }
                    ]
                raise AssertionError(f"unexpected pagination request: {path}")

        client = PaginatedPullClient()
        url = cascade.open_or_find_pr(
            client,
            "shizuha-labs",
            "deploy",
            branch,
            base,
            3,
        )

        self.assertEqual(url, target_url)
        self.assertEqual(
            client.reads,
            [
                "/repos/shizuha-labs/deploy/pulls?state=open&limit=50&page=1",
                "/repos/shizuha-labs/deploy/pulls?state=open&limit=50&page=2",
            ],
        )

    def test_manifest_is_stable_json_and_contains_expected_hashes(self):
        parsed = json.loads((ROOT / "security-ci/consumers.json").read_text())
        self.assertEqual(parsed["schema"], "shizuha.security-ci-consumers.v1")
        self.assertNotIn("REFRESH_REQUIRED", json.dumps(parsed))


if __name__ == "__main__":
    unittest.main()
