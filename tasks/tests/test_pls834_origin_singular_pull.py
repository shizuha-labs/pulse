"""PLS-834 / PLAT-1296 v9: legacy Origin singular /pull/N row repair."""

from __future__ import annotations

import unittest
from unittest import mock

from tasks.origin_provenance import (
    canonical_origin_pr_url,
    origin_pr_link_ingress_error,
    parse_linked_pr_identity,
    parse_origin_pr_legacy_singular_url,
    parse_origin_pr_reference,
    parse_origin_pr_url,
)


class OriginSingularParseTests(unittest.TestCase):
    def test_canonical_plural_still_parses(self):
        self.assertEqual(
            parse_origin_pr_reference(
                "https://origin.shizuha.com/shizuha-labs/deploy/pulls/337"
            ),
            ("shizuha-labs", "deploy", 337),
        )

    def test_singular_is_not_trusted_reference(self):
        self.assertIsNone(
            parse_origin_pr_reference(
                "https://origin.shizuha.com/shizuha-labs/deploy/pull/337"
            )
        )
        self.assertIsNone(
            parse_origin_pr_url(
                "https://origin.shizuha.com/shizuha-labs/deploy/pull/337"
            )
        )

    def test_legacy_singular_identity_only(self):
        self.assertEqual(
            parse_origin_pr_legacy_singular_url(
                "https://origin.shizuha.com/shizuha-labs/deploy/pull/337"
            ),
            ("shizuha-labs", "deploy", 337),
        )
        self.assertEqual(
            parse_linked_pr_identity(
                "https://origin.shizuha.com/shizuha-labs/deploy/pull/337"
            ),
            ("shizuha-labs", "deploy", 337),
        )
        self.assertEqual(
            parse_linked_pr_identity(
                "https://origin.shizuha.com/shizuha-labs/deploy/pulls/337"
            ),
            ("shizuha-labs", "deploy", 337),
        )

    def test_hostile_singular_variants_fail_closed(self):
        for bad in (
            "http://origin.shizuha.com/shizuha-labs/deploy/pull/337",
            "https://origin.shizuha.com.evil/shizuha-labs/deploy/pull/337",
            "https://evil.example/shizuha-labs/deploy/pull/337",
            "https://origin.shizuha.com/shizuha-labs/deploy/pull/337?x=1",
            "https://origin.shizuha.com/shizuha-labs/deploy/pull/337#frag",
            "https://origin.shizuha.com/shizuha-labs/deploy/pull/0",
            "/shizuha-labs/deploy/pull/337",
        ):
            self.assertIsNone(parse_origin_pr_legacy_singular_url(bad), bad)

    def test_ingress_rejects_noncanonical_origin_shapes(self):
        """rui P1: query/fragment/downgrade must fail closed before persistence."""
        for bad in (
            "https://origin.shizuha.com/shizuha-labs/deploy/pull/337",
            "https://origin.shizuha.com/shizuha-labs/deploy/pull/337?x=1",
            "https://origin.shizuha.com/shizuha-labs/deploy/pull/337#frag",
            "https://origin.shizuha.com/shizuha-labs/deploy/pulls/337?x=1",
            "http://origin.shizuha.com/shizuha-labs/deploy/pulls/337",
            "https://user:pass@origin.shizuha.com/shizuha-labs/deploy/pulls/337",
            "https://origin.shizuha.com:8443/shizuha-labs/deploy/pulls/337",
        ):
            err = origin_pr_link_ingress_error(bad)
            self.assertIsNotNone(err, bad)
            self.assertIn("Error:", err)
            self.assertIn("/pulls/337", err)
        self.assertIsNone(
            origin_pr_link_ingress_error(
                "https://origin.shizuha.com/shizuha-labs/deploy/pulls/337"
            )
        )
        self.assertIsNone(origin_pr_link_ingress_error("shizuha-labs/deploy#337"))
        mirror = origin_pr_link_ingress_error(
            "https://github.com/shizuha-labs/deploy/pull/337"
        )
        self.assertIn("read-only mirror", mirror)

    def test_canonical_url_helper(self):
        self.assertEqual(
            canonical_origin_pr_url("shizuha-labs", "deploy", 337),
            "https://origin.shizuha.com/shizuha-labs/deploy/pulls/337",
        )


class LinkedPrApiRequestLegacySingularTests(unittest.TestCase):
    def test_legacy_singular_uses_configured_origin_base(self):
        from tasks.tasks import _linked_pr_api_request

        fake = mock.Mock()
        fake.ok = True
        with mock.patch("tasks.tasks.requests.get", return_value=fake) as get:
            result = _linked_pr_api_request(
                "https://origin.shizuha.com/shizuha-labs/deploy/pull/337",
                github_token="",
                forgejo_token="tok",
                origin_url="https://origin.shizuha.com",
            )
        self.assertIsNotNone(result)
        provider, owner, repo, number, response = result
        self.assertEqual(provider, "forgejo")
        self.assertEqual((owner, repo, int(number)), ("shizuha-labs", "deploy", 337))
        self.assertIs(response, fake)
        get.assert_called_once()
        self.assertEqual(
            get.call_args.args[0],
            "https://origin.shizuha.com/api/v1/repos/shizuha-labs/deploy/pulls/337",
        )




# ── Production-order gate regression (rei P1 on PR #173) ─────────────────────
# Exercises refresh → supersession → retired_mirror_keys → review_reentry
# validator for the PLAT-5671 shape, not just parsers / link_pr ingress.

import contextlib
import itertools
import re
from types import SimpleNamespace

from django.test import TestCase
from django.utils import timezone

from tasks.models import Item
from tasks.services.workflow_service import WorkflowService, WorkflowValidationError


SINGULAR_URL = "https://origin.shizuha.com/shizuha-labs/deploy/pull/337"
CANONICAL_URL = "https://origin.shizuha.com/shizuha-labs/deploy/pulls/337"
OTHER_CANONICAL = "https://origin.shizuha.com/shizuha-labs/deploy/pulls/999"
HOSTILE_SUPERSEDED = "https://origin.shizuha.com.evil.com/shizuha-labs/deploy/pulls/337"

_key_counter = itertools.count(1)


def _item(pull_requests):
    return Item.objects.create(
        item_key=f"PLS-834-T{next(_key_counter)}",
        title="pls834 singular reentry",
        status="in_progress",
        workflow_name="autonomous-bug",
        workflow_status="in_progress",
        assignee_id=101,
        assignee_email="author@agents.shizuha.io",
        created_by_id=101,
        created_by_email="author@agents.shizuha.io",
        metadata={"pull_requests": pull_requests},
        labels=[],
    )


def _validator_transition():
    return SimpleNamespace(
        validators=[{"type": "review_reentry_requires_progress"}],
        from_status="in_progress",
        to_status="in_review",
    )


def _stamp_bounce(item):
    transition = SimpleNamespace(
        id="request-changes",
        post_functions=[{"type": "stamp_review_bounce"}],
    )
    WorkflowService.execute_post_functions(transition, item)
    item.save(update_fields=["metadata", "updated_at"])
    item.refresh_from_db()


@contextlib.contextmanager
def _live_confirm(item, overrides=None):
    """Confirm each linked PR at its stored head (or override)."""
    overrides = overrides or {}

    def live_request(url, **_kwargs):
        prs = (getattr(item, "metadata", None) or {}).get("pull_requests") or []
        key = str(url or "").rstrip("/")
        pr = next(
            (
                p
                for p in prs
                if isinstance(p, dict) and str(p.get("url") or "").rstrip("/") == key
            ),
            {},
        )
        head = overrides.get(url)
        if head is None:
            head = overrides.get(key)
        if head is None:
            head = pr.get("head_sha") or "live-head"
        m = re.search(r"/([^/]+)/([^/]+)/pulls?/(\d+)", url)
        owner, repo, number = m.groups() if m else ("owner", "repo", "0")
        provider = "github" if "github.com" in url else "forgejo"
        status = pr.get("status") or "open"
        if status == "merged":
            payload = {"state": "closed", "merged": True, "head": {"sha": head}}
        elif status == "closed":
            payload = {"state": "closed", "head": {"sha": head}}
        else:
            payload = {"state": status, "head": {"sha": head}}
        response = mock.Mock(ok=True, status_code=200)
        response.json.return_value = payload
        return provider, owner, repo, number, response

    with mock.patch.dict(
        "os.environ",
        {
            "FORGEJO_TOKEN": "ci-test-token",
            "GITHUB_TOKEN": "ci-test-token",
            "ORIGIN_URL": "https://origin.shizuha.com",
            "PULSE_SUBSTRATE_CAP_MODE": "off",
        },
        clear=True,
    ), mock.patch("tasks.tasks._linked_pr_api_request", side_effect=live_request):
        yield


class Pls834ProductionOrderGateTests(TestCase):
    """PLAT-5671 production order at the real review-reentry caller boundary."""

    def test_malformed_open_no_head_blocks_before_supersession(self):
        """Bounce with singular open/no-head + canonical merged still blocks
        until refresh annotates superseded_by (pre-condition of the fix)."""
        item = _item(
            [
                {
                    "url": SINGULAR_URL,
                    "repo": "shizuha-labs/deploy",
                    "number": 337,
                    "status": "open",
                    # deliberately no head_sha — PLAT-5671 shape
                },
                {
                    "url": CANONICAL_URL,
                    "repo": "shizuha-labs/deploy",
                    "number": 337,
                    "status": "merged",
                    "head_sha": "mergedhead337",
                },
            ]
        )
        # Stamp bounce while singular is still an open obligation key.
        # Force bounce map to include the singular URL with a frozen head so
        # the gate sees it as an unchanged open obligation if not retired.
        _stamp_bounce(item)
        meta = dict(item.metadata or {})
        bounce = dict(meta.get("review_bounce_pr_heads") or {})
        bounce[SINGULAR_URL] = None  # missing head at bounce — sync-required shape
        meta["review_bounce_pr_heads"] = bounce
        item.metadata = meta
        item.save(update_fields=["metadata", "updated_at"])

        # Without live confirm / supersession path: helper alone with no
        # superseded_by and no refresh_confirmed must NOT retire the key.
        by_url = {
            (p.get("url") or "").rstrip("/"): p
            for p in item.metadata["pull_requests"]
        }
        self.assertEqual(
            WorkflowService._review_reentry_retired_mirror_keys(
                item,
                {SINGULAR_URL: None},
                all_prs_by_url=by_url,
                refresh_confirmed={CANONICAL_URL: True},
            ),
            set(),
        )

    def test_refresh_supersedes_singular_and_admits_reentry(self):
        """Full path: refresh confirms canonical merged, stamps superseded_by
        on singular, retires bounce key, admits Submit-for-Review."""
        item = _item(
            [
                {
                    "url": SINGULAR_URL,
                    "repo": "shizuha-labs/deploy",
                    "number": 337,
                    "status": "open",
                },
                {
                    "url": CANONICAL_URL,
                    "repo": "shizuha-labs/deploy",
                    "number": 337,
                    "status": "merged",
                    "head_sha": "mergedhead337",
                },
            ]
        )
        _stamp_bounce(item)
        meta = dict(item.metadata or {})
        bounce = dict(meta.get("review_bounce_pr_heads") or {})
        bounce[SINGULAR_URL] = "stale-or-null"
        # Canonical already terminal at bounce — rule 2 clears it after confirm.
        bounce[CANONICAL_URL] = "mergedhead337"
        meta["review_bounce_pr_heads"] = bounce
        meta["review_bounce_at"] = timezone.now().isoformat()
        item.metadata = meta
        item.save(update_fields=["metadata", "updated_at"])

        with _live_confirm(item, overrides={CANONICAL_URL: "mergedhead337"}):
            # Production-order: real validator (refresh inside).
            WorkflowService._run_validators(_validator_transition(), item)

        item.refresh_from_db()
        singular = next(
            p
            for p in item.metadata["pull_requests"]
            if p.get("url") == SINGULAR_URL
        )
        self.assertEqual(singular.get("superseded_by"), CANONICAL_URL)

    def test_hostile_superseded_by_does_not_retire(self):
        item = _item(
            [
                {
                    "url": SINGULAR_URL,
                    "status": "open",
                    "head_sha": "x",
                    "superseded_by": HOSTILE_SUPERSEDED,
                    "repo": "shizuha-labs/deploy",
                    "number": 337,
                },
                {
                    "url": CANONICAL_URL,
                    "status": "merged",
                    "head_sha": "mergedhead337",
                    "repo": "shizuha-labs/deploy",
                    "number": 337,
                },
            ]
        )
        by_url = {
            (p.get("url") or "").rstrip("/"): p
            for p in item.metadata["pull_requests"]
        }
        self.assertEqual(
            WorkflowService._review_reentry_retired_mirror_keys(
                item,
                {SINGULAR_URL: "x"},
                all_prs_by_url=by_url,
                refresh_confirmed={CANONICAL_URL: True},
            ),
            set(),
        )

    def test_different_coordinates_superseded_by_does_not_retire(self):
        item = _item(
            [
                {
                    "url": SINGULAR_URL,
                    "status": "open",
                    "head_sha": "x",
                    "superseded_by": OTHER_CANONICAL,
                    "repo": "shizuha-labs/deploy",
                    "number": 337,
                },
                {
                    "url": OTHER_CANONICAL,
                    "status": "merged",
                    "head_sha": "otherhead",
                    "repo": "shizuha-labs/deploy",
                    "number": 999,
                },
            ]
        )
        by_url = {
            (p.get("url") or "").rstrip("/"): p
            for p in item.metadata["pull_requests"]
        }
        self.assertEqual(
            WorkflowService._review_reentry_retired_mirror_keys(
                item,
                {SINGULAR_URL: "x"},
                all_prs_by_url=by_url,
                refresh_confirmed={OTHER_CANONICAL: True},
            ),
            set(),
        )

    def test_open_canonical_obligation_still_requires_advancement(self):
        """A same-head open canonical PR is NOT waived by a superseded singular
        sibling — advancement/rebuttal still required."""
        item = _item(
            [
                {
                    "url": SINGULAR_URL,
                    "status": "open",
                    "head_sha": "old",
                    "superseded_by": CANONICAL_URL,
                    "repo": "shizuha-labs/deploy",
                    "number": 337,
                },
                {
                    "url": CANONICAL_URL,
                    "status": "open",
                    "head_sha": "same-open-head",
                    "repo": "shizuha-labs/deploy",
                    "number": 337,
                },
            ]
        )
        _stamp_bounce(item)
        meta = dict(item.metadata or {})
        meta["review_bounce_pr_heads"] = {
            SINGULAR_URL: "old",
            CANONICAL_URL: "same-open-head",
        }
        meta["review_bounce_at"] = timezone.now().isoformat()
        item.metadata = meta
        item.save(update_fields=["metadata", "updated_at"])

        with self.assertRaises(WorkflowValidationError) as ctx, _live_confirm(
            item, overrides={CANONICAL_URL: "same-open-head", SINGULAR_URL: "old"}
        ):
            WorkflowService._run_validators(_validator_transition(), item)
        msg = str(ctx.exception)
        self.assertIn("Review re-entry gate", msg)
        # Canonical open obligation must still be named; singular may be retired.
        self.assertTrue(
            "pulls/337" in msg or "deploy#337" in msg or "same-open-head" in msg,
            msg,
        )

    def test_retired_helper_positive_merged_canonical(self):
        """Helper unit: singular + superseded_by + confirmed merged canonical → exempt."""
        prs = [
            {
                "url": SINGULAR_URL,
                "status": "open",
                "head_sha": "x",
                "superseded_by": CANONICAL_URL,
            },
            {
                "url": CANONICAL_URL,
                "status": "merged",
                "head_sha": "mergedhead337",
            },
        ]
        item = _item(prs)
        by_url = {(p.get("url") or "").rstrip("/"): p for p in prs}
        self.assertEqual(
            WorkflowService._review_reentry_retired_mirror_keys(
                item,
                {SINGULAR_URL: "x"},
                all_prs_by_url=by_url,
                refresh_confirmed={CANONICAL_URL: True},
            ),
            {SINGULAR_URL},
        )

if __name__ == "__main__":
    unittest.main()
