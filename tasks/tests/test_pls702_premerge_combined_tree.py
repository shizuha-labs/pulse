"""PLS-702: the required premerge gate must test the PR head MERGED WITH its base.

PLS-699 is the incident: head ``2e2d2e47`` and main ``2914f388`` each passed
``makemigrations --check`` in isolation, while their merge failed with two
migration leaves (``0134_pls458_security_gate_state`` and
``0134_pls698_active_queue_owner_index``). A gate that tests the head alone is
structurally incapable of seeing that class of break — the defect exists only in
the combination, so both inputs are green and the merge is red.

These are STRUCTURAL guards over the workflow file rather than behavioural tests.
The behaviour lives in an in-cluster Kubernetes Job that cannot be executed from
the Django suite, so what is assertable here is the contract the YAML encodes:
exact-SHA fetches for both sides, no fallback to a moving branch name, a local
merge that is never pushed, containment of both advertised commits, and the
required context name staying put.

That limitation is the point of the negative assertions: several of these can
only fail if someone edits the workflow toward the pre-PLS-702 shape, which is
exactly the regression worth catching. Each admission assertion below has a
refusal twin so the file cannot drift back by deletion.
"""
import pathlib
import re
import unittest

WORKFLOW = (
    pathlib.Path(__file__).resolve().parents[2]
    / '.forgejo' / 'workflows' / 'premerge.yml'
)


def _text():
    return WORKFLOW.read_text(encoding='utf-8')


class PremergeCombinedTreeContractTests(unittest.TestCase):
    """The workflow must encode the combined-tree contract (PLS-702 items 1-6)."""

    def setUp(self):
        self.src = _text()

    # ---- item 2: the required context name must not move --------------------

    def test_required_context_name_is_unchanged(self):
        """`premerge / test (pull_request)` is a REQUIRED branch-protection
        context. Renaming the workflow or the job silently un-gates every PR:
        protection would reference a context that never reports again.
        """
        self.assertRegex(self.src, r'(?m)^name:\s*premerge\s*$')
        self.assertRegex(self.src, r'(?m)^  test:\s*$')

    def test_trigger_is_pull_request_and_not_pull_request_target(self):
        """item 5: `pull_request_target` runs PR code with write credentials."""
        self.assertRegex(self.src, r'(?m)^on:\s*$')
        self.assertRegex(self.src, r'(?m)^\s+pull_request:\s*$')
        self.assertNotIn('pull_request_target', self.src)

    # ---- item 1: both sides fetched by EXACT sha ---------------------------

    def test_head_and_base_are_both_supplied_as_exact_shas(self):
        self.assertIn('value: "${{ github.event.pull_request.head.sha }}"', self.src)
        self.assertIn('value: "${{ github.event.pull_request.base.sha }}"', self.src)

    def test_base_repo_clone_url_is_advertised_not_assumed(self):
        """A fork PR's base repo is not its head repo; take the advertised one."""
        self.assertIn('value: "${{ github.event.pull_request.base.repo.clone_url }}"', self.src)

    def test_base_sha_has_no_moving_branch_fallback(self):
        """The fail-closed core of this change.

        A gate that substitutes `main`/`origin/main`/`HEAD` for an absent
        advertised base still reports the same green context while testing a
        different combination than the one being merged — the exact failure mode
        PLS-702 exists to remove. Absent base must EXIT, not retarget.
        """
        self.assertRegex(
            self.src,
            r"refusing: no exact advertised base SHA",
            msg='the workflow must fail closed when the advertised base is absent',
        )
        # the refusal twin: no assignment of a branch name into BASE_SHA
        for moving in (
            r'BASE_SHA="main"', r"BASE_SHA='main'",
            r'BASE_SHA="origin/main"', r'BASE_SHA="\$\{\{ github.ref',
            r'BASE_SHA="HEAD"', r'BASE_SHA="master"',
        ):
            self.assertNotRegex(
                self.src, moving,
                msg=f'moving-ref fallback for the advertised base: {moving}',
            )

    def test_fetch_depth_permits_a_merge_base(self):
        """`--depth=1` cannot merge: there is no common ancestor to merge from.

        Guarding the specific number is deliberate — reverting this line to
        depth=1 restores the head-only gate while every other assertion here
        still passes.
        """
        self.assertNotRegex(
            self.src, r'git fetch --depth=1\s',
            msg='depth=1 leaves no common ancestor; the merge cannot run',
        )
        self.assertIn('git fetch --depth=50 origin', self.src)
        self.assertIn('git fetch --depth=50 base', self.src)

    # ---- item 1/3: a local merge, verified by containment ------------------

    def test_the_merge_is_performed_and_is_not_a_fast_forward_checkout(self):
        self.assertRegex(self.src, r'merge --no-ff --no-edit "\\\$\{BASE_SHA\}"')

    def test_missing_common_ancestor_fails_closed(self):
        self.assertIn('no common ancestor between head and advertised base', self.src)

    def test_a_conflicting_merge_fails_before_the_test_container_runs(self):
        """The merge lives in an initContainer; a conflict must exit non-zero
        there, so PR-authored code is never executed by the test container.
        """
        self.assertIn('does not merge cleanly with its advertised base', self.src)
        merge_at = self.src.index('merge --no-ff --no-edit')
        test_container_at = self.src.index('name: test\n')
        self.assertLess(
            merge_at, test_container_at,
            msg='the merge must be resolved before the test container is defined',
        )

    def test_containment_of_both_advertised_commits_is_asserted(self):
        """Parent-shape is the wrong invariant: an already-merged base produces
        no merge commit even under --no-ff, so asserting two parents would fail
        on a legitimately up-to-date PR. Containment holds in both cases.
        """
        self.assertIn('git merge-base --is-ancestor "\\${SOURCE_SHA}" HEAD', self.src)
        self.assertIn('git merge-base --is-ancestor "\\${BASE_SHA}" HEAD', self.src)

    def test_head_base_and_tree_shas_are_recorded(self):
        """item 3: the log must let a reviewer reconstruct what was tested."""
        for probe in (
            'combined-tree head', 'combined-tree tree',
            'advertised head', 'advertised base', 'parents',
        ):
            self.assertIn(probe, self.src, msg=f'missing evidence line: {probe}')

    # ---- item 5: nothing is pushed ----------------------------------------

    def test_the_combined_tree_is_never_pushed(self):
        """A pushed temporary merge would create a real ref from untrusted code."""
        self.assertNotRegex(
            self.src, r'(?m)^\s*[^#\n]*\bgit push\b',
            msg='the temporary combined tree must never be pushed',
        )

    def test_only_origin_hosted_remotes_are_accepted(self):
        """Both sides go through the same non-Origin refusal."""
        self.assertIn('refusing non-Origin source URL', self.src)
        self.assertIn('refusing non-Origin base URL', self.src)

    # ---- item 4: the outdated-branch gate is preserved ---------------------

    def test_suite_still_runs_from_the_workspace_checkout(self):
        """The tests must run from the combined tree, not a fresh checkout."""
        self.assertIn('cd /workspace/src', self.src)
        self.assertIn('python manage.py test tasks', self.src)


class PremergeStillParsesTests(unittest.TestCase):
    """@jun's PLAT-5149 instance: a workflow edit that does not parse.

    A manual pre-flight binds to the moment it was run — jun parse-checked the
    workflows BEFORE an edit and not after, so the check passed while the pushed
    tree was broken. A committed check binds to the tree.

    stdlib-only on purpose: PyYAML is absent from the deploy-lane verify runner,
    so a yaml.safe_load here would fail for a reason unrelated to the file.
    """

    def test_workflow_file_exists_and_is_non_empty(self):
        self.assertTrue(WORKFLOW.is_file(), f'missing {WORKFLOW}')
        self.assertGreater(len(_text().strip()), 0)

    def test_no_tab_indentation_anywhere(self):
        """Tabs are invalid YAML indentation and are invisible in review."""
        offenders = [
            i for i, line in enumerate(_text().splitlines(), 1)
            if line[:len(line) - len(line.lstrip())].count('\t')
        ]
        self.assertEqual(offenders, [], f'tab-indented lines: {offenders}')

    def test_shell_command_substitution_stays_escaped_in_the_heredoc(self):
        """The Job manifest is an UNQUOTED heredoc, so an unescaped `$(` is
        evaluated by the Origin runner instead of the in-pod shell — producing an
        empty value that silently changes what is compared.
        """
        body = _text()
        start = body.index('cat <<YAML | kubectl apply -f -')
        end = body.index('\n          YAML', start)
        manifest = body[start:end]
        unescaped = re.findall(r'(?<!\\)\$\((?!\{)', manifest)
        self.assertEqual(
            unescaped, [],
            msg=f'{len(unescaped)} unescaped command substitution(s) in the heredoc',
        )


class PodScriptIsValidShellTests(unittest.TestCase):
    """Syntax-check the script the init container will actually run.

    Converted from a manual pre-flight into a committed check on @jun's ladder
    (PLAT-5149): *a manual check binds to the moment it was run; a committed one
    binds to the tree.* The pod script is otherwise only exercised at CI runtime,
    where a syntax error surfaces as a failed required gate on someone else's PR.

    Two layers of quoting sit between this file and the pod: the Job manifest is
    produced by an UNQUOTED heredoc (so ``\\$`` becomes ``$``), and ``${{ … }}``
    expressions are rendered by the Origin runner before the pod exists. Both are
    undone here so ``sh -n`` sees what the pod's shell will see.
    """

    def _pod_script(self, container):
        raw = _text()
        start = raw.index(f'- name: {container}')
        end = raw.index('env:', start)
        lines = [
            l for l in raw[start:end].splitlines()
            if l.strip() and not l.strip().startswith(
                ('- name:', 'image:', 'command:', 'args:', '- |')
            )
        ]
        indent = min(len(l) - len(l.lstrip()) for l in lines)
        script = "\n".join(l[indent:] for l in lines)
        script = script.replace('\\$', '$')
        return re.sub(r'\$\{\{[^}]*\}\}', 'deadbeef' * 5, script)

    def test_git_clone_script_is_syntactically_valid_shell(self):
        import subprocess
        import tempfile
        script = self._pod_script('git-clone')
        self.assertIn('merge --no-ff', script, 'extracted the wrong block')
        with tempfile.NamedTemporaryFile('w', suffix='.sh') as fh:
            fh.write(script)
            fh.flush()
            proc = subprocess.run(
                ['sh', '-n', fh.name], capture_output=True, text=True,
            )
        self.assertEqual(
            proc.returncode, 0,
            msg=f'pod git-clone script is not valid shell:\n{proc.stderr}',
        )


if __name__ == '__main__':
    unittest.main()
