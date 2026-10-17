from django.test import SimpleTestCase, TestCase

from tasks.origin_provenance import (
    development_repo_block_reason,
    find_origin_run_url,
    infer_origin_observation,
    linked_pr_development_block_reason,
    merge_origin_observations,
    normalize_origin_observations,
    parse_linked_pr_identity,
    parse_origin_pr_reference,
    parse_origin_pr_url,
    parse_origin_run_url,
)
from tasks.serializers import ItemSerializer
from tasks.models import Item, Project


class OriginProvenanceTests(SimpleTestCase):
    def test_canonical_origin_pr_url_parser_is_fail_closed(self):
        self.assertEqual(
            parse_origin_pr_url(
                'https://origin.shizuha.com/shizuha-labs/shizuha-tasks/pulls/8'
            ),
            ('shizuha-labs', 'shizuha-tasks', 8),
        )
        for value in (
            'http://origin.shizuha.com/o/r/pulls/1',
            'https://origin.shizuha.com.evil.example/o/r/pulls/1',
            'https://evil.example/o/r/pulls/1',
            'https://origin.shizuha.com/o/r/pulls/1?token=smuggled',
            'https://origin.shizuha.com/o/r/pulls/1#fragment',
            'https://origin.shizuha.com/o%2Fr/pulls/1',
            'https://origin.shizuha.com/o/r/issues/1',
        ):
            with self.subTest(value=value):
                self.assertIsNone(parse_origin_pr_url(value))

    def test_origin_pr_reference_parser_has_strict_representation_parity(self):
        expected = ('shizuha-labs', 'shizuha-tasks', 8)
        self.assertEqual(
            parse_origin_pr_reference(
                'https://origin.shizuha.com/shizuha-labs/shizuha-tasks/pulls/8'
            ),
            expected,
        )
        self.assertEqual(
            parse_origin_pr_reference('shizuha-labs/shizuha-tasks#8'),
            expected,
        )
        for value in (
            'origin.shizuha.com/shizuha-labs/shizuha-tasks#8',
            'https://evil.example/shizuha-labs/shizuha-tasks#8',
            'https://origin.shizuha.com.evil.example/o/r/pulls/1',
            'shizuha-labs/shizuha-tasks#0',
            'shizuha-labs/shizuha-tasks#8?query',
            'shizuha-labs/extra/shizuha-tasks#8',
        ):
            with self.subTest(value=value):
                self.assertIsNone(parse_origin_pr_reference(value))

    def test_linked_pr_identity_matches_imported_github_and_origin_urls(self):
        expected = ('shizuha-labs', 'shizuha-tasks', 8)
        self.assertEqual(
            parse_linked_pr_identity(
                'https://github.com/Shizuha-Labs/Shizuha-Tasks/pull/8'
            ),
            expected,
        )
        self.assertEqual(
            parse_linked_pr_identity(
                'https://origin.shizuha.com/shizuha-labs/shizuha-tasks/pulls/8'
            ),
            expected,
        )
        self.assertEqual(
            parse_linked_pr_identity('shizuha-labs/shizuha-tasks#8'),
            expected,
        )
        for value in (
            'http://github.com/shizuha-labs/shizuha-tasks/pull/8',
            'https://github.com.evil.example/shizuha-labs/shizuha-tasks/pull/8',
            'https://github.com/shizuha-labs/shizuha-tasks/pull/8?query',
            'https://evil.example/shizuha-labs/shizuha-tasks/pull/8',
        ):
            with self.subTest(value=value):
                self.assertIsNone(parse_linked_pr_identity(value))

    def test_frozen_origin_shizuha_is_not_cli_development_sot(self):
        reason = development_repo_block_reason('shizuha-labs', 'shizuha')
        self.assertIsNotNone(reason)
        self.assertIn('shizuha-beta', reason)
        self.assertIsNone(development_repo_block_reason('shizuha-labs', 'shizuha-beta'))
        self.assertIsNone(development_repo_block_reason('shizuha-labs', 'hive'))
        self.assertIsNotNone(linked_pr_development_block_reason(
            'https://origin.shizuha.com/shizuha-labs/shizuha/pulls/28',
        ))
        self.assertIsNotNone(linked_pr_development_block_reason(
            'https://github.com/shizuha-labs/shizuha/pull/28',
        ))
        self.assertIsNone(linked_pr_development_block_reason(
            'https://origin.shizuha.com/shizuha-labs/shizuha-beta/pulls/1',
        ))
        self.assertIsNotNone(linked_pr_development_block_reason('', 'shizuha-labs/shizuha'))

    def test_relative_origin_job_url_is_canonical_and_repo_qualified(self):
        parsed = parse_origin_run_url(
            '/shizuha-labs/deploy/actions/runs/649/jobs/0'
        )
        self.assertEqual(parsed['repo'], 'shizuha-labs/deploy')
        self.assertEqual(parsed['run_number'], '649')
        self.assertEqual(parsed['job_id'], '0')
        self.assertEqual(
            parsed['run_url'],
            'https://origin.shizuha.com/shizuha-labs/deploy/actions/runs/649/jobs/0',
        )

    def test_description_run_url_is_discovered_without_author_duplication(self):
        self.assertEqual(
            find_origin_run_url(
                'Failing run: /shizuha-labs/deploy/actions/runs/424/jobs/0 (red).'
            ),
            'https://origin.shizuha.com/shizuha-labs/deploy/actions/runs/424/jobs/0',
        )

    def test_legacy_security_writer_prose_is_exactly_inferred(self):
        inferred = infer_origin_observation(
            source='security-ci',
            source_url='https://origin.shizuha.com/shizuha-labs/books/actions/runs/111',
            description='''Automated security finding from Forgejo CI.

            - Repository: `shizuha-labs/books`
            - Ref/SHA: `refs/heads/main` / `12b6540f1b630010f05ffd922bdf384e141e0afe`
            - Run: https://origin.shizuha.com/shizuha-labs/books/actions/runs/111
            ''',
        )
        self.assertEqual(inferred['repo'], 'shizuha-labs/books')
        self.assertEqual(inferred['ref'], 'refs/heads/main')
        self.assertEqual(
            inferred['commit_sha'],
            '12b6540f1b630010f05ffd922bdf384e141e0afe',
        )
        self.assertEqual(inferred['run_number'], '111')
        self.assertEqual(inferred['check'], 'security-ci')

    def test_internal_forgejo_run_url_is_published_as_public_origin(self):
        inferred = infer_origin_observation(
            source='security-ci',
            source_url=(
                'http://forgejo-http.origin.svc.cluster.local/'
                'shizuha-labs/books/actions/runs/111'
            ),
            description='''- Repository: `shizuha-labs/books`
            - Ref/SHA: `refs/heads/main` / `12b6540f1b630010f05ffd922bdf384e141e0afe`''',
        )
        self.assertEqual(
            inferred['run_url'],
            'https://origin.shizuha.com/shizuha-labs/books/actions/runs/111',
        )
        self.assertEqual(inferred['run_number'], '111')

    def test_free_form_non_origin_prose_is_not_guessed(self):
        inferred = infer_origin_observation(
            source='manual',
            source_url='https://github.com/shizuha-labs/books/actions/runs/111',
            description='- Repository: `shizuha-labs/books`\n- Ref/SHA: `main` / `12b6540f`',
        )
        self.assertEqual(inferred, {})

    def test_non_origin_run_url_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Shizuha Origin'):
            normalize_origin_observations([{
                'repo': 'shizuha-labs/deploy',
                'run_url': 'https://example.invalid/actions/runs/649',
            }])

    def test_observations_are_normalized_and_retry_deduplicated(self):
        rows = normalize_origin_observations([
            {
                'run_url': '/shizuha-labs/deploy/actions/runs/649/jobs/0',
                'commit_sha': 'ABCDEF1234567',
                'unknown_secret': 'must-not-survive',
            },
            {
                'repo': 'shizuha-labs/deploy',
                'run_number': 649,
                'job_id': 0,
                'commit_sha': 'abcdef1234567',
            },
        ])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['commit_sha'], 'abcdef1234567')
        self.assertNotIn('unknown_secret', rows[0])

    def test_append_merges_enrichment_for_same_run(self):
        rows = merge_origin_observations(
            [{'run_url': '/shizuha-labs/deploy/actions/runs/649/jobs/0'}],
            [{
                'repo': 'shizuha-labs/deploy',
                'run_number': 649,
                'job_id': 0,
                'commit_sha': 'abcdef1234567890',
                'workflow': 'deploy.yml',
            }],
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['commit_sha'], 'abcdef1234567890')
        self.assertEqual(rows[0]['workflow'], 'deploy.yml')
        self.assertIn('/actions/runs/649/jobs/0', rows[0]['run_url'])

    def test_serializer_exposes_first_class_observations_and_rejects_bad_sha(self):
        valid = ItemSerializer(data={
            'mode': 'task',
            'title': 'Origin build regression',
            'description': 'Reproduce and repair the exact failing build.',
            'metadata': {'origin_observations': [{
                'repo': 'shizuha-labs/deploy',
                'commit_sha': 'abcdef1234567890',
                'run_url': '/shizuha-labs/deploy/actions/runs/649',
            }]},
        })
        self.assertTrue(valid.is_valid(), valid.errors)
        row = valid.validated_data['metadata']['origin_observations'][0]
        self.assertEqual(row['repo'], 'shizuha-labs/deploy')
        self.assertEqual(
            row['run_url'],
            'https://origin.shizuha.com/shizuha-labs/deploy/actions/runs/649',
        )

        invalid = ItemSerializer(data={
            'mode': 'task',
            'title': 'Origin build regression',
            'description': 'Reproduce and repair the exact failing build.',
            'metadata': {'origin_observations': [{
                'repo': 'shizuha-labs/deploy',
                'commit_sha': 'not-a-commit',
            }]},
        })
        self.assertFalse(invalid.is_valid())
        self.assertIn('origin_observations', invalid.errors)


class OriginProvenanceSerializerWriteTests(TestCase):
    def test_legacy_security_item_is_enriched_on_read_without_migration(self):
        project = Project.objects.create(key='OLD', org_id=1, name='Legacy Security')
        item = Item.objects.create(
            project=project,
            mode='task',
            title='Existing security finding',
            description='''Automated security finding from Forgejo CI.

            - Repository: `shizuha-labs/books`
            - Last scan: `refs/heads/main` / `12b6540f1b630010f05ffd922bdf384e141e0afe`
            - Run: https://origin.shizuha.com/shizuha-labs/books/actions/runs/111
            ''',
            source='security-ci',
            source_url=(
                'http://forgejo-http.origin.svc.cluster.local/'
                'shizuha-labs/books/actions/runs/111'
            ),
            metadata={},
        )

        observation = ItemSerializer(item).data['origin_observations'][0]

        self.assertEqual(item.metadata, {})
        self.assertEqual(observation['repo'], 'shizuha-labs/books')
        self.assertEqual(observation['commit_sha'], '12b6540f1b630010f05ffd922bdf384e141e0afe')
        self.assertEqual(observation['run_number'], '111')
        self.assertEqual(
            observation['run_url'],
            'https://origin.shizuha.com/shizuha-labs/books/actions/runs/111',
        )

    def test_legacy_security_create_persists_inferred_observation(self):
        project = Project.objects.create(key='SEC', org_id=1, name='Security')
        serializer = ItemSerializer(data={
            'project': project.id,
            'mode': 'task',
            'title': 'Security CI regression',
            'description': '''Automated security finding from Forgejo CI.

            - Repository: `shizuha-labs/books`
            - Ref/SHA: `refs/heads/main` / `12b6540f1b630010f05ffd922bdf384e141e0afe`
            - Run: https://origin.shizuha.com/shizuha-labs/books/actions/runs/111
            ''',
            'source': 'security-ci',
            'source_url': 'https://origin.shizuha.com/shizuha-labs/books/actions/runs/111',
            # PLAT-9100 filing contract (carried live again by PLAT-9427's
            # create() consolidation): a teamless ownerless task create
            # loud-fails — name the owning team.
            'assignment_group': 'security',
        })
        self.assertTrue(serializer.is_valid(), serializer.errors)
        item = serializer.save()
        observation = item.metadata['origin_observations'][0]
        self.assertEqual(observation['commit_sha'], '12b6540f1b630010f05ffd922bdf384e141e0afe')
        self.assertEqual(observation['run_number'], '111')

    def test_append_contract_preserves_unrelated_metadata(self):
        project = Project.objects.create(key='ORIG', org_id=1, name='Origin')
        item = Item.objects.create(
            project=project,
            item_key='ORIG-1',
            mode='task',
            title='Build failure',
            description='Diagnose the exact failing Origin run.',
            metadata={'owner_hint': 'devops'},
        )
        serializer = ItemSerializer(item, data={
            'append_origin_observations': [{
                'run_url': '/shizuha-labs/deploy/actions/runs/649/jobs/0',
                'commit_sha': 'abcdef1234567890',
            }],
        }, partial=True)
        self.assertTrue(serializer.is_valid(), serializer.errors)
        updated = serializer.save()
        self.assertEqual(updated.metadata['owner_hint'], 'devops')
        self.assertEqual(
            updated.metadata['origin_observations'][0]['commit_sha'],
            'abcdef1234567890',
        )
