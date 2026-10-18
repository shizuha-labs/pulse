"""PLS-1346: the pulse repo's deployment-event recorder must mint non-empty
image_digest/image_tag.

Regression context: PLS-1279's fix (cortex#207) repaired the recorder for the
cortex repo only; the pulse repo's record-deployment-event.sh kept emitting
`image_digest: ""` / `image_tag: ""` (live event 14d520c9-…, pipeline run
49603), so focused-qa no-PR carriers on Pulse rollouts could not terminal-close
on digest evidence. The fix ports the cortex producer-class block
(PLS-1243/PLS-1277/PLS-1278): digest resolved from the converged rollout's
live pods, tag split from the repo:tag image string, both omitted entirely
when unresolved (fabricating identity claims is worse than omitting).

These tests exercise the recorder script end-to-end in DRY_RUN mode (no
cluster, no network): the payload the script would POST to the ingest
endpoint is asserted directly.
"""
import importlib.util
import json
import pathlib
import subprocess
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / '.forgejo/scripts/record-deployment-event.sh'
DIGEST = 'sha256:' + 'a' * 64


def _run(env_overrides):
    env = {
        'PATH': '/usr/bin:/bin:/usr/local/bin',
        'DEPLOYMENT_EVENT_DRY_RUN': '1',
        'GITHUB_REPOSITORY': 'shizuha-labs/pulse',
        'GITHUB_SHA': '01dd42f36077aaaaaaaaaaaaaaaaaaaaaaaaaaaa',
        'GITHUB_REF_NAME': 'main',
        'GITHUB_RUN_ID': '49603',
    }
    env.update(env_overrides)
    proc = subprocess.run(
        ['bash', str(SCRIPT), 'PLS', 'shizuha', 'shizuha-pulse', 'backend',
         'shizuha-pulse', 'localhost:30500/shizuha-pulse:shizuha-tasks-01dd42f',
         '/pulse/'],
        env=env, capture_output=True, text=True, timeout=60,
    )
    return proc


class RecorderDigestRegressionTests(unittest.TestCase):
    def test_digest_and_tag_persist_when_resolved(self):
        proc = _run({
            'DEPLOYMENT_EVENT_IMAGE_DIGEST': DIGEST,
            'DEPLOYMENT_EVENT_IMAGE_TAG': 'shizuha-tasks-01dd42f',
        })
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(payload['image_digest'], DIGEST)
        self.assertEqual(payload['image_tag'], 'shizuha-tasks-01dd42f')
        # image stays the full repo:tag string (ingest identity unchanged).
        self.assertEqual(payload['image'], 'localhost:30500/shizuha-pulse:shizuha-tasks-01dd42f')

    def test_unresolved_digest_is_omitted_not_fabricated(self):
        proc = _run({})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertNotIn('image_digest', payload)
        # Tag is split from IMAGE even when DRY_RUN skips the digest resolver.
        self.assertEqual(payload['image_tag'], 'shizuha-tasks-01dd42f')

    def test_explicit_digest_override_wins_unvalidated(self):
        # Documented contract (cortex parity): an explicitly-provided digest
        # (workflow-resolved) wins over in-script resolution and is carried
        # through as-is; only the in-script resolver validates the sha256
        # form. If the workflow ever passes a malformed digest it must be
        # fixed at the workflow layer, not silently dropped here.
        proc = _run({'DEPLOYMENT_EVENT_IMAGE_DIGEST': 'sha256:short'})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(payload['image_digest'], 'sha256:short')


class PodImageDigestSelectorTests(unittest.TestCase):
    """reika 6649: the live resolver must be exercised without a cluster."""

    @classmethod
    def setUpClass(cls):
        path = ROOT / '.forgejo/scripts/select_pod_image_digest.py'
        spec = importlib.util.spec_from_file_location('select_pod_image_digest', path)
        cls.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)

    def test_sha256_image_id_is_selected_and_non_sha256_is_dropped(self):
        good = 'sha256:' + 'b' * 64
        pods = [{
            'status': {'containerStatuses': [
                {
                    'name': 'backend',
                    'image': 'localhost:30500/shizuha-pulse:other',
                    'imageID': 'docker://not-a-digest',
                },
                {
                    'name': 'backend',
                    'image': 'localhost:30500/shizuha-pulse:shizuha-tasks-01dd42f',
                    'imageID': 'docker://registry.example/shizuha-pulse@' + good,
                },
            ]},
        }]
        self.assertEqual(
            self.mod.select_digest(
                pods, component='backend',
                image='localhost:30500/shizuha-pulse:shizuha-tasks-01dd42f',
            ),
            good,
        )
        dropped = [{
            'status': {'containerStatuses': [{
                'name': 'backend',
                'image': 'localhost:30500/shizuha-pulse:tag',
                'imageID': 'docker://registry.example/shizuha-pulse:tag',
            }]},
        }]
        self.assertEqual(
            self.mod.select_digest(
                dropped, component='backend',
                image='localhost:30500/shizuha-pulse:tag',
            ),
            '',
        )


if __name__ == '__main__':
    unittest.main()
