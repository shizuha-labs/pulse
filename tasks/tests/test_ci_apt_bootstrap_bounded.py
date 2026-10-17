"""PLAT-4905 (recurrence of PLS-506): every apt-get in a Python CI workflow must
be bounded by ``timeout``.

A corrupt/stale partial ``InRelease`` can leave apt's http/gpgv workers alive
forever despite the ``Acquire`` timeouts, so a bare ``apt-get update``/``install``
holds the CI lane until the Job's ``activeDeadlineSeconds`` (mcp-contract-tests
wedged ~25min on gx10-6). PLS-506 bounded ``premerge.yml``/``deploy.yml``; this
guard makes the contract structural so a new/edited workflow cannot reintroduce
the wedge. Lives under ``tasks/tests`` so ``manage.py test tasks`` (the scoped CI
discovery, PLS-229) actually runs it.
"""
import re
from pathlib import Path
from unittest import TestCase

WORKFLOWS = Path(__file__).resolve().parents[2] / ".forgejo" / "workflows"
# Python CI workflows that apt-bootstrap a Debian toolchain in-cluster.
PYTHON_CI_WORKFLOWS = ("mcp-contract-tests.yml", "premerge.yml", "deploy.yml")


class AptBootstrapBoundedTests(TestCase):
    def test_every_apt_get_is_wrapped_in_timeout(self):
        for name in PYTHON_CI_WORKFLOWS:
            path = WORKFLOWS / name
            with self.subTest(workflow=name):
                text = path.read_text()
                apt_lines = [
                    line
                    for line in text.splitlines()
                    if "apt-get" in line and not line.lstrip().startswith("#")
                ]
                self.assertTrue(
                    apt_lines,
                    f"expected an apt-get bootstrap in {name}",
                )
                for line in apt_lines:
                    prefix = line.split("apt-get", 1)[0]
                    self.assertRegex(
                        prefix,
                        r"\btimeout\s+\d+\s+$",
                        msg=(
                            f"unbounded apt-get in {name}: {line.strip()!r} — wrap "
                            "it as `timeout <N> apt-get ...` (PLAT-4905 / PLS-506)"
                        ),
                    )

    def test_mcp_contract_uses_the_bounded_retry_loop(self):
        text = (WORKFLOWS / "mcp-contract-tests.yml").read_text()
        # The bounded bootstrap uses named helpers + a finite retry loop, matching
        # premerge.yml/deploy.yml — not an open-ended sleep;apt-get chain.
        self.assertIn("apt_update()", text)
        self.assertIn("apt_install()", text)
        self.assertRegex(text, r"for attempt in 1 2 3;")

    def test_full_suite_gates_share_the_parallel_recipe(self):
        """Keep PR and deploy gates below the shared 30-minute owner deadline."""
        required = (
            "runs-on: deploy-lane",
            "--python /workspace/src/.venv/bin/python --no-deps 'tblib==1.7.0'",
            "export TEST_JOBS=8",
            "python manage.py check --settings=taskmanager.test_settings",
            "python manage.py makemigrations --check --dry-run",
            '--noinput --failfast --keepdb --parallel "\\${TEST_JOBS}"',
        )
        for name in ("premerge.yml", "deploy.yml"):
            with self.subTest(workflow=name):
                text = (WORKFLOWS / name).read_text()
                for fragment in required:
                    self.assertIn(fragment, text)
