"""PLAT-13191-shaped regression test: supervisord.conf percent-escape sweep.

The 2026-11-09 Pulse release crash family (PLAT-13203) traced to a bare ``%h``
in supervisord directives (boot death: "incomplete format"). pulse#949 escaped
the faces; this test pins the class so any future unescaped ``%`` in the file
fails the gate at the head, wherever it mutates (program names, command lines,
any directive — whole-file sweep per the 2026-11-09 scope refinement).
"""
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import check_supervisord_percent_escape as gate  # noqa: E402


class RepoConfSweepTests(unittest.TestCase):
    def test_repo_supervisord_conf_is_clean(self):
        """The committed supervisord.conf must contain no unescaped %."""
        conf = ROOT / "supervisord.conf"
        self.assertTrue(conf.exists(), "supervisord.conf must exist at repo root")
        violations = gate.sweep(conf.read_text(encoding="utf-8"), display="supervisord.conf")
        self.assertEqual(
            violations,
            [],
            "unescaped % in committed config: " + "; ".join(v.message for v in violations),
        )


class SweepBehaviourTests(unittest.TestCase):
    def test_bare_percent_h_is_caught(self):
        bad = "[program:w]\ncommand=celery worker -n w@%h\n"
        vs = gate.sweep(bad)
        self.assertEqual(len(vs), 1)
        self.assertEqual(vs[0].line_no, 2)

    def test_escaped_percent_h_passes(self):
        good = "[program:w]\ncommand=celery worker -n w@%%h\n"
        self.assertEqual(gate.sweep(good), [])

    def test_valid_expansions_pass(self):
        good = "[program:worker-%(process_num)02d]\ncommand=echo %(here)s\n"
        self.assertEqual(gate.sweep(good), [])

    def test_gunicorn_logformat_escapes_pass(self):
        """The committed gunicorn access-logformat uses %%(...) escapes."""
        good = (
            'command=gunicorn --access-logformat "%%({x-forwarded-for}i)s '
            '%%(l)s %%(u)s" taskmanager.wsgi:application\n'
        )
        self.assertEqual(gate.sweep(good), [])


class CliTests(unittest.TestCase):
    def test_cli_fails_on_bad_config(self):
        bad = pathlib.Path(ROOT / "supervisord.conf")  # real file is clean; use tmp via --file
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as f:
            f.write("[program:w]\ncommand=celery worker -n w@%h\n")
            tmp = f.name
        self.assertEqual(gate.main(["--file", tmp]), 1)

    def test_cli_passes_on_real_config(self):
        self.assertEqual(gate.main(["--root", str(ROOT)]), 0)


if __name__ == "__main__":
    unittest.main()
