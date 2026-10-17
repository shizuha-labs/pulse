"""PLS-238: CVQA-L7 QA Target Map — unit tests."""
from unittest.mock import patch, MagicMock

from django.test import TestCase, override_settings

from tasks.qa_target_map import (
    MapFetch,
    MapRow,
    WikiAuthUnavailable,
    _mint_service_token,
    _normalize_title,
    _parse_map_table,
    fetch_target_map,
    fetch_target_map_full,
    compose_checklist,
    f1_slice_size,
    f2_slice_size,
    no_map_banner,
    fetch_error_banner,
)
from tasks.qa_cadence import (
    QA_TIERS,
    build_task_fields,
    _build_checklist_from_map,
    _Svc,
)
from tasks.models import QaCadenceRotation

# ── parser ────────────────────────────────────────────────────────────────────

MAP_TABLE_MD = """\
| Tier | Feature | Expectation | Recipe |
|------|---------|-------------|--------|
| F0 | Login | Auth token issued | POST /auth/ |
| F0 | Org chart | Loads without 500 | GET /org/ |
| F1 | DM send | Recipient receives in real-time | WS ping |
| F1 | Notification | Badge appears | poll |
| F2 | Media upload | File attached to post | multipart |
| F2 | Search | Results within 2s | GET /search/ |
| F2 | Thread reply | Nested reply visible | POST /reply/ |
"""

EXTRA_BEFORE = "# QA Target Map — Test Service\n\nSome preamble.\n\n"


class TestParseMapTable(TestCase):
    def _rows(self, body="", url="https://wiki/page"):
        return _parse_map_table(body, url)

    def test_parse_all_tiers(self):
        rows = self._rows(MAP_TABLE_MD)
        self.assertEqual(len(rows), 7)
        f0 = [r for r in rows if r.tier == "F0"]
        f1 = [r for r in rows if r.tier == "F1"]
        f2 = [r for r in rows if r.tier == "F2"]
        self.assertEqual(len(f0), 2)
        self.assertEqual(len(f1), 2)
        self.assertEqual(len(f2), 3)

    def test_feature_expectation_recipe_populated(self):
        rows = self._rows(MAP_TABLE_MD)
        login = next(r for r in rows if r.feature == "Login")
        self.assertEqual(login.expectation, "Auth token issued")
        self.assertEqual(login.recipe, "POST /auth/")

    def test_page_url_anchor(self):
        rows = self._rows(MAP_TABLE_MD, url="https://wiki/page/123")
        login = next(r for r in rows if r.feature == "Login")
        self.assertTrue(login.page_url.startswith("https://wiki/page/123#"))

    def test_checklist_line_format(self):
        row = MapRow("F0", "My Feature", "Does X", "recipe Y", "https://w/p#f")
        line = row.checklist_line()
        self.assertIn("[My Feature]", line)
        self.assertIn("Does X", line)
        self.assertIn("recipe Y", line)

    def test_empty_body_returns_no_rows(self):
        self.assertEqual(self._rows(""), [])

    def test_body_with_preamble(self):
        """Header text before the table should not confuse the parser."""
        body = EXTRA_BEFORE + MAP_TABLE_MD
        rows = self._rows(body)
        self.assertEqual(len(rows), 7)

    def test_invalid_tier_rows_skipped(self):
        body = "| Tier | Feature | Expectation |\n|---|---|---|\n| F9 | X | Y |\n| F0 | Good | ok |\n"
        rows = self._rows(body)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].tier, "F0")


# ── slice sizing ──────────────────────────────────────────────────────────────

class TestSliceSizing(TestCase):
    def test_f1_t1_daily_ceil(self):
        # T1 daily: 7 runs/week → need ceil(n/7) per run for ≤weekly coverage
        self.assertEqual(f1_slice_size(7, "T1"), 1)
        self.assertEqual(f1_slice_size(8, "T1"), 2)

    def test_f1_t2_weekly_all(self):
        # T2 weekly: 1 run/week → include ALL F1 rows each run
        self.assertEqual(f1_slice_size(5, "T2"), 5)

    def test_f1_t3_biweekly_all(self):
        # T3 bi-weekly: 14-day interval → runs_in_period = max(1, 7//14) = 1 → all
        self.assertEqual(f1_slice_size(4, "T3"), 4)

    def test_f2_t2_weekly_quarter(self):
        # T2 weekly: ~4 runs/month → ceil(n/4) per run
        self.assertEqual(f2_slice_size(4, "T2"), 1)
        self.assertEqual(f2_slice_size(5, "T2"), 2)

    def test_f2_t1_daily(self):
        # T1 daily: 30 runs/month → ceil(n/30) per run
        self.assertEqual(f2_slice_size(30, "T1"), 1)
        self.assertEqual(f2_slice_size(31, "T1"), 2)

    def test_zero_rows_returns_zero(self):
        self.assertEqual(f1_slice_size(0, "T2"), 0)
        self.assertEqual(f2_slice_size(0, "T2"), 0)


# ── compose_checklist ─────────────────────────────────────────────────────────

class TestComposeChecklist(TestCase):
    def _make_rows(self):
        return [
            MapRow("F0", "Core A", "works", "", ""),
            MapRow("F0", "Core B", "works", "", ""),
            MapRow("F1", "Imp 1", "ok", "do X", ""),
            MapRow("F1", "Imp 2", "ok", "do Y", ""),
            MapRow("F2", "Ext 1", "ok", "", ""),
            MapRow("F2", "Ext 2", "ok", "", ""),
            MapRow("F2", "Ext 3", "ok", "", ""),
            MapRow("F2", "Ext 4", "ok", "", ""),
        ]

    def test_f0_always_included(self):
        rows = self._make_rows()
        text, nf1, nf2 = compose_checklist(rows, "T2", 0, 0)
        self.assertIn("F0 — Core journeys", text)
        self.assertIn("Core A", text)
        self.assertIn("Core B", text)

    def test_f1_t2_cursor_advances(self):
        rows = self._make_rows()
        # T2 weekly → f1_slice_size(2, "T2") = 2 (all) → cursor wraps to 0
        text, nf1, nf2 = compose_checklist(rows, "T2", 0, 0)
        self.assertIn("Imp 1", text)
        self.assertIn("Imp 2", text)
        # After all-F1 slice, cursor wraps: (0+2)%2 = 0
        self.assertEqual(nf1, 0)

    def test_f2_rotation_advances(self):
        rows = self._make_rows()
        # T2: f2_slice_size(4, "T2") = 1; cursor starts at 0 → takes Ext 1
        text, nf1, nf2 = compose_checklist(rows, "T2", 0, 0)
        self.assertIn("Ext 1", text)
        self.assertNotIn("Ext 2", text)
        self.assertEqual(nf2, 1)

    def test_f2_rotation_wraps(self):
        rows = self._make_rows()
        # cursor=3 → takes Ext 4; next = (3+1)%4 = 0
        text, nf1, nf2 = compose_checklist(rows, "T2", 0, 3)
        self.assertIn("Ext 4", text)
        self.assertEqual(nf2, 0)

    def test_no_f1_rows_no_section(self):
        rows = [MapRow("F0", "X", "ok", "", "")]
        text, nf1, nf2 = compose_checklist(rows, "T2", 0, 0)
        self.assertNotIn("F1 —", text)

    def test_empty_rows_empty_text(self):
        text, nf1, nf2 = compose_checklist([], "T2", 0, 0)
        self.assertEqual(text, "")
        self.assertEqual(nf1, 0)
        self.assertEqual(nf2, 0)


# ── fetch_target_map (mocked wiki) ────────────────────────────────────────────

@override_settings(SHIZUHA_WIKI_API_URL="http://fake-wiki/api")
class TestFetchTargetMap(TestCase):
    def _mock_client(self, search_results, page_data=None):
        client = MagicMock()
        client.search.return_value = search_results
        if page_data is not None:
            client.get_page.return_value = page_data
        return client

    @patch("tasks.qa_target_map._WikiClient")
    def test_page_absent_returns_none_none(self, MockClient):
        MockClient.return_value = self._mock_client([])
        rows, err = fetch_target_map("My Service")
        self.assertIsNone(rows)
        self.assertIsNone(err)

    @patch("tasks.qa_target_map._WikiClient")
    def test_page_found_returns_rows(self, MockClient):
        MockClient.return_value = self._mock_client(
            [{"id": 1, "title": "QA Target Map — My Service"}],
            {"body": MAP_TABLE_MD, "url": "https://wiki/page/1"},
        )
        rows, page_url = fetch_target_map("My Service")
        self.assertIsNotNone(rows)
        self.assertEqual(len(rows), 7)

    @patch("tasks.qa_target_map._WikiClient")
    def test_search_error_returns_none_errstr(self, MockClient):
        client = MagicMock()
        client.search.side_effect = Exception("connection refused")
        MockClient.return_value = client
        rows, err = fetch_target_map("My Service")
        self.assertIsNone(rows)
        self.assertIn("connection refused", err)

    @patch("tasks.qa_target_map._WikiClient")
    def test_get_page_error_returns_none_errstr(self, MockClient):
        client = MagicMock()
        client.search.return_value = [{"id": 1, "title": "QA Target Map — My Service"}]
        client.get_page.side_effect = Exception("404 not found")
        MockClient.return_value = client
        rows, err = fetch_target_map("My Service")
        self.assertIsNone(rows)
        self.assertIn("404", err)


# ── build_task_fields integration (mocked wiki) ───────────────────────────────

@override_settings(SHIZUHA_WIKI_API_URL="http://fake-wiki/api")
class TestBuildTaskFieldsWithMap(TestCase):
    def _make_svc(self):
        return _Svc("Connect", "CON", "connect", "- static checklist item")

    def _fake_project(self):
        proj = MagicMock()
        proj.pk = 1
        return proj

    @patch("tasks.qa_cadence.fetch_target_map_full")
    def test_no_map_shows_banner(self, mock_fetch):
        mock_fetch.return_value = MapFetch(exists=False)
        svc = self._make_svc()
        fields = build_task_fields("T2", svc, self._fake_project(), None)
        self.assertIn("NO TARGET MAP", fields["description"])
        self.assertIn("static checklist item", fields["description"])

    @patch("tasks.qa_cadence.fetch_target_map_full")
    def test_fetch_error_shows_error_banner(self, mock_fetch):
        mock_fetch.return_value = MapFetch(error="timed out after 8s")
        svc = self._make_svc()
        fields = build_task_fields("T2", svc, self._fake_project(), None)
        self.assertIn("fetch error", fields["description"])
        self.assertIn("timed out", fields["description"])
        self.assertIn("static checklist item", fields["description"])

    @patch("tasks.qa_cadence._build_checklist_from_map")
    def test_target_map_composition_exception_fails_loud(self, mock_build):
        mock_build.side_effect = RuntimeError("rotation table unavailable")
        svc = self._make_svc()
        fields = build_task_fields("T2", svc, self._fake_project(), None)
        self.assertIn("NO TARGET MAP", fields["description"])
        self.assertIn("fetch error", fields["description"])
        self.assertIn("target-map composition failed", fields["description"])
        self.assertIn("rotation table unavailable", fields["description"])
        self.assertIn("static checklist item", fields["description"])

    @patch("tasks.qa_cadence.fetch_target_map_full")
    def test_map_present_uses_target_map_header(self, mock_fetch):
        f0 = MapRow("F0", "Login", "auth works", "", "")
        mock_fetch.return_value = MapFetch(
            exists=True, rows=[f0], page_url="https://wiki/page/1"
        )
        svc = self._make_svc()
        fields = build_task_fields("T2", svc, self._fake_project(), None)
        self.assertIn("CVQA-L7 Target Map", fields["description"])
        self.assertIn("Login", fields["description"])
        # The map page is linked as the source of truth
        self.assertIn("https://wiki/page/1", fields["description"])
        # Static checklist should NOT appear when map is used
        self.assertNotIn("static checklist item", fields["description"])

    @patch("tasks.qa_cadence.fetch_target_map_full")
    def test_rotation_cursor_persists(self, mock_fetch):
        f1a = MapRow("F1", "A", "ok", "", "")
        f1b = MapRow("F1", "B", "ok", "", "")
        f2a = MapRow("F2", "X", "ok", "", "")
        f2b = MapRow("F2", "Y", "ok", "", "")
        mock_fetch.return_value = MapFetch(exists=True, rows=[f1a, f1b, f2a, f2b])
        svc = self._make_svc()
        proj = self._fake_project()

        # First run: cursors at 0
        build_task_fields("T2", svc, proj, None)
        # T2 weekly: f1_slice=2 (all), wraps to 0; f2_slice=ceil(2/4)=1, cursor→1
        self.assertEqual(
            QaCadenceRotation.objects.get(project_key="CON", tier="F2").cursor, 1
        )

        # Second run: f2 cursor at 1 → takes Y
        build_task_fields("T2", svc, proj, None)
        self.assertEqual(
            QaCadenceRotation.objects.get(project_key="CON", tier="F2").cursor, 0
        )  # wraps: (1+1)%2=0


# ── real-world map formats (2026-07-14 parser fix) ───────────────────────────
# The canonical QA-space maps are written by QA agents walking the live
# product and use tier SECTIONS + varied table headers — not the canonical
# `| Tier | Feature | ... |` form. The parser must consume all of them.

CORTEX_STYLE_MD = """\
# QA Target Map — Example (service)

## Tier legend
| Tier | Cadence |
|---|---|
| F0 | every run |

## F0 — critical journeys (every run)
### F0.1 — `/v1/models` reachable
Some prose about the journey.
### F0.2 — Auth gate on inference
More prose.

## F1 — core (weekly rotation)
| Journey | Expectation | Recipe | Verdict |
|---|---|---|---|
| API-key issuance | key works on /v1/* | dashboard flow | pending |
| Rate-limit 429 | correct Retry-After | burst calls | pending |

## F2 — long-tail (monthly)
| Journey | Verdict |
|---|---|
| Dashboard truthfulness | pending |

## Execution log
| Date (UTC) | Journey | Verdict | Bugs | Evidence |
|---|---|---|---|---|
| 2026-07-03 | F0.1 models | PASS | — | probes |
"""

PULSE_STYLE_MD = """\
# QA Target Map — Example

## Live test matrix — F0 (every run)
### F0.1 Board + DB consistency (critical)
### F0.2 Task lifecycle: create/edit/transition

## F1 rotation (weekly)
| ID | Surface | Expectation | Recipe | Verdict |
|---|---|---|---|---|
| F1.1 | Project list + search | filters consistent | open /projects | PASS |
| F1.2 | Admin ops | toggles auditable | /admin | PASS |

## Bugs found during verification
| ID | Surface | Issue | Severity | Pulse Task |
|---|---|---|---|---|
| BUG-1 | Notifications | 404 on task key | high | PLS-1 |
"""


class TestRealWorldMapFormats(TestCase):
    def test_cortex_style_sections_and_tables(self):
        rows = _parse_map_table(CORTEX_STYLE_MD, "https://w/p")
        tiers = {}
        for r in rows:
            tiers.setdefault(r.tier, []).append(r.feature)
        # F0 journeys come from the numbered sub-headings
        self.assertEqual(len(tiers.get("F0", [])), 2)
        self.assertIn("Auth gate on inference", tiers["F0"])
        # F1 table under the F1 section heading
        self.assertEqual(len(tiers.get("F1", [])), 2)
        self.assertIn("API-key issuance", tiers["F1"])
        # 2-column F2 table still yields feature rows
        self.assertEqual(tiers.get("F2"), ["Dashboard truthfulness"])
        # Execution log + tier legend rows are NEVER checklist rows
        all_features = [r.feature for r in rows]
        self.assertNotIn("2026-07-03", all_features)
        self.assertNotIn("F0", all_features)

    def test_pulse_style_row_ids_and_bug_table_excluded(self):
        rows = _parse_map_table(PULSE_STYLE_MD, "")
        f0 = [r.feature for r in rows if r.tier == "F0"]
        f1 = [r.feature for r in rows if r.tier == "F1"]
        self.assertEqual(len(f0), 2)
        self.assertIn("Task lifecycle: create/edit/transition", f0)
        self.assertEqual(f1, ["Project list + search", "Admin ops"])
        # BUG-table rows carry no F-tier id and sit outside tier sections
        self.assertNotIn("Notifications", [r.feature for r in rows])

    def test_two_col_row_has_clean_checklist_line(self):
        rows = _parse_map_table(CORTEX_STYLE_MD, "")
        f2 = next(r for r in rows if r.tier == "F2")
        line = f2.checklist_line()
        self.assertIn("Dashboard truthfulness", line)
        self.assertFalse(line.rstrip().endswith(":"))


class TestNormalizeTitle(TestCase):
    def test_dash_case_whitespace_variants_match(self):
        variants = [
            "QA Target Map — Cortex",
            "QA Target Map - Cortex",
            "QA  Target Map – CORTEX",
        ]
        self.assertEqual(len({_normalize_title(v) for v in variants}), 1)


# ── service-token auth (2026-07-14 wiki-auth fix) ────────────────────────────

class TestWikiServiceAuth(TestCase):
    @override_settings(
        SECRET_KEY="django-insecure-change-this-in-production", SIMPLE_JWT={}
    )
    def test_insecure_key_raises_and_reports_error_not_absent(self):
        with self.assertRaises(WikiAuthUnavailable):
            _mint_service_token()
        # An auth failure must surface as a fetch ERROR (banner), never as
        # "page absent" — absence is what silently degraded every cadence
        # task to the static smoke checklist at launch.
        result = fetch_target_map_full("QA Target Map — Anything")
        self.assertIsNotNone(result.error)
        self.assertFalse(result.exists)

    @override_settings(
        SECRET_KEY="test-signing-key-1234567890",
        SIMPLE_JWT={"SIGNING_KEY": "test-signing-key-1234567890"},
    )
    def test_token_carries_service_claims(self):
        import jwt as pyjwt

        token = _mint_service_token()
        payload = pyjwt.decode(
            token,
            "test-signing-key-1234567890",
            algorithms=["HS256"],
            options={"verify_aud": False},
        )
        self.assertTrue(payload["is_agent"])
        self.assertEqual(payload["enabled_services"], ["wiki"])
        self.assertEqual(payload["organization_memberships"], {"1": "member"})
        self.assertEqual(payload["email"], "pulse-qa-cadence@shizuha.com")

    @override_settings(
        SECRET_KEY="test-signing-key-1234567890",
        SIMPLE_JWT={"SIGNING_KEY": "test-signing-key-1234567890"},
    )
    def test_client_sends_bearer_header(self):
        from tasks.qa_target_map import _WikiClient

        client = _WikiClient()
        auth = client._session.headers.get("Authorization", "")
        self.assertTrue(auth.startswith("Bearer "))
