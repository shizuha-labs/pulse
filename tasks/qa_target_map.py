"""CVQA-L7: QA Target Map — wiki fetch and F0/F1/F2 row parser.

Fetches a ``QA Target Map — <Project>`` wiki page from shizuha-wiki and
parses the markdown table of F0/F1/F2 tier rows so ``qa_cadence.py`` can
compose a richer, rotating checklist instead of the static CVQA-L2 lists.
"""
from __future__ import annotations

import math
import re
import logging
from typing import Optional

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

# Tier cadence: how many days between consecutive runs (used for slice sizing).
_CADENCE_INTERVAL_DAYS = {"T1": 1, "T2": 7, "T3": 14, "T4": 28}

# Every F1 row must appear ≤weekly; every F2 row ≤monthly.
_F1_MAX_PERIOD_DAYS = 7
_F2_MAX_PERIOD_DAYS = 30


def f1_slice_size(n_rows: int, tier_key: str) -> int:
    """Number of F1 rows to include per run so all are covered within 7 days."""
    interval = _CADENCE_INTERVAL_DAYS.get(tier_key, 7)
    runs_in_period = max(1, _F1_MAX_PERIOD_DAYS // interval)
    return max(1, math.ceil(n_rows / runs_in_period)) if n_rows else 0


def f2_slice_size(n_rows: int, tier_key: str) -> int:
    """Number of F2 rows to include per run so all are covered within 30 days."""
    interval = _CADENCE_INTERVAL_DAYS.get(tier_key, 7)
    runs_in_period = max(1, _F2_MAX_PERIOD_DAYS // interval)
    return max(1, math.ceil(n_rows / runs_in_period)) if n_rows else 0


# ── Banners ──────────────────────────────────────────────────────────────────

def no_map_banner(project_name: str) -> str:
    return (
        f"**NO TARGET MAP — coverage is smoke-only; discovery task required.** "
        f"No `QA Target Map — {project_name}` wiki page exists yet. "
        f"Run coverage is limited to the CVQA-L2 static checklist below.\n\n"
    )


def fetch_error_banner(project_name: str, reason: str) -> str:
    return (
        f"**NO TARGET MAP (fetch error) — coverage is smoke-only; discovery task required.** "
        f"Wiki fetch failed for `QA Target Map — {project_name}`: {reason}. "
        f"Run coverage is limited to the CVQA-L2 static checklist below.\n\n"
    )


# ── Row ──────────────────────────────────────────────────────────────────────

class MapRow:
    __slots__ = ("tier", "feature", "expectation", "recipe", "page_url")

    def __init__(self, tier: str, feature: str, expectation: str,
                 recipe: str, page_url: str = ""):
        self.tier = tier          # "F0", "F1", "F2"
        self.feature = feature
        self.expectation = expectation
        self.recipe = recipe
        self.page_url = page_url  # deep-link into the map page

    def checklist_line(self) -> str:
        label = f"[{self.feature}]({self.page_url})" if self.page_url else self.feature
        text = f"- {label}: {self.expectation}" if self.expectation else f"- {label}"
        if self.recipe:
            text += f" — _{self.recipe}_"
        return text


# ── Parser ────────────────────────────────────────────────────────────────────

# Section heading carrying a bare tier token ("## F0 — critical journeys",
# "## Live test matrix — F0 (every run)", "## F1 rotation (weekly)").
_TIER_SECTION_RE = re.compile(r"^#{1,6}\s.*\bF([0-2])\b(?!\.\d)")
# Feature heading with a numbered tier id ("### F0.1 — Fresh-tenant provision",
# "### F0.2 Task lifecycle: ...").
_TIER_FEATURE_HEADING_RE = re.compile(r"^#{1,6}\s*F([0-2])\.\d+\s*[—–:\-]?\s*(.+)$")
# Row-level tier id in a cell ("F1", "F1.3").
_ROW_TIER_RE = re.compile(r"^F([0-2])(?:\.\d+)?$")

_FEATURE_COL_NAMES = ("journey", "feature", "surface", "item")
_EXPECTATION_COL_NAMES = ("expectation", "expected", "behavior")
_RECIPE_COL_NAMES = ("recipe", "verification", "steps", "how")


def _row_anchor(feature: str, page_url: str) -> str:
    anchor = re.sub(r"[^a-z0-9]+", "-", feature.lower()).strip("-")
    return f"{page_url}#{anchor}" if page_url else ""


def _named_col(header_cols: list[str], names: tuple[str, ...]) -> Optional[int]:
    for name in names:
        for i, h in enumerate(header_cols):
            if name in h:
                return i
    return None


def _parse_map_table(body: str, page_url: str) -> list[MapRow]:
    """Parse F0/F1/F2 rows from a QA Target Map page body (CVQA-L7).

    Real maps are written by QA agents and vary (walked live, not generated):
    tier sections as headings (``## F0 — critical journeys``), F0 items as
    numbered sub-headings (``### F0.2 — Claw isolation``), F1/F2 tables headed
    ``| Journey | Expectation | Recipe | Verdict |`` or ``| ID | Surface | ... |``
    with per-row ids like ``F1.3``, plus non-target tables (execution logs,
    tier legends, bug lists) that must NOT become checklist rows. This parser
    accepts all of those and the canonical ``| Tier | Feature | ... |`` form.
    """
    rows: list[MapRow] = []
    section_tier: Optional[str] = None   # tier context from the nearest heading
    header_cols: list[str] = []
    table_active = False                 # inside a parseable table's data rows
    separator_seen = False

    for raw_line in body.splitlines():
        line = raw_line.strip()

        if line.startswith("#"):
            table_active = False
            separator_seen = False
            header_cols = []
            feat = _TIER_FEATURE_HEADING_RE.match(line)
            if feat:
                # Numbered heading IS a feature row (typical for F0 journeys);
                # scenario detail lives in the section body — link to it.
                tier = f"F{feat.group(1)}"
                feature = feat.group(2).strip().rstrip("#").strip()
                if feature:
                    rows.append(MapRow(
                        tier=tier,
                        feature=feature,
                        expectation="",
                        recipe="see map section",
                        page_url=_row_anchor(feature, page_url),
                    ))
                # numbered features keep (and imply) their section's tier
                section_tier = tier
                continue
            sec = _TIER_SECTION_RE.match(line)
            section_tier = f"F{sec.group(1)}" if sec else None
            continue

        if not (line.startswith("|") and line.endswith("|")):
            table_active = False
            separator_seen = False
            header_cols = []
            continue

        cells = [c.strip() for c in line.strip("|").split("|")]
        lower_row = [c.lower() for c in cells]

        # Header row of a new table
        if not table_active:
            if separator_seen is False and header_cols == []:
                # A table is parseable when it either sits inside a tier
                # section, carries an explicit tier/id column, or uses the
                # canonical Tier-first form. Tables with a date column are
                # execution logs — never checklist rows.
                has_date = any("date" in c for c in lower_row)
                has_tier_col = any(c in ("tier", "id") for c in lower_row)
                has_feature_col = _named_col(lower_row, _FEATURE_COL_NAMES) is not None
                if not has_date and (
                    section_tier is not None or (has_tier_col and has_feature_col)
                ):
                    header_cols = lower_row
                continue

        # Separator row
        if header_cols and not separator_seen:
            if all(re.match(r"^[-:|]+$", c) for c in cells if c):
                separator_seen = True
                table_active = True
                continue
            # header without separator — not a markdown table; reset
            header_cols = []
            continue

        # Data rows
        if table_active and header_cols:
            if len(cells) < 2:
                continue

            # Row tier: explicit id/tier cell wins; else section context.
            tier = None
            tier_idx = next(
                (i for i, h in enumerate(header_cols) if h in ("tier", "id")), None
            )
            if tier_idx is not None and tier_idx < len(cells):
                m = _ROW_TIER_RE.match(cells[tier_idx].upper())
                if m:
                    tier = f"F{m.group(1)}"
            if tier is None:
                tier = section_tier
            if tier is None:
                continue

            feat_idx = _named_col(header_cols, _FEATURE_COL_NAMES)
            if feat_idx is None:
                # positional fallback: first non-tier column
                feat_idx = 1 if tier_idx == 0 and len(cells) > 1 else 0
            feature = cells[feat_idx] if feat_idx < len(cells) else ""
            if not feature or re.match(r"^[-:\s]*$", feature):
                continue

            exp_idx = _named_col(header_cols, _EXPECTATION_COL_NAMES)
            rec_idx = _named_col(header_cols, _RECIPE_COL_NAMES)
            expectation = cells[exp_idx] if exp_idx is not None and exp_idx < len(cells) else ""
            recipe = cells[rec_idx] if rec_idx is not None and rec_idx < len(cells) else ""

            rows.append(MapRow(
                tier=tier,
                feature=feature,
                expectation=expectation,
                recipe=recipe,
                page_url=_row_anchor(feature, page_url),
            ))

    return rows


# ── Wiki client ───────────────────────────────────────────────────────────────

class WikiAuthUnavailable(Exception):
    """Raised when a wiki service token cannot be minted (missing/insecure key)."""


def _mint_service_token() -> str:
    """Mint an HS256 service JWT the wiki accepts (PLS-238 auth fix).

    Wiki validates HS256 tokens against the platform-shared ``JWT_SECRET_KEY``
    (same ``shizuha-secrets/jwt-secret`` both deployments mount). The QA space
    is org-visibility, so an unauthenticated search returns zero results and
    every Target Map looks "absent" — auth here is what connects CVQA-L7 maps
    to the cadence generator at all.

    Claims: ``is_agent`` puts wiki on the FederatedUser path (no local user row
    needed); ``organization_memberships`` grants org-scoped READ; explicit
    ``enabled_services`` satisfies ServiceAccessPermission.
    """
    signing_key = settings.SIMPLE_JWT.get("SIGNING_KEY") or settings.SECRET_KEY
    if not signing_key or "django-insecure" in signing_key:
        raise WikiAuthUnavailable(
            "wiki service auth not configured: JWT_SECRET_KEY missing or insecure default"
        )
    import time
    import uuid

    import jwt as pyjwt

    org_id = str(getattr(settings, "SHIZUHA_WIKI_SERVICE_ORG_ID", 1))
    now = int(time.time())
    payload = {
        # SimpleJWT AccessToken contract on the wiki side
        "token_type": "access",
        "exp": now + 3600,
        "iat": now,
        "jti": uuid.uuid4().hex,
        # Shizuha federated-identity claims
        "user_id": int(getattr(settings, "SHIZUHA_WIKI_SERVICE_USER_ID", 0)),
        "email": "pulse-qa-cadence@shizuha.com",
        "username": "pulse-qa-cadence",
        "is_agent": True,
        "enabled_services": ["wiki"],
        "organization_memberships": {org_id: "member"},
        "aud": ["wiki"],
    }
    token = pyjwt.encode(payload, signing_key, algorithm="HS256")
    return token if isinstance(token, str) else token.decode("utf-8")


def _normalize_title(title: str) -> str:
    """Compare titles tolerantly: unify dash variants, collapse whitespace, casefold."""
    text = re.sub(r"[‐-―−-]+", "-", title or "")
    return re.sub(r"\s+", " ", text).strip().casefold()


class _WikiClient:
    def __init__(self) -> None:
        self._base = getattr(
            settings, "SHIZUHA_WIKI_API_URL", "http://shizuha-wiki:8013/api"
        ).rstrip("/")
        self._session = requests.Session()
        self._session.headers.update({
            "Content-Type": "application/json",
            "Authorization": f"Bearer {_mint_service_token()}",
        })
        self._timeout = 8

    def search(self, query: str, space_key: str = "") -> list[dict]:
        """Search pages, optionally scoped to one space, following pagination.

        The wiki's SearchFilter is unranked and pages at PAGE_SIZE=20, so a
        global title search can bury the exact match beyond page 1. Scoping to
        the canonical QA space keeps the candidate set tiny; following `next`
        (bounded) covers it regardless.
        """
        params = {"search": query}
        if space_key:
            params["space_key"] = space_key
        results: list[dict] = []
        url = f"{self._base}/pages/"
        for page_no in range(5):  # bound: 100 candidates is plenty
            resp = self._session.get(
                url,
                params=params if page_no == 0 else None,
                timeout=self._timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict):
                results.extend(data.get("results", []))
                url = data.get("next")
            else:
                results.extend(data)
                url = None
            if not url:
                break
        return results

    def get_page(self, page_id) -> dict:
        resp = self._session.get(
            f"{self._base}/pages/{page_id}/",
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp.json()


# ── Public interface ──────────────────────────────────────────────────────────

class MapFetch:
    """Outcome of a Target Map wiki lookup, incl. freshness metadata."""

    __slots__ = ("exists", "rows", "page_url", "updated_at", "error")

    def __init__(self, exists=False, rows=None, page_url="",
                 updated_at=None, error=None):
        self.exists = exists          # page found (rows may still be empty)
        self.rows = rows or []
        self.page_url = page_url
        self.updated_at = updated_at  # datetime | None — for staleness checks
        self.error = error            # str | None — fetch/auth failure


def _public_page_url(page: dict) -> str:
    base = getattr(
        settings, "SHIZUHA_WIKI_PUBLIC_URL", "https://shizuha.com/wiki"
    ).rstrip("/")
    space_key = page.get("space_key")
    slug = page.get("slug")
    if space_key and slug:
        return f"{base}/spaces/{space_key}/pages/{slug}"
    return page.get("url") or page.get("html_url") or ""


def fetch_target_map_full(map_title: str) -> MapFetch:
    """Fetch and parse the Target Map wiki page titled ``map_title``.

    Auth failures and wiki errors come back as ``MapFetch(error=...)`` — never
    as "absent" — so an unreachable/unauthenticated wiki can't silently
    degrade every cadence task to the smoke checklist (the PLS-238 launch bug).
    """
    try:
        client = _WikiClient()
    except WikiAuthUnavailable as exc:
        logger.warning("PLS-238 wiki auth unavailable: %s", exc)
        return MapFetch(error=str(exc))

    space_key = getattr(settings, "SHIZUHA_WIKI_QA_SPACE_KEY", "QA")
    try:
        results = client.search(map_title, space_key=space_key)
    except Exception as exc:
        logger.warning("PLS-238 wiki search failed for %r: %s", map_title, exc)
        return MapFetch(error=str(exc))

    want = _normalize_title(map_title)
    match = next(
        (r for r in results
         if _normalize_title(r.get("title") or "") == want),
        None,
    )
    if not match:
        return MapFetch(exists=False)  # page genuinely absent

    try:
        page = client.get_page(match.get("id") or match.get("page_id"))
    except Exception as exc:
        logger.warning("PLS-238 wiki get_page failed for %r: %s", map_title, exc)
        return MapFetch(error=str(exc))

    # Wiki detail serializer exposes content_text (markdown) — the historical
    # body/content keys never existed on this API.
    body = (
        page.get("content_text") or page.get("body") or page.get("content") or ""
    )
    page_url = _public_page_url(page)
    updated_at = None
    raw_updated = page.get("updated_at")
    if raw_updated:
        from django.utils.dateparse import parse_datetime
        updated_at = parse_datetime(str(raw_updated))
    rows = _parse_map_table(body, page_url)
    return MapFetch(exists=True, rows=rows, page_url=page_url,
                    updated_at=updated_at)


def fetch_target_map(project_name: str) -> tuple[Optional[list[MapRow]], Optional[str]]:
    """Back-compat wrapper: fetch by project name (title = ``QA Target Map — <name>``).

    Returns:
        ``(rows, page_url)`` on success (rows may be empty if the page has no
        valid F0/F1/F2 table yet).
        ``(None, None)`` when the page genuinely does not exist.
        ``(None, error_str)`` on any fetch/parse error (caller shows the
        fetch-error banner).
    """
    result = fetch_target_map_full(f"QA Target Map — {project_name}")
    if result.error is not None:
        return None, result.error
    if not result.exists:
        return None, None
    return result.rows, result.page_url


def compose_checklist(
    rows: list[MapRow],
    tier_key: str,
    f1_cursor: int,
    f2_cursor: int,
) -> tuple[str, int, int]:
    """Compose the F0+rotating-F1/F2 checklist markdown and return the new cursors.

    Args:
        rows: All MapRow objects from the target map.
        tier_key: e.g. "T2" — determines the slice sizes.
        f1_cursor: Current rotation cursor for F1 rows (offset into f1_rows).
        f2_cursor: Current rotation cursor for F2 rows (offset into f2_rows).

    Returns:
        ``(checklist_markdown, new_f1_cursor, new_f2_cursor)``
    """
    f0_rows = [r for r in rows if r.tier == "F0"]
    f1_rows = [r for r in rows if r.tier == "F1"]
    f2_rows = [r for r in rows if r.tier == "F2"]

    lines: list[str] = []

    if f0_rows:
        lines.append("#### F0 — Core journeys (every run)")
        lines.extend(r.checklist_line() for r in f0_rows)

    new_f1_cursor = f1_cursor
    if f1_rows:
        k = f1_slice_size(len(f1_rows), tier_key)
        start = f1_cursor % len(f1_rows)
        # Rotate: wrap around if slice exceeds end
        if start + k <= len(f1_rows):
            slice_rows = f1_rows[start:start + k]
        else:
            slice_rows = f1_rows[start:] + f1_rows[: (start + k) % len(f1_rows)]
        new_f1_cursor = (f1_cursor + k) % len(f1_rows)
        lines.append(
            f"\n#### F1 — Important journeys (rotating; {start + 1}–"
            f"{min(start + k, len(f1_rows))} of {len(f1_rows)})"
        )
        lines.extend(r.checklist_line() for r in slice_rows)

    new_f2_cursor = f2_cursor
    if f2_rows:
        k = f2_slice_size(len(f2_rows), tier_key)
        start = f2_cursor % len(f2_rows)
        if start + k <= len(f2_rows):
            slice_rows = f2_rows[start:start + k]
        else:
            slice_rows = f2_rows[start:] + f2_rows[: (start + k) % len(f2_rows)]
        new_f2_cursor = (f2_cursor + k) % len(f2_rows)
        lines.append(
            f"\n#### F2 — Extended journeys (rotating; {start + 1}–"
            f"{min(start + k, len(f2_rows))} of {len(f2_rows)})"
        )
        lines.extend(r.checklist_line() for r in slice_rows)

    return "\n".join(lines), new_f1_cursor, new_f2_cursor
