import os
import re
import subprocess
from pathlib import Path
from unittest import TestCase

import yaml


class WorkflowShellSyntaxTests(TestCase):
    """Keep embedded Actions shell programs parseable before they reach a runner."""

    def test_every_run_block_parses_as_bash(self):
        workflows = Path(__file__).parents[2] / ".forgejo/workflows"
        checked = 0

        def walk(value, source):
            nonlocal checked
            if isinstance(value, dict):
                for key, child in value.items():
                    if key == "run" and isinstance(child, str):
                        checked += 1
                        # Forgejo expressions are substituted before execution;
                        # replace them so bash -n validates the resulting shell
                        # structure, including heredoc delimiter alignment.
                        script = re.sub(r"\$\{\{.*?\}\}", "FORGEJO_EXPR", child)
                        result = subprocess.run(
                            ["bash", "--noprofile", "--norc", "-n"],
                            input=script,
                            text=True,
                            capture_output=True,
                            check=False,
                        )
                        self.assertEqual(
                            result.returncode,
                            0,
                            f"{source}: invalid bash run block:\n{result.stderr}",
                        )
                    else:
                        walk(child, source)
            elif isinstance(value, list):
                for child in value:
                    walk(child, source)

        for path in sorted(workflows.glob("*.yml")):
            walk(yaml.safe_load(path.read_text()), path.name)

        self.assertGreater(checked, 0)

    def test_source_authority_env_survives_unquoted_manifest_heredoc(self):
        workflow = Path(__file__).parents[2] / ".forgejo/workflows/premerge.yml"
        document = yaml.safe_load(workflow.read_text())
        run_script = next(
            step["run"]
            for step in document["jobs"]["test"]["steps"]
            if step.get("name") == "Run backend test suite before deploy"
        )
        start = run_script.index("cat <<YAML")
        end = run_script.index("\nYAML", start) + len("\nYAML")
        manifest_writer = run_script[start:end].replace(
            "cat <<YAML | kubectl apply -f -",
            "cat <<YAML",
        )
        manifest_writer = re.sub(
            r"\$\{\{.*?\}\}",
            "FORGEJO_EXPR",
            manifest_writer,
        )
        env = os.environ.copy()
        env.pop("SOURCE_SHA", None)
        env["JOB"] = "ci-test-shizuha-pulse-deadbee"

        result = subprocess.run(
            ["bash", "--noprofile", "--norc", "-u"],
            input=manifest_writer,
            text=True,
            capture_output=True,
            check=False,
            env=env,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stderr,
            "",
            "unquoted manifest heredoc evaluated an in-pod command or variable",
        )
        source_authority = result.stdout[
            result.stdout.index("- name: source-authority-contract") :
            result.stdout.index("- name: git-clone")
        ]
        self.assertIn('case "${SOURCE_SHA}" in', source_authority)
        self.assertIn('"${SOURCE_SHA}" > /tmp/source-authority.json', source_authority)
        self.assertIn(
            "curl --fail-with-body --retry 8 --retry-connrefused",
            source_authority,
        )
        self.assertIn("--retry-delay 2 --retry-max-time 30", source_authority)
        self.assertNotIn(
            "--retry-all-errors",
            source_authority,
            "deterministic contract rejections must fail immediately",
        )


class HeredocCommentExpansionTests(TestCase):
    """Comments inside an UNQUOTED heredoc must not be shell-evaluable.

    A pod script embedded in `cat <<YAML | kubectl apply -f -` is expanded by the
    Origin runner before the manifest is applied. Inside such a heredoc, markdown
    backticks are command substitution and `${VAR}` is a parameter expansion — so
    a *comment* documenting shell syntax gets executed by the runner, where those
    names are undefined.

    That is harmless to the pod (the text stays a comment) but it printed five
    fake errors into every build log:

        line 7:  SOURCE_SHA: unbound variable
        line 7:  tasks: command not found
        line 7:  manage.py: command not found
        line 13: SOURCE_SHA: unbound variable
        line 13: uv: command not found

    Those appeared identically in passing and failing runs (verified against
    shizuha-tasks build runs 2697/2707), so they are noise — but noise that
    actively misleads CI triage because it looks like the cause of whatever else
    went wrong.

    Only comment lines are checked. Deliberate runner-side expansion of real
    variables elsewhere in an unquoted heredoc is a legitimate, widely-used
    pattern here; inside a comment it can only ever be accidental.
    """

    #  <<DELIM / <<-DELIM expand; <<'DELIM' / <<"DELIM" do not.
    _HEREDOC = re.compile(r"<<-?\s*(?P<quote>['\"]?)(?P<delim>[A-Za-z_][A-Za-z0-9_]*)(?P=quote)")
    _BAD_BACKTICK = re.compile(r"(?<!\\)`")
    _BAD_EXPANSION = re.compile(r"(?<!\\)\$\{")

    def test_no_comment_inside_an_unquoted_heredoc_is_shell_evaluable(self):
        workflows = Path(__file__).parents[2] / ".forgejo/workflows"
        offences: list[str] = []
        scanned_heredocs = 0

        def scan(script: str, source: str):
            nonlocal scanned_heredocs
            delim = None
            for lineno, raw in enumerate(script.splitlines(), start=1):
                if delim is None:
                    match = self._HEREDOC.search(raw)
                    if match and match.group("quote") == "":
                        delim = match.group("delim")
                        scanned_heredocs += 1
                    continue
                if raw.strip() == delim:
                    delim = None
                    continue
                stripped = raw.lstrip()
                if not stripped.startswith("#"):
                    continue
                if self._BAD_BACKTICK.search(raw) or self._BAD_EXPANSION.search(raw):
                    offences.append(f"{source}:{lineno}: {raw.strip()}")

        for path in sorted(workflows.glob("*.yml")):
            for block_index, block in enumerate(
                re.split(r"\n---\n", path.read_text())
            ):
                scan(block, f"{path.name}#block{block_index}")

        # The scan must have found at least one unquoted heredoc, or it passed
        # vacuously.
        self.assertGreater(scanned_heredocs, 0)
        self.assertEqual(
            offences, [],
            "comment inside an unquoted heredoc must escape backticks and ${}:\n"
            + "\n".join(offences),
        )
