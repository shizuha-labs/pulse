"""PLAT-1209: risk labels must be auto-derived at task creation.

The 2026-07-02 PLAT-1149 audit found the high-risk QA sweep matched 0 of 32
completed tasks — including an auth-outage RCA (PLAT-1173), agent-runtime
daemon changes (PLAT-1193), and a Books rollout (BKST-3) — because authors
never apply the risk labels the QA machinery (PLS-184, PLAT-1147/#6559) keys
on. These tests pin the creation-time auto-labeling against those real cases.
"""

from django.test import TestCase

from tasks.models import Item
from tasks.services.risk_labels import (
    RISK_LABEL_PATTERNS,
    derive_risk_labels,
    merge_into_tags,
)


def _tags(item):
    return {t.strip() for t in (item.tags or '').split(',') if t.strip()}


class DeriveRiskLabelsTests(TestCase):
    def test_plat1173_style_auth_outage_rca(self):
        labels = derive_risk_labels(
            "[RCA] Fleet MCP auth outage 2026-07-02: shizuha-id post-auth mint 502 "
            "+ mcp-auth-proxy sidecar unbootstrapped (tokenreview rejected)",
            None,
        )
        self.assertIn('auth', labels)
        self.assertIn('agent-runtime', labels)

    def test_books_rollout_matches_payments_books(self):
        labels = derive_risk_labels(
            "Books data-visibility rollout", "voucher and ledger visibility change")
        self.assertIn('payments-books', labels)

    def test_migration_task(self):
        labels = derive_risk_labels(
            "Add search_vector trigger migration", "adds a data backfill step")
        self.assertIn('data-migration', labels)

    def test_neutral_title_matches_nothing(self):
        self.assertEqual(derive_risk_labels("Update meeting notes", "typo fix"), set())

    def test_empty_inputs(self):
        self.assertEqual(derive_risk_labels(None, None), set())
        self.assertEqual(derive_risk_labels("", ""), set())


class MergeIntoTagsTests(TestCase):
    def test_appends_missing_preserves_existing(self):
        merged = merge_into_tags("admin,mcp", {'auth', 'agent-runtime'})
        self.assertEqual(merged, "admin,mcp,agent-runtime,auth")

    def test_case_insensitive_dedup(self):
        merged = merge_into_tags("Auth, custom", {'auth'})
        self.assertEqual(merged, "Auth,custom")

    def test_empty_tags(self):
        self.assertEqual(merge_into_tags(None, {'auth'}), "auth")
        self.assertEqual(merge_into_tags("", set()), "")


class ItemCreationAutoLabelTests(TestCase):
    def test_task_creation_applies_risk_tags(self):
        item = Item.objects.create(
            mode="task",
            title="Fix OAuth login 502 on shizuha-id",
            description="JWT Set-Cookie exceeds nginx buffers",
        )
        item.refresh_from_db()
        self.assertIn('auth', _tags(item))

    def test_author_tags_are_preserved_and_deduped(self):
        item = Item.objects.create(
            mode="task",
            title="Harden credential broker bootstrap retry",
            tags="agent-runtime,custom-tag",
        )
        item.refresh_from_db()
        tags = _tags(item)
        self.assertIn('custom-tag', tags)
        self.assertIn('agent-runtime', tags)
        # not duplicated
        self.assertEqual((item.tags or '').lower().split(',').count('agent-runtime'), 1)

    def test_neutral_task_gets_no_risk_tags(self):
        item = Item.objects.create(
            mode="task",
            title="Update weekly meeting notes",
        )
        item.refresh_from_db()
        self.assertEqual(_tags(item) & set(RISK_LABEL_PATTERNS), set())

    def test_alerts_are_exempt(self):
        item = Item.objects.create(
            mode="alert",
            title="auth login failure alert",
            status="firing",
        )
        item.refresh_from_db()
        self.assertEqual(_tags(item), set())

    def test_update_does_not_relabel(self):
        item = Item.objects.create(mode="task", title="Update weekly meeting notes")
        item.title = "now mentions auth and kubernetes"
        item.save()
        item.refresh_from_db()
        self.assertEqual(_tags(item), set())
