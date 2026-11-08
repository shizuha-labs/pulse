from io import StringIO

from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from tasks.models import Comment, Item, Project


def _hermetic_org(org_id):
    return {'id': org_id, 'slug': 'shizuha'}


def _hermetic_classify(email, **_kwargs):
    return ('canonical', (email or '').strip().lower())


class HermeticIdentityMixin:
    """Module-scope hoist of the identity-authority mocks (PLS-1338 gate).

    These tests write comments and serialize them through the digest contract;
    that path resolves the project's organization via
    admin_service.get_organization (Project.get_organization from the comment
    serializer) and roster/author identity via
    canonical_identity.classify_roster_email (the roster/lane signal chain).
    In the hermetic test environment both authorities point at the
    127.0.0.1:9 sentinel and fail open, spamming `Admin API error` /
    `resolution_error` ERRORs into the suite log — the unmocked write-time
    identity-lookup noise kei's discriminator (main run 7489) showed firing
    harmlessly on main but interacting with the PLS-539 face under this diff.
    Main's other tests mock these authorities (PLS-539 patches admin_service
    per request; the WIP-cap suite injects a FakeUserService); hoist
    equivalent mocks here so every write/serialize in this module is
    hermetic. Production behavior under test is untouched: the PLS-1338
    classification is author-suffix based and performs no lookups.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        for target, kwargs in (
            ('tasks.services.admin_service.admin_service.get_organization',
             {'side_effect': _hermetic_org}),
            ('tasks.services.canonical_identity.classify_roster_email',
             {'side_effect': _hermetic_classify}),
        ):
            p = patch(target, **kwargs)
            p.start()
            cls.addClassCleanup(p.stop)


class SystemCommentWriteTimeClassificationTests(HermeticIdentityMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.project = Project.objects.create(
            name='P', key='P1338', org_id=1, access_level='open')
        cls.item = Item.objects.create(
            project=cls.project, item_key='P1338-1', mode='task',
            title='fixture', status='todo')

    def test_automation_author_defaults_to_workflow_lifecycle(self):
        c = Comment.objects.create(
            item=self.item, author_id=0, author_email='system@pulse',
            content='Auto-failover to **x** — primary unavailable.')
        self.assertEqual(c.origin_kind, Comment.ORIGIN_WORKFLOW_LIFECYCLE)

    def test_workflow_author_classified(self):
        c = Comment.objects.create(
            item=self.item, author_id=0, author_email='workflow@pulse',
            content='Normalized status-lane ownership.')
        self.assertEqual(c.origin_kind, Comment.ORIGIN_WORKFLOW_LIFECYCLE)

    def test_human_author_stays_legacy_untyped(self):
        c = Comment.objects.create(
            item=self.item, author_id=7, author_email='zen@example.com',
            content='substantive note')
        self.assertEqual(c.origin_kind, Comment.ORIGIN_LEGACY_UNTYPED)

    def test_explicit_origin_kind_not_overridden(self):
        c = Comment.objects.create(
            item=self.item, author_id=0, author_email='system@pulse',
            content='resolver audit', origin_kind=Comment.ORIGIN_RESOLVER_AUDIT)
        self.assertEqual(c.origin_kind, Comment.ORIGIN_RESOLVER_AUDIT)

    def test_digest_partitioning_sees_system(self):
        # the serializer contract the digest consumes
        from tasks.serializers import CommentSerializer
        Comment.objects.create(
            item=self.item, author_id=0, author_email='system@pulse',
            content='churn row')
        human = Comment.objects.create(
            item=self.item, author_id=7, author_email='zen@example.com',
            content='substantive')
        data = CommentSerializer(
            Comment.objects.filter(item=self.item).order_by('id'), many=True
        ).data
        flags = [row['is_system'] for row in data]
        self.assertEqual(flags, [True, False])


class SystemCommentBackfillTests(HermeticIdentityMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.project = Project.objects.create(
            name='P', key='P1338B', org_id=1, access_level='open')
        cls.item = Item.objects.create(
            project=cls.project, item_key='P1338B-1', mode='task',
            title='fixture', status='todo')

    def test_backfill_flags_historical_automation_rows_idempotently(self):
        # simulate the historical defect: automation rows on the default kind
        system = Comment.objects.create(
            item=self.item, author_id=0, author_email='system@pulse',
            content='Auto-failover churn (historical, untyped)')
        workflow = Comment.objects.create(
            item=self.item, author_id=0, author_email='workflow@pulse',
            content='Rebalance churn (historical, untyped)')
        human = Comment.objects.create(
            item=self.item, author_id=7, author_email='zen@example.com',
            content='substantive historical')
        typed = Comment.objects.create(
            item=self.item, author_id=0, author_email='system@pulse',
            content='explicitly typed', origin_kind=Comment.ORIGIN_RESOLVER_AUDIT)
        out = StringIO()
        call_command('backfill_system_comment_classification', stdout=out)
        system.refresh_from_db(); workflow.refresh_from_db()
        human.refresh_from_db(); typed.refresh_from_db()
        self.assertEqual(system.origin_kind, Comment.ORIGIN_WORKFLOW_LIFECYCLE)
        self.assertEqual(workflow.origin_kind, Comment.ORIGIN_WORKFLOW_LIFECYCLE)
        self.assertEqual(human.origin_kind, Comment.ORIGIN_LEGACY_UNTYPED)
        self.assertEqual(typed.origin_kind, Comment.ORIGIN_RESOLVER_AUDIT)
        # idempotent: second run matches zero
        out2 = StringIO()
        call_command('backfill_system_comment_classification', stdout=out2)
        self.assertIn('classified 0 of 0', out2.getvalue())
