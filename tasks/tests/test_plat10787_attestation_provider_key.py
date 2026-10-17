"""PLAT-10787 — merge attestations key per (item, repo, number, provider).

A mirror-era attestation row must never shadow the canonical Origin identity:
provider-scoped uniqueness lets a forgejo row and a github row coexist for the
same linked PR, and the freeze gate's merge_commit filter selects the right one.
"""
from django.test import TestCase
from django.utils import timezone

from tasks.models import (
    Item,
    LinkedPullRequestMergeAttestation,
    Project,
)
from tasks.services.deployment_evidence import record_merge_attestation

ORIGIN_MERGE_COMMIT = '92e0e4169107b5efe6dcd088e5d58514eb51fff8'
MIRROR_MERGE_COMMIT = 'a' * 40


class AttestationProviderKeyTests(TestCase):
    def _item(self):
        project = Project.objects.create(
            name='PLAT-10787', key='P10787', org_id=1, access_level='private',
        )
        return Item.objects.create(
            project=project,
            item_key=f'PLAT-10787-{Item.objects.filter(project=project).count() + 1}',
            title='Security finding',
            description='Attestation provider-key regression guard.',
            mode='task',
            item_type='task.security',
            status='awaiting_merge',
            workflow_status='awaiting_merge',
            workflow_name='security-finding',
        )

    def _attest(self, item, provider, merge_commit, attested_at):
        return LinkedPullRequestMergeAttestation.objects.create(
            item=item,
            provider=provider,
            repo='shizuha-labs/browser',
            number=15,
            url=f'https://{provider}.example/shizuha-labs/browser/pulls/15',
            head_sha='6f045ff9599a4b4b6d0b67a79ae18130f32fef71',
            merge_commit=merge_commit,
            merged_at=timezone.now(),
            attested_by='pr-sync',
            attested_at=attested_at,
        )

    def test_mirror_era_row_cannot_shadow_canonical_origin_identity(self):
        item = self._item()
        older = timezone.now() - timezone.timedelta(days=1)
        self._attest(item, 'github', MIRROR_MERGE_COMMIT, older)
        canonical = self._attest(item, 'forgejo', ORIGIN_MERGE_COMMIT, timezone.now())

        hit = LinkedPullRequestMergeAttestation.objects.filter(
            item=item,
            repo='shizuha-labs/browser',
            number=15,
            merge_commit=ORIGIN_MERGE_COMMIT,
        ).first()
        self.assertIsNotNone(hit, 'freeze-gate lookup must find the Origin identity')
        self.assertEqual(hit.id, canonical.id)

    def test_same_provider_duplicate_is_rejected(self):
        item = self._item()
        self._attest(item, 'forgejo', ORIGIN_MERGE_COMMIT, timezone.now())
        with self.assertRaises(Exception):
            self._attest(item, 'forgejo', 'b' * 40, timezone.now())

    def test_upsert_mints_canonical_identity_while_mirror_row_exists(self):
        # aoi review 6470 regression: the upsert existence check was scoped
        # across providers, so an unordered .first() returned the github-era
        # shadow row and rejected the canonical forgejo mint at creation —
        # re-stranding the exact freeze gate this re-key unfreezes.
        item = self._item()
        older = timezone.now() - timezone.timedelta(days=1)
        self._attest(item, 'github', MIRROR_MERGE_COMMIT, older)

        canonical, created = record_merge_attestation(
            item=item,
            provider='forgejo',
            repo='shizuha-labs/browser',
            number=15,
            url='https://origin.example/shizuha-labs/browser/pulls/15',
            head_sha='6f045ff9599a4b4b6d0b67a79ae18130f32fef71',
            merge_commit=ORIGIN_MERGE_COMMIT,
            merged_at=timezone.now(),
        )
        self.assertTrue(created)
        self.assertEqual(canonical.provider, 'forgejo')
        self.assertEqual(canonical.merge_commit, ORIGIN_MERGE_COMMIT)
        # coexistence is the point of the re-key: the shadow row survives
        self.assertTrue(
            LinkedPullRequestMergeAttestation.objects.filter(
                item=item, provider='github',
            ).exists()
        )

    def test_upsert_same_provider_conflict_still_rejected(self):
        # the per-provider immutability check must stay intact after the
        # provider scoping: same provider, different merge identity -> raise.
        item = self._item()
        record_merge_attestation(
            item=item,
            provider='forgejo',
            repo='shizuha-labs/browser',
            number=15,
            url='https://origin.example/shizuha-labs/browser/pulls/15',
            head_sha='6f045ff9599a4b4b6d0b67a79ae18130f32fef71',
            merge_commit=ORIGIN_MERGE_COMMIT,
            merged_at=timezone.now(),
        )
        with self.assertRaises(Exception):
            record_merge_attestation(
                item=item,
                provider='forgejo',
                repo='shizuha-labs/browser',
                number=15,
                url='https://origin.example/shizuha-labs/browser/pulls/15',
                head_sha='6f045ff9599a4b4b6d0b67a79ae18130f32fef71',
                merge_commit='c' * 40,
                merged_at=timezone.now(),
            )
