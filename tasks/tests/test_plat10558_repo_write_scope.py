"""PLAT-10558: only a definitive non-write collaborator answer blocks."""
import importlib.util
import os
import unittest


def _load():
    spec = importlib.util.spec_from_file_location(
        'repo_write_scope',
        'tasks/services/repo_write_scope.py',
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class RepoWriteScopeTests(unittest.TestCase):
    def test_read_and_missing_block(self):
        mod = _load()
        self.assertTrue(mod.permission_blocks_write('read', status_code=200))
        self.assertTrue(mod.permission_blocks_write(None, status_code=404))

    def test_probe_stays_off_without_explicit_flag(self):
        mod = _load()
        os.environ.pop('PLAT_10558_REPO_WRITE_PROBE', None)
        mod.probe_assignee_repo_write(1)

    def test_write_and_unknown_stay_open(self):
        mod = _load()
        self.assertFalse(mod.permission_blocks_write('write', status_code=200))
        self.assertFalse(mod.permission_blocks_write('admin', status_code=200))
        self.assertFalse(mod.permission_blocks_write('', status_code=503))
        self.assertFalse(mod.permission_blocks_write('', status_code=401))


if __name__ == '__main__':
    unittest.main()
