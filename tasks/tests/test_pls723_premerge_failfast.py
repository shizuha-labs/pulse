"""
PLS-723 regression — the premerge gate must not under-report failures.

The deploy.yml premerge job used to run `manage.py test ... --failfast`, which
stops at the FIRST failure. A red log then reports `failures=1` regardless of
how many tests actually broke, so authors read "one root cause" and fix one
per ring (2026-07-27: one predicate change broke 3 fixtures, but each red run
showed 1 — two engineers spent turns on what one no-failfast run shows at once).

The fix is two-phase: --failfast gives the fast verdict on green; on red the
job re-runs WITHOUT --failfast so the log reports EVERY failing test before
exiting nonzero. This guard pins that contract so a bare --failfast revert
fails CI.
"""
from pathlib import Path

from django.test import SimpleTestCase

WORKFLOW = (
    Path(__file__).resolve().parents[2]
    / '.forgejo' / 'workflows' / 'deploy.yml'
)


def _logical_lines(text: str) -> list[str]:
    """Join shell backslash-continuation lines into logical command lines."""
    logical: list[str] = []
    current = ''
    for raw in text.splitlines():
        stripped = raw.strip()
        current = f'{current} {stripped}'.strip() if current else stripped
        if stripped.endswith('\\'):
            # Shell continuation: drop the trailing backslash and keep building.
            current = current[:-1].rstrip()
        else:
            logical.append(current)
            current = ''
    if current:
        logical.append(current)
    return logical


class PLS723PremergeFailfastTests(SimpleTestCase):
    def test_workflow_exists(self):
        self.assertTrue(WORKFLOW.exists(), f'missing {WORKFLOW}')

    def test_every_failfast_invocation_has_a_non_failfast_rerun(self):
        text = WORKFLOW.read_text()
        # Every manage.py test invocation that carries --failfast must be
        # paired, in the same block, with a non-failfast re-run so the full
        # failure set is reported on red. A bare --failfast revert fails this.
        # Commands may be split across backslash-continuation lines, so join
        # them first — the contract is about the command, not its formatting.
        failfast_lines = [
            line
            for line in _logical_lines(text)
            if 'manage.py test' in line
            and '--failfast' in line
            and not line.startswith('#')
        ]
        self.assertTrue(failfast_lines, 'no --failfast invocation found')
        for line in failfast_lines:
            self.assertIn(
                '|| {',
                line,
                f'--failfast invocation must be two-phase (re-run without '
                f'--failfast on failure): {line}',
            )
        # The paired re-run must actually drop --failfast and keep the
        # hermetic settings module.
        joined = ' '.join(_logical_lines(text)).replace('\\', ' ')
        self.assertIn(
            'manage.py test tasks --settings=taskmanager.test_settings '
            '--noinput --keepdb',
            joined,
            'missing non-failfast full-set re-run in the premerge gate',
        )

    def test_rerun_keeps_hermetic_settings(self):
        text = WORKFLOW.read_text()
        # Both the failfast pass and the full-set re-run must carry the
        # explicit hermetic settings module (check_hermetic_settings contract).
        joined = ' '.join(_logical_lines(text)).replace('\\', ' ')
        self.assertIn(
            'python manage.py test tasks --settings=taskmanager.test_settings '
            '--noinput --keepdb',
            joined,
        )
