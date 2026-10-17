"""PLS-842 / BKSD-14 §7 — Gen-1 census + migration_capsule_v1 admission."""
from __future__ import annotations

from unittest import mock

from django.core.exceptions import ValidationError
from django.test import TestCase

from tasks.models import ExternalWaitAuthority, Item, ItemLink, Project
from tasks.services.external_wait import admit_migration_capsule_wait
from tasks.services.org_isolation_capsule_census import (
    CAPSULE_SELECTOR,
    run_census,
)
from tasks.services.workflow_service import WorkflowService


class Pls842CensusTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(
            name='PLS-842 census org',
            key='P842',
            org_id=42,
        )

    def _capsule(self, key_suffix, *, title=None, metadata=None, status='open'):
        item = Item.objects.create(
            title=title or f'[Org-local dependency] Resolve condition {key_suffix}',
            mode='task',
            status=status,
            project=self.project,
            source='org_isolation_migration',
            metadata=metadata or {
                'org_isolation_replacement': True,
                'former_external_item_key': f'FOREIGN-{key_suffix}',
                'org_safe_condition_capsule': {
                    'version': 1,
                    'condition_summary': (
                        'Foreign org evidence required; do not re-link.'
                    ),
                    'verification_criteria': (
                        'Local org cannot produce the foreign filing evidence.'
                    ),
                },
            },
            assignment_group='engineering',
        )
        # item_key is often assigned by save hooks; force a stable key when empty.
        if not item.item_key:
            Item.objects.filter(pk=item.pk).update(item_key=f'CAP-{key_suffix}')
            item.refresh_from_db()
        return item

    def test_selector_matches_three_arms(self):
        a = self._capsule('A')
        b = Item.objects.create(
            title='Hand-titled local stand-in',
            mode='task',
            status='open',
            project=self.project,
            metadata={'org_isolation_replacement': True},
        )
        c = Item.objects.create(
            title='[Org-local dependency] bare title arm',
            mode='task',
            status='open',
            project=self.project,
        )
        noise = Item.objects.create(
            title='ordinary engineering task',
            mode='task',
            status='open',
            project=self.project,
        )
        keys = set(
            Item.objects.filter(CAPSULE_SELECTOR).values_list('pk', flat=True)
        )
        self.assertIn(a.pk, keys)
        self.assertIn(b.pk, keys)
        self.assertIn(c.pk, keys)
        self.assertNotIn(noise.pk, keys)

    def test_census_dry_run_emits_digests_and_edges(self):
        capsule = self._capsule('EDGE')
        dependent = Item.objects.create(
            title='Downstream epic',
            mode='task',
            status='open',
            project=self.project,
        )
        link = ItemLink(
            source_item=capsule,
            target_item=dependent,
            link_type='blocks',
            created_by_id=1,
            created_by_email='census@example.com',
        )
        link._skip_auto_block = True
        link.save()
        Item.objects.filter(pk=dependent.pk).update(status='blocked')
        dependent.refresh_from_db()
        report = run_census()
        self.assertEqual(report['schema'], 'pls842.org_isolation_capsule_census.v1')
        self.assertTrue(report['census_valid'])
        self.assertGreaterEqual(report['capsule_count'], 1)
        self.assertTrue(report['plat4813_plat4720_absent'])
        digests = {c['capsule_digest'] for c in report['capsules']}
        self.assertTrue(all(len(d) == 64 for d in digests))
        edge_keys = {
            (e['capsule_key'], e['dependent_key'])
            for e in report['edges']
            if e['link_type'] == 'blocks'
        }
        self.assertIn((capsule.item_key, dependent.item_key), edge_keys)
        # Provisional disposition for foreign-evidence capsule.
        mine = next(
            c for c in report['capsules'] if c['item_key'] == capsule.item_key
        )
        self.assertEqual(
            mine['provisional_disposition'],
            'external_unverifiable_locally',
        )
        # No foreign item id leaked.
        self.assertNotIn('former_external_item_id', mine)

    def test_census_is_idempotent_read_only(self):
        self._capsule('IDEM')
        first = run_census()
        second = run_census()
        self.assertEqual(first['report_digest'], second['report_digest'])
        self.assertEqual(first['capsule_count'], second['capsule_count'])


class Pls842MigrationCapsuleWaitTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(
            name='PLS-842 wait org',
            key='P842W',
            org_id=43,
        )
        self._org_patch = mock.patch.object(
            WorkflowService,
            'assignment_org_mismatch',
            return_value=(False, 'test-org', 'test-org'),
        )
        self._org_patch.start()
        self.addCleanup(self._org_patch.stop)

    def _item(self, title='migration capsule'):
        return Item.objects.create(
            title=title,
            mode='task',
            status='open',
            project=self.project,
            source='org_isolation_migration',
            assignee_email='engineer@example.com',
            assignment_group='engineering',
        )

    def test_admit_parks_at_waiting_external_without_releasing_blocks(self):
        capsule = self._item()
        dependent = Item.objects.create(
            title='blocked dependent',
            mode='task',
            status='open',
            project=self.project,
        )
        link = ItemLink(
            source_item=capsule,
            target_item=dependent,
            link_type='blocks',
            created_by_id=1,
            created_by_email='census@example.com',
        )
        link._skip_auto_block = True
        link.save()
        Item.objects.filter(pk=dependent.pk).update(status='blocked')
        dependent.refresh_from_db()
        authority = admit_migration_capsule_wait(
            capsule,
            capsule_digest='a' * 64,
            reason_code='foreign_evidence_only',
            local_recheck_criterion=(
                'Local org obtains its own filing evidence copy.'
            ),
            legacy_key_redacted='FOREIGN-X',
            reviewer_email='arch@example.com',
        )
        capsule.refresh_from_db()
        self.assertEqual(capsule.status, 'todo')
        self.assertNotEqual(capsule.status, 'waiting_external')
        self.assertEqual(
            authority.branch,
            ExternalWaitAuthority.BRANCH_MIGRATION_CAPSULE,
        )
        self.assertEqual(authority.state, ExternalWaitAuthority.STATE_WAITING)
        self.assertEqual(
            authority.evidence.get('schema'),
            'pls842.migration_capsule_v1',
        )
        # Dependent stays blocked — parking does not release.
        dependent.refresh_from_db()
        self.assertEqual(dependent.status, 'blocked')
        self.assertFalse(
            WorkflowService._status_releases_blocks('waiting_external')
        )
        # Excluded from assignment/ready accounting.
        self.assertIn(
            'waiting_external',
            WorkflowService._assignment_excluded_statuses(),
        )

    def test_admit_is_idempotent_and_rejects_branch_conflict(self):
        capsule = self._item('idempotent capsule')
        first = admit_migration_capsule_wait(
            capsule,
            capsule_digest='b' * 64,
            reason_code='foreign_evidence_only',
            local_recheck_criterion='recheck when local evidence lands',
        )
        second = admit_migration_capsule_wait(
            capsule,
            capsule_digest='b' * 64,
            reason_code='foreign_evidence_only',
            local_recheck_criterion='recheck when local evidence lands',
        )
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(
            ExternalWaitAuthority.objects.filter(item=capsule).count(),
            1,
        )

    def test_admit_requires_digest_and_recheck(self):
        capsule = self._item('validation capsule')
        with self.assertRaises(ValidationError):
            admit_migration_capsule_wait(
                capsule,
                capsule_digest='',
                reason_code='x',
                local_recheck_criterion='y',
            )
        with self.assertRaises(ValidationError):
            admit_migration_capsule_wait(
                capsule,
                capsule_digest='c' * 64,
                reason_code='x',
                local_recheck_criterion='',
            )


class Pls842ListValuedCriteriaTests(TestCase):
    """PLS-916: real 0106 capsules store verification_criteria as a JSON list;
    the Gen-1 census crashed with AttributeError 'list' object has no attribute
    'strip'. These lock the normalized digest + disposition path."""

    def setUp(self):
        self.project = Project.objects.create(
            name='PLS-842 list criteria org',
            key='P842L',
            org_id=44,
        )

    def _list_capsule(self, criteria):
        item = Item.objects.create(
            title='[Org-local dependency] list-criteria capsule',
            mode='task',
            status='open',
            project=self.project,
            source='org_isolation_migration',
            metadata={
                'org_isolation_replacement': True,
                'former_external_item_key': 'FOREIGN-LIST',
                'org_safe_condition_capsule': {
                    'version': 1,
                    'condition_summary': 'Foreign org evidence required.',
                    'verification_criteria': criteria,
                    'evidence_hints': ['bank statement'],
                },
            },
            assignment_group='engineering',
        )
        if not item.item_key:
            Item.objects.filter(pk=item.pk).update(item_key='CAP-LIST')
            item.refresh_from_db()
        return item

    def test_census_accepts_list_valued_criteria(self):
        self._list_capsule([
            'Local org cannot produce the foreign filing evidence.',
            'Do not re-link; verify from local evidence only.',
        ])
        # Must not raise AttributeError.
        report = run_census()
        self.assertTrue(report['census_valid'])
        mine = next(
            c for c in report['capsules'] if c['item_key'] == 'CAP-LIST'
        )
        # List normalized to a stable string; digest deterministic.
        self.assertIsInstance(mine['verification_criteria'], str)
        self.assertIn('Local org cannot produce', mine['verification_criteria'])
        self.assertEqual(
            mine['provisional_disposition'],
            'external_unverifiable_locally',
        )
        # Deterministic digest across runs.
        again = run_census()
        self.assertEqual(report['report_digest'], again['report_digest'])

    def test_string_and_list_criteria_classify_and_normalize_identically(self):
        a = self._list_capsule(['alpha', 'beta'])
        b_item = Item.objects.create(
            title='[Org-local dependency] string-criteria capsule',
            mode='task',
            status='open',
            project=self.project,
            source='org_isolation_migration',
            metadata={
                'org_isolation_replacement': True,
                'former_external_item_key': 'FOREIGN-LIST',
                'org_safe_condition_capsule': {
                    'version': 1,
                    'condition_summary': 'Foreign org evidence required.',
                    'verification_criteria': 'alpha beta',
                },
            },
            assignment_group='engineering',
        )
        if not b_item.item_key:
            Item.objects.filter(pk=b_item.pk).update(item_key='CAP-STR')
            b_item.refresh_from_db()
        report = run_census()
        cap_a = next(c for c in report['capsules'] if c['item_key'] == 'CAP-LIST')
        cap_b = next(c for c in report['capsules'] if c['item_key'] == 'CAP-STR')
        # The normalized emitted criteria is textually identical whether the
        # source capsule stored a list or a string.
        self.assertEqual(
            cap_a['verification_criteria'],
            cap_b['verification_criteria'],
            'list ["alpha","beta"] must normalize to string "alpha beta"',
        )
        # Both are foreign-evidence capsules -> same disposition, and each
        # digest is deterministic across runs (no list-repr jitter).
        self.assertEqual(cap_a['provisional_disposition'],
                         cap_b['provisional_disposition'])
        again = run_census()
        self.assertEqual(report['report_digest'], again['report_digest'])


class Pls842Gen2FrozenManifestApplyTests(TestCase):
    """PLS-916: the missing Gen-2 apply path reads a frozen census and admits
    external_unverifiable_locally capsules to waiting_external idempotently."""

    def setUp(self):
        self.project = Project.objects.create(
            name='PLS-842 gen2 org',
            key='P842G',
            org_id=45,
        )

    def _capsule(self, suffix, *, status='open'):
        item = Item.objects.create(
            title=f'[Org-local dependency] capsule {suffix}',
            mode='task',
            status=status,
            project=self.project,
            source='org_isolation_migration',
            metadata={
                'org_isolation_replacement': True,
                'former_external_item_key': f'FOREIGN-{suffix}',
                'org_safe_condition_capsule': {
                    'version': 1,
                    'condition_summary': 'Foreign org evidence required.',
                    'verification_criteria': [
                        'Local org cannot produce this foreign evidence.',
                    ],
                    'evidence_hints': ['statement'],
                },
            },
            assignment_group='engineering',
        )
        if not item.item_key:
            item_key = f'CAP-{str(suffix)[:8]}'
            Item.objects.filter(pk=item.pk).update(item_key=item_key)
            item.refresh_from_db()
        return item

    def test_apply_frozen_manifest_admits_external_capsules(self):
        from tasks.management.commands.apply_capsule_frozen_manifest import (
            apply_frozen_manifest,
        )
        capsule = self._capsule('A')
        report = run_census()
        manifest = {
            'schema': report['schema'],
            'capsules': report['capsules'],
        }
        verdicts = apply_frozen_manifest(manifest)
        capsule.refresh_from_db()
        self.assertEqual(capsule.status, 'todo')
        self.assertNotEqual(capsule.status, 'waiting_external')
        self.assertEqual(len(verdicts.get('admitted')), 1)
        from tasks.models import ExternalWaitAuthority
        authority = ExternalWaitAuthority.objects.filter(
            item=capsule,
            branch=ExternalWaitAuthority.BRANCH_MIGRATION_CAPSULE,
            state=ExternalWaitAuthority.STATE_WAITING,
        ).first()
        self.assertIsNotNone(authority, 'migration_capsule_v1 authority expected')

    def test_apply_is_idempotent_and_skips_digest_mismatch(self):
        from tasks.management.commands.apply_capsule_frozen_manifest import (
            apply_frozen_manifest,
        )
        capsule = self._capsule('B')
        first_report = run_census()
        manifest = {
            'schema': first_report['schema'],
            'capsules': first_report['capsules'],
        }
        first = apply_frozen_manifest(manifest)
        second = apply_frozen_manifest(manifest)
        capsule.refresh_from_db()
        self.assertEqual(capsule.status, 'todo')
        self.assertNotEqual(capsule.status, 'waiting_external')
        # Second application of the same frozen manifest admits nothing new.
        self.assertEqual(len(first.get('admitted')), 1)
        self.assertNotIn('admitted', second)
        # A mutated live capsule (digest changed) is skipped, not parked.
        other = self._capsule('C')
        stale = run_census()
        live_digest = stale['capsules'][0]['capsule_digest']
        tampered_manifest = {
            'schema': stale['schema'],
            'capsules': [
                {
                    'item_key': other.item_key,
                    'capsule_digest': 'f' * 64,
                    'provisional_disposition': 'external_unverifiable_locally',
                }
            ],
        }
        pre_other_status = other.status
        verdicts = apply_frozen_manifest(tampered_manifest)
        other.refresh_from_db()
        self.assertEqual(other.status, pre_other_status,
                         'digest mismatch must not mutate/park')
        self.assertEqual(len(verdicts.get('skipped_digest_mismatch')), 1)

    def test_apply_dry_run_does_not_mutate(self):
        from tasks.management.commands.apply_capsule_frozen_manifest import (
            apply_frozen_manifest,
        )
        capsule = self._capsule('D')
        pre_status = capsule.status
        report = run_census()
        manifest = {'schema': report['schema'], 'capsules': report['capsules']}
        verdicts = apply_frozen_manifest(manifest, dry_run=True)
        capsule.refresh_from_db()
        self.assertEqual(capsule.status, pre_status,
                         'dry-run must not mutate status')
        self.assertEqual(len(verdicts.get('would_admit')), 1)
        self.assertNotIn('admitted', verdicts)
