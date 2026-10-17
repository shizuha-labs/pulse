"""PLS-420: single-membership Security sign-off authority regressions."""
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase

from tasks.models import Comment, Item, Project, TeamRoster
from tasks.services.workflow_service import WorkflowService, WorkflowValidationError


SECURITY_EMAIL = 'akira@shizuha.com'
SECURITY_ALIAS = 'akira@agents.shizuha.io'
REVIEW_EMAIL = 'kei@shizuha.com'

THREAT_MODEL = """
## Threat Model (STRIDE-lite)
Assets: identities. Trust boundaries: external API edge. Data flows: client to API.
Spoofing: signed tokens. Tampering: request signatures. Repudiation: audit log.
Information Disclosure: scoped reads. Denial of Service: rate limits.
Elevation of Privilege: deny-by-default authorization.
Mitigation: server-side authorization; residual risk accepted for internal canary.
"""


class SecurityRosterAuthorityTests(TestCase):
    def setUp(self):
        WorkflowService._roster_cache = None
        TeamRoster.objects.update_or_create(
            organization_slug='shizuha',
            team_name='security',
            defaults={'tiers': [[SECURITY_EMAIL]], 'is_active': True},
        )
        TeamRoster.objects.update_or_create(
            organization_slug='shizuha',
            team_name='review',
            defaults={'tiers': [[REVIEW_EMAIL]], 'is_active': True},
        )

    def test_current_security_member_and_agent_alias_are_authorized(self):
        self.assertTrue(WorkflowService._is_security_team_email(SECURITY_EMAIL))
        self.assertTrue(WorkflowService._is_security_team_email(SECURITY_ALIAS))

    def test_single_team_membership_is_preserved_and_review_member_is_denied(self):
        security = TeamRoster.objects.get(
            organization_slug='shizuha', team_name='security')
        review = TeamRoster.objects.get(
            organization_slug='shizuha', team_name='review')
        review.tiers = [[SECURITY_EMAIL, REVIEW_EMAIL]]
        review.save()

        security.refresh_from_db()
        self.assertNotIn(SECURITY_EMAIL, WorkflowService._flat_roster(security.tiers))
        self.assertFalse(WorkflowService._is_security_team_email(SECURITY_EMAIL))
        self.assertFalse(WorkflowService._is_security_team_email(REVIEW_EMAIL))

    def test_admin_only_or_duplicate_admin_security_records_never_authorize(self):
        TeamRoster.objects.filter(
            organization_slug='shizuha', team_name='security').update(is_active=False)
        self.assertFalse(WorkflowService._is_security_team_email(REVIEW_EMAIL))

    def test_unavailable_pulse_authority_and_non_member_fail_closed(self):
        with patch(
            'tasks.models.TeamRoster.objects.filter',
            side_effect=RuntimeError('pulse roster unavailable'),
        ):
            self.assertFalse(WorkflowService._is_security_team_email(SECURITY_EMAIL))
        self.assertFalse(WorkflowService._is_security_team_email(REVIEW_EMAIL))

    def test_plat1328_uses_the_same_server_roster_predicate(self):
        self.assertTrue(WorkflowService._comment_author_is_security(
            SimpleNamespace(author_email=SECURITY_ALIAS)))
        self.assertFalse(WorkflowService._comment_author_is_security(
            SimpleNamespace(author_email=REVIEW_EMAIL)))


class StrideGateSecurityRosterTests(TestCase):
    def setUp(self):
        WorkflowService._roster_cache = None
        TeamRoster.objects.update_or_create(
            organization_slug='shizuha',
            team_name='security',
            defaults={'tiers': [[SECURITY_EMAIL]], 'is_active': True},
        )
        TeamRoster.objects.update_or_create(
            organization_slug='shizuha',
            team_name='review',
            defaults={'tiers': [[REVIEW_EMAIL]], 'is_active': True},
        )
        self.project = Project.objects.create(
            name='Pulse', key='PLST', org_id=1,
            access_level='private', is_active=True,
        )

    def _item(self):
        return Item.objects.create(
            project=self.project,
            title='HLD: external API trust boundary',
            description='External customer API boundary.' + THREAT_MODEL,
            labels=['trust-boundary'],
            status='in_review',
            workflow_name='reviewed-task',
            item_type='task',
            created_by_id=1,
            created_by_email=REVIEW_EMAIL,
        )

    def test_plat1253_accepts_current_security_author_and_rejects_review_author(self):
        item = self._item()
        Comment.objects.create(
            item=item,
            content='Security sign-off: approved — reviewed the trust boundary.',
            author_id=8,
            author_email=REVIEW_EMAIL,
        )
        with self.assertRaises(WorkflowValidationError):
            WorkflowService._validate_stride_gate(item, 'done')

        Comment.objects.create(
            item=item,
            content='Security sign-off: approved — independently reviewed.',
            author_id=9,
            author_email=SECURITY_EMAIL,
        )
        WorkflowService._validate_stride_gate(item, 'done')

    def test_missing_security_review_transition_has_actionable_fallback(self):
        item = self._item()
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._validate_stride_gate(item, 'done')
        message = str(ctx.exception)
        self.assertIn('when that transition is available', message)
        self.assertIn('assign or route the item to Security', message)
        self.assertIn('on this same item', message)
