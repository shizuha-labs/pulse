import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ("deploy.yml", "premerge.yml", "mcp-contract-tests.yml")
SETTINGS = {"fsync": "off", "synchronous_commit": "off", "full_page_writes": "off"}


def startup_command(workflow):
    commands = [line.strip() for line in workflow.splitlines()
                if line.strip().startswith("pg_ctlcluster ")]
    if len(commands) != 1:
        raise AssertionError(f"Expected one disposable PostgreSQL startup, got {commands}")
    return commands[0]


class EphemeralPostgresTests(unittest.TestCase):
    def test_actual_startup_shell_passes_durability_options(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            executable = temporary / "pg_ctlcluster"
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, sys\n"
                "with open(os.environ['CALL_RECEIPT'], 'w') as output:\n"
                "    json.dump(sys.argv[1:], output)\n"
            )
            executable.chmod(0o755)
            for name in WORKFLOWS:
                with self.subTest(workflow=name):
                    workflow = (ROOT / ".forgejo" / "workflows" / name).read_text()
                    receipt = temporary / "receipt.json"
                    subprocess.run(
                        ["sh", "-ec", startup_command(workflow)], check=True, timeout=5,
                        env={**os.environ, "PATH": str(temporary) + os.pathsep + os.environ["PATH"],
                             "CALL_RECEIPT": str(receipt)},
                    )
                    arguments = json.loads(receipt.read_text())
                    self.assertEqual(arguments[-3:], ["15", "main", "start"])
                    self.assertEqual(arguments[:1], ["-o"])
                    options = shlex.split(arguments[1])
                    self.assertEqual(options[::2], ["-c"] * len(SETTINGS))
                    self.assertEqual(dict(option.split("=", 1) for option in options[1::2]), SETTINGS)

    def test_settings_match_existing_local_gate(self):
        local_gate = (ROOT / ".ci-local-gate.sh").read_text()
        for setting, value in SETTINGS.items():
            self.assertIn(f"-c {setting}={value}", local_gate)

    def test_startup_stays_job_local_without_database_volume(self):
        for name in WORKFLOWS:
            with self.subTest(workflow=name):
                workflow = (ROOT / ".forgejo" / "workflows" / name).read_text()
                command = startup_command(workflow)
                self.assertLess(workflow.index(command), workflow.index("createdb shizuha_pulse_ci"))
                self.assertIn("POSTGRES_HOST=127.0.0.1", workflow)
                self.assertIn("emptyDir: {}", workflow)
                self.assertNotIn("persistentVolumeClaim:", workflow)
                self.assertNotIn("/var/lib/postgresql", workflow)

    def test_production_dockerfile_does_not_disable_durability(self):
        dockerfile = (ROOT / "Dockerfile").read_text()
        self.assertNotIn("pg_ctlcluster", dockerfile)
        for setting in SETTINGS:
            self.assertNotIn(f"{setting}=off", dockerfile)


if __name__ == "__main__":
    unittest.main()
