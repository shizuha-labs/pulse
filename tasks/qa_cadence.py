"""CVQA-L2 — per-project standing QA verification cadence (PLAT-474 / PLAT-552).

Data + helpers for the ``qa_verification_sweep`` Celery beat task (defined in
``tasks/tasks.py``). The sweep runs DAILY and, on each run, generates per-project
QA verification tasks for whichever risk tiers are due that day:

    T1 (daily)     — every run
    T2 (weekly)    — Mondays
    T3 (bi-weekly) — Mondays of even ISO weeks
    T4 (monthly)   — the 1st of the month

Full spec + per-service checklists: Engineering wiki
"CVQA-L2: Per-Project Standing QA Verification Cadence (PLAT-474)"
(page 1c481d6b-1abc-4808-84da-f5e811837e17).
"""
from __future__ import annotations

import logging

from tasks.qa_target_map import (
    compose_checklist,
    fetch_error_banner,
    fetch_target_map_full,
    no_map_banner,
)

logger = logging.getLogger(__name__)

# CVQA-L8 (operator directive 2026-07-11): every cadence run ALSO scrutinizes the
# commit delta since the last QA pass — tracked in the project's "QA Ledger" page
# in the dedicated QA wiki space. The step is embedded in every generated task so
# the delta pass is mechanical, not memory-dependent.
DELTA_WIKI_REF = 'wiki "CVQA-L8: Delta QA" (QA space, e4931509-9064-4887-beb7-999ea48c6836)'
DELTA_QA_STEP = """### Delta-QA (CVQA-L8) — scrutinize what changed since the last pass
1. Open wiki `QA Ledger — <this project>` in the **QA** space (create from the L8 template on first run; seed pointer = current origin HEAD + do a full checklist pass).
2. `git log --stat <last_tested_commit>..origin/<default-branch>` (GitHub CLI, read-only). Empty delta -> record "no changes" in the ledger and continue.
3. Classify commits: **risky (auth/tenancy/quota/money/migrations/deploy/deps) -> mandatory deep pass**; user-facing -> run the affected Target Map rows' live recipes AS A USER even if outside today's rotation; infra/docs-only -> smoke. A change that maps to NO Target Map row = map gap -> add the row.
4. Run ONE timeboxed (<=45m) exploratory charter around the highest-risk change (SBTM; session notes on the ledger).
5. Bug-fix commits: verify the fix live AND probe its blast radius.
6. Update the ledger: pointer -> new HEAD, delta entry (classification, rows exercised, charter notes, findings). Moving the pointer without evidence = failed run. Route findings via workflows: functional -> autonomous-bug (owning team) · security smell -> security-finding · UX friction -> design discovery · expectation ambiguity -> Product ratifies the map row."""

WIKI_REF = (
    "Engineering wiki — \"CVQA-L2: Per-Project Standing QA Verification Cadence "
    "(PLAT-474)\" (1c481d6b-1abc-4808-84da-f5e811837e17)"
)

# Marker used for idempotent dedup of auto-generated cadence tasks.
QA_CADENCE_SOURCE = "qa_cadence_sweep"

# Fallback project for sub-services that don't have their own Pulse project
# (per PLAT-474 §7 / aoi: sub-services are tracked under Shizuha Platform via
# labels, not separate projects). Resolved by key first, then by name.
PLATFORM_PROJECT_KEY = "PLAT"
PLATFORM_PROJECT_NAME = "Shizuha Platform"

# Statuses that count as "this cadence task is finished" — a fresh one may be
# generated next window. Anything else open means a task is already in flight.
TERMINAL_STATUSES = frozenset({
    "done", "completed", "closed", "cancelled", "canceled",
    "resolved", "rejected", "duplicate", "wont_fix", "merged", "failed",
})


class _Svc:
    """One service in the cadence: display name, owning Pulse project key, slug."""

    __slots__ = ("name", "project_key", "slug", "checklist", "map_title")

    def __init__(self, name: str, project_key: str, slug: str, checklist: str,
                 map_title: str = ""):
        self.name = name
        # Owning Pulse project key; falls back to PLATFORM_PROJECT_KEY at
        # resolution time if no project with this key exists.
        self.project_key = project_key
        self.slug = slug
        self.checklist = checklist.strip()
        # Exact wiki page title of this service's CVQA-L7 Target Map. The
        # canonical pages in the QA space are NOT all named after the cadence
        # display name (e.g. "QA Target Map — Connect", not "... — Shizuha
        # Connect"), so the lookup title is explicit.
        self.map_title = map_title or f"QA Target Map — {name}"


# ── Tier → cadence → services. Mirrors wiki §2/§3. ──────────────────────────
# Checklists are the actionable core of each wiki §3 entry (kept concise; the
# wiki page is the authority and is linked into every generated task).
QA_TIERS = {
    "T1": {
        "label": "Core daily-use",
        "cadence": "daily",
        "services": [
            _Svc("Shizuha ID / Auth", "PLAT", "id-auth", """
- `GET /id/api/health/` -> 200
- Login: username+password -> token issued -> /admin dashboard loads
- Org chart `/admin/org-chart` loads + shows current agents
- Token refresh works without re-login
""", map_title="QA Target Map — Shizuha ID"),
            _Svc("Shizuha Connect", "CON", "connect", """
- `GET /api/feed/` (auth) -> 200 + posts present
- Compose + send a DM -> recipient receives in conversation
- DM WebSocket: connect, ping -> pong within 5s
- `message_user` MCP: DM to self -> delivered + visible
""", map_title="QA Target Map — Connect"),
            _Svc("Shizuha Pulse", "PLS", "pulse", """
- `GET /api/tasks/` (auth) -> 200 + tasks listed
- Create a test task -> appears in the list
- Transition test task open -> in_progress -> done (clean round-trip)
- `pulse_get_my_tasks` MCP -> valid list
""", map_title="QA Target Map — Pulse"),
            _Svc("Kura", "KURA", "kura", """
- `GET /api/sources/` on the configured source endpoint -> 200, sources listed
- `GET /api/sources/` on s1:9400 -> 200, sources listed
- >=1 source per hub `last_status` in {completed, success, partial} within freshness window
- No source in `failed` (if any: file bug immediately to owning team)
"""),
            _Svc("Shizuha Platform UI", "PLAT", "platform-ui", """
- `GET ${PLATFORM_PUBLIC_URL}/` -> 200 + title "Shizuha"
- `GET https://shizuha.com/` -> 200 + home dashboard (org-progress charts render)
- Login + session persists on authenticated page load
- Connect/Pulse/Wiki/Admin nav all load without 500
""", map_title="QA Target Map — Shizuha Platform (cross-cutting infra)"),
            _Svc("Dojo", "DOJO", "dojo", """
- Dojo landing `shizuha.com/connect/dojo` loads logged-in (enrolled view, reports/sessions counters, course units)
- Practice `shizuha.com/connect/dojo/practice`: Start interview -> session created FAST (<5s, not a 30s hang) -> opener is a safe user-facing question (NO internal prompt/instructions/scratchpad leak)
- Submit an answer -> interviewer follow-up arrives (bounded wait); session completes -> report generated
- Resume builder `shizuha.com/connect/dojo/resume`: create a profile -> assistant draft/improve returns bullets
- Coding platform `shizuha.com/connect/dojo/code`: open a problem -> editor loads -> Run a solution -> judge returns a verdict (pass/fail/runtime), no 500
- Course content: unit lessons/learn sections render without 500
- Entitlement/quota: Free 1-active-session cap enforced server-side (402 + reason_code); paid-intent funnel shows waitlist/payment-link CTA (NEVER 'Pay now')
""", map_title="QA Target Map — Dojo"),
        ],
    },
    "T2": {
        "label": "Active + changing",
        "cadence": "weekly",
        "services": [
            _Svc("Cortex", "CTX", "cortex", """
- `GET /cortex/nodes` -> 200 + >=1 node
- `GET /cortex/backends` -> 200 + >=1 backend ready
- Live inference: POST 5-token completion to an active backend -> response within 30s
- `/cortex/nodes` count matches `kubectl get nodes` (weekly cross-check)
"""),
            _Svc("Shizuha Cloud Services", "SCS", "scs", """
- `GET /scs/api/health/` -> 200 {"status": "healthy"} (revived 2026-07-11; public path is /scs, NOT /shizuha-scs)
- `GET /scs/api/hosting/apps/` (auth) -> 200 + caller's apps listed
- App-hosting round-trip: deploy a tiny bundle (scs_deploy_app MCP or POST /scs/api/hosting/apps/<slug>/deploy/) -> https://<slug>.apps.shizuha.com serves the new index.html live
- `GET /scs/api/hosting/quota/` -> 200, reserved_bytes <= limit_bytes
- `GET /scs/api/compute/jobs/` (auth) -> 200
""", map_title="QA Target Map — SCS"),
            _Svc("HIVE", "HIVE", "hive", """
- Agent roster (`/admin/roster` or `admin_list_employees`) -> non-empty
- >=1 agent responded to a heartbeat within last 2 windows
- MCP reachable: `pulse_list_projects` returns valid response
"""),
            _Svc("Shizuha Wiki", "PLAT", "wiki", """
- `GET /wiki/api/spaces/` -> 200 + spaces listed
- Create test page -> appears in space tree -> delete it
- `wiki_search_pages` MCP -> valid response for a known keyword
""", map_title="QA Target Map — Wiki"),
            _Svc("Shizuha Drive", "PLAT", "drive", """
- `GET /drive/api/files/` (auth) -> 200
- Upload a small test file -> appears in listing -> delete it
""", map_title="QA Target Map — Drive"),
            _Svc("Shizuha CLI", "SCLI", "cli", """
- `shizuha --version` -> prints version, exit 0
- `shizuha pulse list` (or equivalent) -> valid output, exit 0
- `shizuha auth whoami` -> current user identity
""", map_title="QA Target Map — SCLI"),
            _Svc("Shizuha Android App", "PLAT", "android", """
- Launch on emulator -> no crash on cold start
- Login via Shizuha ID -> home screen loads
- Connect feed visible + >=1 post
- Compose a test message -> sent successfully
"""),
            _Svc("Shizuha Search", "SRCH", "search", """
- `GET /search/api/health/` (or service health endpoint) -> 200
- Authenticated query for a known keyword -> results within 2s, no 500
- Cross-service result (wiki page or pulse task) appears for a known-indexed term
""", map_title="QA Target Map — Search"),
            _Svc("Shizuha Origin", "ORIG", "origin", """
- Origin (git.shizuha.com) login page -> 200; API `/api/v1/version` -> 200
- Repo browse: a known private repo loads for an authorized user
- Actions: latest CI run for an active repo visible + logs readable
""", map_title="QA Target Map — Shizuha Origin (Forgejo dev platform)"),
            _Svc("Shizuha Admin", "PLAT", "admin", """
- `GET /admin/api/health/` (or `/admin/` shell) -> 200 authenticated
- Org chart loads + reflects current Pulse roster (teams/members present)
- `admin_list_teams` MCP -> non-empty, matches UI
"""),
        ],
    },
    "T3": {
        "label": "Dev / low-traffic",
        "cadence": "bi-weekly",
        "services": [
            _Svc("Shizuha Books", "BKS", "books", """
- `GET /books/api/` (auth) -> 200
- Create a test ledger entry -> appears -> delete it
- PDF/report generation: trigger a test report -> generates without error
""", map_title="QA Target Map — Books"),
            _Svc("Shizuha Notes", "PLAT", "notes", """
- `GET /notes/api/notes/` -> 200 + notes listed
- Create a test note -> appears -> delete it
""", map_title="QA Target Map — Notes"),
            _Svc("Shizuha Mail", "MAIL", "mail", """
- `GET /mail/api/` -> 200 (service running)
- Send a test email via Stalwart -> confirm delivery
""", map_title="QA Target Map — Mail"),
            _Svc("Shizuha Finance", "FIN", "finance", """
- Finance API health check -> 200
- One key flow (invoice list) -> non-empty, no 500
""", map_title="QA Target Map — Finance"),
            _Svc("Shizuha Browser", "BRW", "browser", """
- Browser control endpoint reachable
- Launch a page -> screenshot returned without error
""", map_title="QA Target Map — Browser"),
            _Svc("Shizuha Forge", "FRG", "forge", """
- Forge public entry (shizuha.com/forge) -> 200
- Authenticated generate flow: submit a tiny prompt -> asset/result produced, no 500
- Quota surface reflects the run (no silent unmetered use)
""", map_title="QA Target Map — Forge"),
            _Svc("Shizuha Inventory", "INV", "inventory", """
- `GET /inventory/api/` (auth) -> 200
- Item CRUD round-trip: create item -> appears in list -> delete it
- Stock movement: receive -> stock level reflects -> issue back out
""", map_title="QA Target Map — Inventory"),
        ],
    },
    "T4": {
        "label": "Infra / research",
        "cadence": "monthly",
        "services": [
            _Svc("Hikari (model training)", "HKR", "hikari", """
- No crashed training runs in the past month (logs / dashboard)
- Latest checkpoint accessible from the expected storage path
"""),
            _Svc("Headscale (VPN)", "PLAT", "headscale", """
- `tailscale status` -> all known nodes connected (the deployment inventory)
- Peer reachability: ping a configured peer from the gateway -> <10ms
"""),
            _Svc("shizuha-tasks (orchestration)", "PLAT", "shizuha-tasks", """
- No tasks stuck `in_progress` >24h without a heartbeat (flag to ops)
- Celery beat scheduled tasks firing on expected cadence (beat logs)
"""),
            _Svc("kura-beta (backup binary)", "KURA", "kura-beta", """
- Deployed binary version on the configured hosts matches the expected tag
- Run a dry-run backup if available, or confirm last scheduled run succeeded
"""),
        ],
    },
}


def tier_is_due(tier: str, when) -> bool:
    """Whether ``tier``'s cadence boundary falls on ``when`` (a date/datetime).

    The sweep is scheduled daily; this decides which tiers to act on each run:
      T1 daily; T2 Mondays; T3 Mondays of even ISO weeks; T4 the 1st.
    """
    weekday = when.weekday()          # Mon=0 .. Sun=6
    iso_week = when.isocalendar()[1]  # 1..53
    if tier == "T1":
        return True
    if tier == "T2":
        return weekday == 0
    if tier == "T3":
        return weekday == 0 and iso_week % 2 == 0
    if tier == "T4":
        return when.day == 1
    return False


def due_tiers(when) -> list[str]:
    """Ordered list of tier keys due on ``when``."""
    return [t for t in ("T1", "T2", "T3", "T4") if tier_is_due(t, when)]


def dedup_source_id(tier: str, svc: _Svc) -> str:
    """Stable per (tier, service) dedup key stored on Item.source_id."""
    return f"{tier}:{svc.slug}"


def _build_checklist_from_map(tier_key: str, svc: _Svc, map_fetch=None) -> str:
    """Try CVQA-L7 map; return (checklist_text, map_used: bool).

    ``map_fetch`` lets the sweep reuse one wiki fetch per service for both
    checklist composition and the freshness check.
    """
    from tasks.models import QaCadenceRotation

    fetched = map_fetch if map_fetch is not None else fetch_target_map_full(svc.map_title)

    if fetched.error is not None:
        # Fetch/auth error — NEVER treated as "no map" (fail-loud, PLAT-1254)
        return fetch_error_banner(svc.name, fetched.error) + svc.checklist, False

    if not fetched.exists:
        # Page genuinely absent — fall back with banner
        return no_map_banner(svc.name) + svc.checklist, False

    rows = fetched.rows
    if not rows:
        # Page exists but no parseable F0/F1/F2 rows — treat same as absent
        return no_map_banner(svc.name) + svc.checklist, False

    # Load rotation cursors (get_or_create is safe under Django's atomic defaults)
    f1_obj, _ = QaCadenceRotation.objects.get_or_create(
        project_key=svc.project_key, tier="F1", defaults={"cursor": 0}
    )
    f2_obj, _ = QaCadenceRotation.objects.get_or_create(
        project_key=svc.project_key, tier="F2", defaults={"cursor": 0}
    )

    checklist, new_f1, new_f2 = compose_checklist(
        rows, tier_key, f1_obj.cursor, f2_obj.cursor
    )

    # Advance + persist cursors
    if new_f1 != f1_obj.cursor:
        QaCadenceRotation.objects.filter(pk=f1_obj.pk).update(cursor=new_f1)
    if new_f2 != f2_obj.cursor:
        QaCadenceRotation.objects.filter(pk=f2_obj.pk).update(cursor=new_f2)

    if fetched.page_url:
        checklist = (
            f"**Target Map:** {fetched.page_url} — the map is the source of "
            f"truth; update the row's verdict there after each check.\n\n"
            + checklist
        )
    return checklist, True


def build_task_fields(tier_key: str, svc: _Svc, project, when, map_fetch=None) -> dict:
    """Assemble the Item.objects.create kwargs for one cadence task.

    When a ``QA Target Map — <service>`` wiki page exists (CVQA-L7), the
    checklist is composed from F0 (every run) + rotating F1/F2 slices.
    Otherwise falls back to the static CVQA-L2 checklist with a banner.
    """
    tier = QA_TIERS[tier_key]
    cadence = tier["cadence"]
    title = f"[QA Cadence] {svc.name} — {tier_key}/{cadence} verification"

    try:
        checklist_text, map_used = _build_checklist_from_map(tier_key, svc, map_fetch=map_fetch)
    except Exception as exc:
        logger.exception(
            "PLS-238 target-map composition failed for %s/%s",
            tier_key,
            svc.slug,
        )
        checklist_text = (
            fetch_error_banner(svc.name, f"target-map composition failed: {exc}")
            + svc.checklist
        )
        map_used = False

    checklist_header = "### Checklist — {name} (CVQA-L7 Target Map)".format(
        name=svc.name
    ) if map_used else f"### Checklist — {svc.name}"

    description = (
        f"Auto-generated standing QA verification ({tier_key} — {tier['label']}, "
        f"{cadence}). Run the checklist on the LIVE stack and **file any failure "
        f"immediately as a bug to the owning team** (do not root-cause here).\n\n"
        f"{checklist_header}\n{checklist_text}\n\n"
        f"{DELTA_QA_STEP}\n\n"
        f"**Bug filing:** urgent if unavailable/data-loss, high if a key flow is "
        f"broken, normal if degraded. Include the exact command + output, "
        f"timestamp, node/hub, HTTP status, and repro steps.\n\n"
        f"Spec/authority: {WIKI_REF} · Delta-QA standard: {DELTA_WIKI_REF}."
    )
    return {
        "mode": "task",
        "item_type": "qa.cadence",
        "title": title,
        "description": description,
        "project": project,
        "workflow_name": "autonomous-dev",
        "assignment_group": "qa",
        "status": "open",
        "priority": "low",
        "source": QA_CADENCE_SOURCE,
        "source_id": dedup_source_id(tier_key, svc),
        "labels": ["qa-cadence", f"tier:{tier_key.lower()}", f"service:{svc.slug}"],
        "metadata": {"recurring_owner_team": "qa"},
    }


# ── CVQA-L7 map maintenance (operator directive 2026-07-14) ──────────────────
# The Target Maps ARE the QA knowledge base — what exists and what correct
# means. They are maintained BY the QA team IN the wiki, and the sweep enforces
# it mechanically: a service whose map is missing, unparseable, or stale gets a
# discovery/refresh task filed to qa (deduped like cadence tasks).

MAP_STALE_DAYS = 30


def all_services() -> list[_Svc]:
    """Unique services across all tiers (freshness is checked daily for all)."""
    seen, out = set(), []
    for tier in QA_TIERS.values():
        for svc in tier["services"]:
            if svc.slug not in seen:
                seen.add(svc.slug)
                out.append(svc)
    return out


def map_refresh_dedup_id(svc: _Svc) -> str:
    """Stable per-service dedup key for map discovery/refresh tasks."""
    return f"map-refresh:{svc.slug}"


def build_map_refresh_task_fields(svc: _Svc, project, reason: str) -> dict:
    """Item.objects.create kwargs for a Target Map build/refresh task."""
    title = f"[QA Discovery] Build/refresh Target Map — {svc.name}"
    description = (
        f"Auto-generated CVQA-L7 map-maintenance task ({reason}).\n\n"
        f"The QA team owns the feature inventory for **{svc.name}** in the wiki "
        f"— it is the reference for all recurring QA. Walk the LIVE product "
        f"(every page/screen/menu/API/MCP tool), not memory, and create or "
        f"refresh the canonical page **`{svc.map_title}`** in the dedicated QA "
        f"space (key `QA`):\n\n"
        f"1. Inventory every feature/journey with: expectation (how it is "
        f"SUPPOSED to behave, incl. edge behavior), live verification recipe "
        f"(exact steps AS A USER), scenario matrix for critical journeys "
        f"(disconnect/offline/token-expiry/retry), and tier — F0 critical "
        f"(every run) / F1 core (weekly) / F2 long-tail (monthly).\n"
        f"2. Use the standard row format so the cadence generator can compose "
        f"checklists: tier sections (`## F0 — ...`) with numbered journeys "
        f"(`### F0.1 — <journey>`), and F1/F2 tables headed "
        f"`| Journey | Expectation | Recipe | Verdict |` (or an `ID` column "
        f"with `F1.x` ids). Keep execution logs in a separate "
        f"`## Execution log` section — never inside tier tables.\n"
        f"3. Features that changed/shipped since the last refresh get their "
        f"rows updated; features that no longer exist get removed; expectation "
        f"drift is a finding (update the row, note it).\n"
        f"4. Page title must be EXACTLY `{svc.map_title}` (the generator "
        f"looks it up by title).\n\n"
        f"Standard: Engineering wiki — CVQA-L7 QA Target Registry (b8ebf20c) · "
        f"Delta-QA: CVQA-L8 (QA space, e4931509). Freshness contract: maps "
        f"refreshed at least every {MAP_STALE_DAYS} days; this task regenerates "
        f"whenever the map is missing or stale."
    )
    return {
        "mode": "task",
        "item_type": "qa.discovery",
        "title": title,
        "description": description,
        "project": project,
        "workflow_name": "autonomous-dev",
        "assignment_group": "qa",
        "status": "open",
        "priority": "normal",
        "source": QA_CADENCE_SOURCE,
        "source_id": map_refresh_dedup_id(svc),
        "labels": ["qa-target-map", "map-maintenance", f"service:{svc.slug}"],
        "metadata": {"recurring_owner_team": "qa"},
    }
