"""
Import tasks from another Pulse instance over the network.

Usage:
  python manage.py import_from_pulse \\
      --source https://shizuha.com/pulse \\
      --username hritik \\
      --password 'secret' \\
      [--target-project AUTOMATA] \\
      [--search 'some query'] \\
      [--mode task] \\
      [--limit 500] \\
      [--dry-run]

Mechanism:
  1. Log into the source instance via /id/api/auth/login/ → JWT
  2. GET /pulse/api/items/export/bundle/ with filters → receive ZIP bundle
  3. Call import_service.import_bundle() locally to insert into this DB

Idempotent: existing items (matched by item_key) are skipped. Re-running is safe.
"""
import json
import sys
from urllib.parse import urlencode
from urllib.request import Request
from urllib.error import HTTPError, URLError

from tasks.net_utils import safe_urlopen  # PLAT-1570: scheme-guarded urlopen (http/https only)

from django.core.management.base import BaseCommand, CommandError
from django.contrib.auth import get_user_model

from ...models import Project
from ...services.import_service import import_bundle as _import_bundle, ImportError as _ImportError


class Command(BaseCommand):
    help = "Import tasks from another Pulse instance over HTTP"

    def add_arguments(self, parser):
        parser.add_argument(
            "--source", required=True,
            help="Base URL of source Pulse instance (e.g. https://shizuha.com/pulse — no trailing /api)",
        )
        parser.add_argument("--username", required=True, help="Username on source instance")
        parser.add_argument("--password", required=True, help="Password on source instance")
        parser.add_argument(
            "--target-project",
            help="Project key on THIS instance to import into. "
                 "If omitted, each item tries to match its source project by key.",
        )
        parser.add_argument(
            "--importer-email",
            help="Email of the local user to attribute imports to. Defaults to the first superuser.",
        )
        parser.add_argument(
            "--search", help="Filter source items by title/key substring (same as Tasks page search)",
        )
        parser.add_argument(
            "--mode", default="task", choices=["task", "action", "alert", ""],
            help="Filter by mode. Default: task. Empty string = all modes.",
        )
        parser.add_argument(
            "--status", help="Filter by status (comma-separated supported, e.g. 'pending,in_progress')",
        )
        parser.add_argument(
            "--is-active", action="store_true",
            help="Only import active items (non-terminal status). Uses is_active=true filter on source.",
        )
        parser.add_argument(
            "--limit", type=int, default=0,
            help="Cap number of tasks to fetch (0 = no limit). Uses page_size.",
        )
        parser.add_argument(
            "--no-comments", action="store_true", help="Skip importing comments",
        )
        parser.add_argument(
            "--no-links", action="store_true", help="Skip importing linked issues",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Fetch the bundle but don't insert — print summary of what WOULD be imported.",
        )

    def handle(self, *args, **opts):
        source_base = opts["source"].rstrip("/")
        # Split auth host from pulse path. The auth endpoint lives at /id/api/auth/login/
        # relative to the DOMAIN (not /pulse/). Try a few resolvable forms.
        # Example input: https://shizuha.com/pulse → domain = https://shizuha.com
        if source_base.endswith("/pulse"):
            auth_base = source_base[: -len("/pulse")]
        else:
            auth_base = source_base

        self.stdout.write(f"Source:        {source_base}")
        self.stdout.write(f"Auth endpoint: {auth_base}/id/api/auth/login/")

        # --- 1. Log in ---
        token = self._login(auth_base, opts["username"], opts["password"])
        self.stdout.write(self.style.SUCCESS("  ✓ logged in"))

        # --- 2. Fetch bundle ---
        params = {}
        if opts.get("search"):
            params["search"] = opts["search"]
        if opts.get("mode"):
            params["mode"] = opts["mode"]
        if opts.get("status"):
            params["status"] = opts["status"]
        if opts.get("is_active"):
            params["is_active"] = "true"
        if opts.get("limit"):
            params["page_size"] = opts["limit"]
        if opts.get("no_comments"):
            params["include_comments"] = "false"
        if opts.get("no_links"):
            params["include_links"] = "false"

        url = f"{source_base}/api/items/export/bundle/"
        if params:
            url += "?" + urlencode(params)

        self.stdout.write(f"Fetching:      {url}")
        bundle_bytes = self._fetch_bundle(url, token)
        self.stdout.write(self.style.SUCCESS(f"  ✓ fetched {len(bundle_bytes):,} bytes"))

        # --- 3. Resolve target project locally ---
        target_project = None
        target_key = opts.get("target_project")
        if target_key:
            try:
                target_project = Project.objects.get(key=target_key)
                self.stdout.write(f"Target project: {target_project.name} ({target_project.key})")
            except Project.DoesNotExist:
                raise CommandError(f"Target project key {target_key!r} not found on this instance")

        # --- 4. Resolve importer user ---
        User = get_user_model()
        importer = None
        imp_email = opts.get("importer_email")
        if imp_email:
            importer = User.objects.filter(email=imp_email).first()
            if not importer:
                raise CommandError(f"User with email {imp_email!r} not found")
        else:
            importer = User.objects.filter(is_superuser=True).first()
            if not importer:
                raise CommandError("No superuser found — pass --importer-email to specify")
        self.stdout.write(f"Importer:      {importer.email}")

        # --- 5. Dry run? ---
        if opts["dry_run"]:
            import io, zipfile, json as j
            zf = zipfile.ZipFile(io.BytesIO(bundle_bytes))
            manifest = j.loads(zf.read("manifest.json").decode("utf-8"))
            self.stdout.write(self.style.WARNING("DRY RUN — no changes written"))
            self.stdout.write(f"  schemaVersion: {manifest.get('schemaVersion')}")
            self.stdout.write(f"  tasks:         {manifest['counts']['tasks']}")
            self.stdout.write(f"  comments:      {manifest['counts']['comments']}")
            self.stdout.write(f"  links:         {manifest['counts']['links']}")
            return

        # --- 6. Import ---
        self.stdout.write("Importing...")
        try:
            result = _import_bundle(
                bundle_bytes,
                target_project=target_project,
                importer_user=importer,
                include_comments=not opts["no_comments"],
                include_links=not opts["no_links"],
            )
        except _ImportError as e:
            raise CommandError(f"Import failed: {e}")

        # --- 7. Summary ---
        self.stdout.write(self.style.SUCCESS("\nDone:"))
        self.stdout.write(f"  tasks created:    {result.tasks_created}")
        self.stdout.write(f"  tasks skipped:    {result.tasks_skipped} (already exist)")
        self.stdout.write(f"  tasks failed:     {result.tasks_failed}")
        self.stdout.write(f"  comments created: {result.comments_created}")
        self.stdout.write(f"  comments skipped: {result.comments_skipped}")
        self.stdout.write(f"  links created:    {result.links_created}")
        self.stdout.write(f"  links skipped:    {result.links_skipped}")
        if result.warnings:
            self.stdout.write(self.style.WARNING(f"  warnings ({len(result.warnings)}):"))
            for w in result.warnings[:10]:
                self.stdout.write(f"    {w}")
            if len(result.warnings) > 10:
                self.stdout.write(f"    ... and {len(result.warnings) - 10} more")
        if result.errors:
            self.stdout.write(self.style.ERROR(f"  errors ({len(result.errors)}):"))
            for e in result.errors[:10]:
                self.stdout.write(f"    {e}")

    # --- helpers ---

    def _login(self, auth_base: str, username: str, password: str) -> str:
        body = json.dumps({"username": username, "password": password}).encode("utf-8")
        req = Request(
            f"{auth_base}/id/api/auth/login/",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with safe_urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise CommandError(f"Login failed (HTTP {e.code}): {body[:500]}")
        except URLError as e:
            raise CommandError(f"Login failed (network): {e.reason}")

        token = (data.get("tokens") or {}).get("access")
        if not token:
            raise CommandError(f"Login response missing access token: {data}")
        return token

    def _fetch_bundle(self, url: str, token: str) -> bytes:
        req = Request(url, headers={"Authorization": f"Bearer {token}"}, method="GET")
        try:
            with safe_urlopen(req, timeout=120) as resp:
                return resp.read()
        except HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise CommandError(f"Bundle fetch failed (HTTP {e.code}): {body[:500]}")
        except URLError as e:
            raise CommandError(f"Bundle fetch failed (network): {e.reason}")
