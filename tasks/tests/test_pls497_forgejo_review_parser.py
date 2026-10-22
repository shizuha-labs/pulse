"""PLS-497 / Revi P1: Forgejo current-head review parser is exact-SHA only.

Stale or missing commit_id must never count as current-head SoD evidence, and
linked metadata must never choose the destination of a token-bearing request.
"""
import os
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from tasks.services.workflow_service import WorkflowService


HEAD = 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
STALE = 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'


class ForgejoCurrentHeadReviewParserTests(SimpleTestCase):
    def test_empty_string_hash_is_not_a_commit(self):
        """PLAT-11788: sha256('') must not compare equal to itself or a head."""
        empty_hash = 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'
        self.assertEqual(WorkflowService._normalize_scm_sha(empty_hash), '')
        self.assertEqual(WorkflowService._normalize_scm_sha(''), '')
        reviews = [{
            'user': {'login': 'aoi'},
            'commit_id': empty_hash,
            'state': 'APPROVED',
        }]
        self.assertEqual(
            WorkflowService._parse_forgejo_current_head_reviewers(reviews, empty_hash),
            set(),
        )

    def test_exact_normalized_commit_id_accepted(self):
        reviews = [
            {
                'user': {'login': 'Kai2574'},
                'commit_id': HEAD.upper(),  # case-insensitive normalize
                'state': 'APPROVED',
            },
            {
                'user': {'username': 'revi'},
                'commitID': f'  {HEAD}  ',
                'type': 'COMMENT',
            },
        ]
        got = WorkflowService._parse_forgejo_current_head_reviewers(reviews, HEAD)
        self.assertEqual(got, {'kai2574', 'revi'})

    def test_stale_commit_id_ignored(self):
        reviews = [
            {
                'user': {'login': 'kai2574'},
                'commit_id': STALE,
                'state': 'APPROVED',
            },
            {
                'user': {'login': 'aoi'},
                'commit_id': HEAD,
                'state': 'APPROVED',
            },
        ]
        got = WorkflowService._parse_forgejo_current_head_reviewers(reviews, HEAD)
        self.assertEqual(got, {'aoi'})
        self.assertNotIn('kai2574', got)

    def test_missing_or_blank_commit_id_ignored(self):
        reviews = [
            {'user': {'login': 'missing'}, 'state': 'APPROVED'},
            {'user': {'login': 'empty'}, 'commit_id': '', 'state': 'APPROVED'},
            {'user': {'login': 'spaces'}, 'commit_id': '   ', 'state': 'APPROVED'},
            {'user': {'login': 'none'}, 'commit_id': None, 'state': 'APPROVED'},
            {
                'user': {'login': 'current'},
                'commit_id': HEAD,
                'state': 'CHANGES_REQUESTED',
            },
        ]
        got = WorkflowService._parse_forgejo_current_head_reviewers(reviews, HEAD)
        self.assertEqual(got, {'current'})

    def test_empty_head_sha_yields_no_reviewers(self):
        reviews = [
            {'user': {'login': 'kai2574'}, 'commit_id': HEAD, 'state': 'APPROVED'},
        ]
        self.assertEqual(
            WorkflowService._parse_forgejo_current_head_reviewers(reviews, ''),
            set(),
        )
        self.assertEqual(
            WorkflowService._parse_forgejo_current_head_reviewers(reviews, None),
            set(),
        )

    def test_no_fallback_to_all_reviewers_when_none_match_head(self):
        """Regression: never promote stale/missing rows when exact filter is empty."""
        reviews = [
            {'user': {'login': 'stale'}, 'commit_id': STALE, 'state': 'APPROVED'},
            {'user': {'login': 'missing'}, 'state': 'APPROVED'},
        ]
        got = WorkflowService._parse_forgejo_current_head_reviewers(reviews, HEAD)
        self.assertEqual(got, set())


class ForgejoCurrentHeadReviewRequestBoundaryTests(SimpleTestCase):
    canonical_url = 'https://origin.shizuha.com/shizuha-labs/pulse/pulls/42'

    def _entry(self, url=None):
        return {'url': url or self.canonical_url, 'head_sha': HEAD}

    @patch('tasks.services.workflow_service.cache.get', return_value=None)
    @patch('requests.get')
    def test_hostile_or_noncanonical_url_performs_zero_requests(
        self, request_get, _cache_get,
    ):
        hostile_urls = (
            'http://origin.shizuha.com/shizuha-labs/pulse/pulls/42',
            'https://attacker.invalid/shizuha-labs/pulse/pulls/42',
            (
                'https://origin.shizuha.com.attacker.invalid/'
                'shizuha-labs/pulse/pulls/42'
            ),
            f'{self.canonical_url}//',
            f'{self.canonical_url}?target=attacker',
            f'{self.canonical_url}#fragment',
        )
        with patch.dict(
            os.environ,
            {'FORGEJO_TOKEN': 'test-token', 'ORIGIN_URL': 'https://trusted.invalid'},
            clear=True,
        ):
            for url in hostile_urls:
                self.assertEqual(
                    WorkflowService._forgejo_current_head_reviewers(
                        self._entry(url),
                    ),
                    (set(), None),
                )
        request_get.assert_not_called()

    @patch('tasks.services.workflow_service.cache.get', return_value=None)
    @patch('requests.get')
    def test_missing_trusted_base_fails_closed_without_request(
        self, request_get, _cache_get,
    ):
        with patch.dict(os.environ, {'FORGEJO_TOKEN': 'test-token'}, clear=True):
            reviewers, error = WorkflowService._forgejo_current_head_reviewers(
                self._entry(),
            )

        self.assertEqual(reviewers, set())
        self.assertEqual(error, 'forgejo_trusted_base_missing')
        request_get.assert_not_called()

    @patch('tasks.services.workflow_service.cache.get', return_value=None)
    @patch('requests.get')
    def test_canonical_url_uses_only_configured_trusted_base(
        self, request_get, _cache_get,
    ):
        pr_response = Mock()
        pr_response.raise_for_status.return_value = None
        pr_response.json.return_value = {'head': {'sha': HEAD}}
        reviews_response = Mock()
        reviews_response.raise_for_status.return_value = None
        reviews_response.json.return_value = [
            {
                'user': {'login': 'kai2574'},
                'commit_id': HEAD,
                'state': 'APPROVED',
            },
        ]
        request_get.side_effect = [pr_response, reviews_response]

        trusted_base = 'https://trusted-origin.internal'
        with patch.dict(
            os.environ,
            {'FORGEJO_TOKEN': 'test-token', 'ORIGIN_URL': f' {trusted_base}/ '},
            clear=True,
        ):
            reviewers, error = WorkflowService._forgejo_current_head_reviewers(
                self._entry(),
            )

        self.assertEqual(reviewers, {'kai2574'})
        self.assertIsNone(error)
        api_base = f'{trusted_base}/api/v1/repos/shizuha-labs/pulse/pulls/42'
        self.assertEqual(request_get.call_count, 2)
        self.assertEqual(request_get.call_args_list[0].args[0], api_base)
        self.assertEqual(request_get.call_args_list[1].args[0], f'{api_base}/reviews')


class ForgejoAuthorRequestBoundaryTests(SimpleTestCase):
    """PLS-564: linked author lookup shares the PLAT-4675 trust boundary."""

    canonical_url = 'https://origin.shizuha.com/shizuha-labs/pulse/pulls/42'

    def _entry(self, url=None):
        return {'url': url or self.canonical_url, 'head_sha': HEAD}

    @patch('tasks.services.workflow_service.cache.get', return_value=None)
    @patch('requests.get')
    def test_noncanonical_author_evidence_performs_zero_requests(
        self, request_get, _cache_get,
    ):
        hostile_urls = (
            'http://origin.shizuha.com/shizuha-labs/pulse/pulls/42',
            'https://attacker.invalid/shizuha-labs/pulse/pulls/42',
            f'{self.canonical_url}?target=attacker',
            f'{self.canonical_url}#fragment',
        )
        with patch.dict(
            os.environ,
            {'FORGEJO_TOKEN': 'test-token', 'ORIGIN_URL': 'https://trusted.invalid'},
            clear=True,
        ):
            for url in hostile_urls:
                self.assertEqual(
                    WorkflowService._origin_pr_author_login(self._entry(url)),
                    (None, 'not_origin_pr'),
                )
        request_get.assert_not_called()

    @patch('tasks.services.workflow_service.cache.get', return_value=None)
    @patch('requests.get')
    def test_missing_trusted_base_author_fails_closed_without_request(
        self, request_get, _cache_get,
    ):
        with patch.dict(os.environ, {'FORGEJO_TOKEN': 'test-token'}, clear=True):
            author, error = WorkflowService._origin_pr_author_login(
                self._entry(), require_exact_head=True,
            )

        self.assertIsNone(author)
        self.assertEqual(error, 'forgejo_trusted_base_missing')
        request_get.assert_not_called()

    @patch('tasks.services.workflow_service.cache.get', return_value=None)
    @patch('requests.get')
    def test_provenance_free_author_fails_closed_without_request(
        self, request_get, _cache_get,
    ):
        entry = {'url': self.canonical_url, 'head_sha': ''}
        with patch.dict(
            os.environ,
            {'FORGEJO_TOKEN': 'test-token', 'ORIGIN_URL': 'https://trusted.invalid'},
            clear=True,
        ):
            author, error = WorkflowService._origin_pr_author_login(entry)

        self.assertIsNone(author)
        self.assertEqual(error, 'origin_pr_provenance_missing')
        request_get.assert_not_called()

    @patch('tasks.services.workflow_service.cache.get', return_value=None)
    @patch('requests.get')
    def test_canonical_author_uses_only_configured_trusted_base(
        self, request_get, _cache_get,
    ):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            'user': {'login': 'Aoi'},
            'head': {'sha': HEAD.upper()},
            'state': 'open',
        }
        request_get.return_value = response

        trusted_base = 'https://trusted-origin.internal'
        with patch.dict(
            os.environ,
            {'FORGEJO_TOKEN': 'test-token', 'ORIGIN_URL': f' {trusted_base}/ '},
            clear=True,
        ):
            author, error = WorkflowService._origin_pr_author_login(
                self._entry(), require_exact_head=True,
            )

        self.assertEqual(author, 'aoi')
        self.assertIsNone(error)
        self.assertEqual(request_get.call_count, 1)
        self.assertEqual(
            request_get.call_args.args[0],
            f'{trusted_base}/api/v1/repos/shizuha-labs/pulse/pulls/42',
        )

    @patch('tasks.services.workflow_service.cache.get', return_value=None)
    @patch('requests.get')
    def test_stale_linked_head_cannot_supply_author_identity(
        self, request_get, _cache_get,
    ):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            'user': {'login': 'Aoi'},
            'head': {'sha': STALE},
            'state': 'open',
        }
        request_get.return_value = response

        with patch.dict(
            os.environ,
            {'FORGEJO_TOKEN': 'test-token', 'ORIGIN_URL': 'https://trusted.invalid'},
            clear=True,
        ):
            author, error = WorkflowService._origin_pr_author_login(
                self._entry(), require_exact_head=True,
            )

        self.assertIsNone(author)
        self.assertEqual(error, 'origin_pr_head_mismatch')

    @patch('tasks.services.workflow_service.cache.get', return_value=None)
    @patch('requests.get')
    def test_closed_pr_cannot_supply_author_identity(
        self, request_get, _cache_get,
    ):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            'user': {'login': 'Aoi'},
            'head': {'sha': HEAD},
            'state': 'closed',
        }
        request_get.return_value = response

        with patch.dict(
            os.environ,
            {'FORGEJO_TOKEN': 'test-token', 'ORIGIN_URL': 'https://trusted.invalid'},
            clear=True,
        ):
            author, error = WorkflowService._origin_pr_author_login(
                self._entry(), require_exact_head=True,
            )

        self.assertIsNone(author)
        self.assertEqual(error, 'origin_pr_not_open')


class AgentOriginIdentityRegistryTests(SimpleTestCase):
    """PLAT-5037: agent-name -> Origin-login mapping must resolve the legacy
    numeric-suffixed accounts (sara2574/kai2574) to the agent name so every
    name-keyed SoD/roster/attribution check collapses the two principals."""

    def test_explicit_registry_has_legacy_accounts_for_sara_and_kai(self):
        # Fails if the mapping is absent/emptied — the SoD checks would then
        # silently see sara2574/kai2574 as different principals from sara/kai.
        from tasks.services.workflow_service import AGENT_ORIGIN_IDENTITY_MAP
        self.assertIn('sara2574', AGENT_ORIGIN_IDENTITY_MAP['sara'])
        self.assertIn('kai2574', AGENT_ORIGIN_IDENTITY_MAP['kai'])
        self.assertIn('sara', AGENT_ORIGIN_IDENTITY_MAP['sara'])
        self.assertIn('kai', AGENT_ORIGIN_IDENTITY_MAP['kai'])

    def test_legacy_author_login_matches_agent_email(self):
        # sara2574 (Origin PR author) must match sara@shizuha.com (merger email).
        self.assertTrue(
            WorkflowService._origin_login_matches_email('sara2574', 'sara@shizuha.com')
        )
        self.assertTrue(
            WorkflowService._origin_login_matches_email('kai2574', 'kai@shizuha.com')
        )
        # And the native login matches too.
        self.assertTrue(
            WorkflowService._origin_login_matches_email('sara', 'sara@shizuha.com')
        )

    def test_unrelated_login_does_not_match(self):
        self.assertFalse(
            WorkflowService._origin_login_matches_email('jun', 'sara@shizuha.com')
        )
        self.assertFalse(
            WorkflowService._origin_login_matches_email('sara2574', 'jun@shizuha.com')
        )
