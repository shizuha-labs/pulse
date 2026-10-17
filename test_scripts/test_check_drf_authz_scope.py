"""Regression tests for the PLAT-4196 deterministic DRF authz AST gate."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).parents[1] / "scripts" / "check_drf_authz_scope.py"
SPEC = importlib.util.spec_from_file_location("check_drf_authz_scope", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
LINT = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = LINT
SPEC.loader.exec_module(LINT)


HEADER = """
from rest_framework import permissions, viewsets
from rest_framework.decorators import action
from rest_framework.views import APIView
from tasks.models import Comment, CustomFieldValue, Item, WorkLog
"""


class DrfAuthzScopeLintTests(unittest.TestCase):
    def scan(self, source: str, *, allowlist: dict | None = None):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        target = root / "views.py"
        target.write_text(HEADER + source, encoding="utf-8")
        allowlist_path = None
        if allowlist is not None:
            allowlist_path = root / "allowlist.json"
            allowlist_path.write_text(json.dumps(allowlist), encoding="utf-8")
        return LINT.scan([target], allowlist_path=allowlist_path, cwd=root)

    def assert_rules(self, result, expected):
        self.assertEqual(result.errors, [])
        self.assertEqual([finding.rule for finding in result.findings], expected)

    def test_plat4195_bulk_write_visibility_scope_is_not_mutation_authority(self):
        result = self.scan(
            """
class ItemBulkOperationView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        visible_ids = PermissionService.get_user_item_queryset(request.user).values("id")
        return Item.objects.filter(id__in=visible_ids).delete()
"""
        )

        self.assert_rules(result, [LINT.RULE_DIRECT_QUERY])
        self.assertIn("ItemBulkOperationView.post", result.findings[0].fingerprint)

    def test_bulk_write_ids_filtered_by_object_permission_are_safe(self):
        result = self.scan(
            """
class ItemBulkOperationView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        visible = PermissionService.get_user_item_queryset(request.user)
        permission = ItemProjectPermission()
        authorized_ids = [
            item.id for item in visible
            if permission.has_object_permission(request, self, item)
        ]
        return Item.objects.filter(id__in=authorized_ids).delete()
"""
        )

        self.assert_rules(result, [])

    def test_comment_detail_direct_pk_lookup_is_flagged(self):
        result = self.scan(
            """
class CommentViewSet(viewsets.ModelViewSet):
    permission_classes = [permissions.IsAuthenticated]

    @action(detail=True, methods=["get"])
    def reactions(self, request, pk=None):
        return Comment.objects.get(pk=pk)
"""
        )

        self.assert_rules(result, [LINT.RULE_DIRECT_QUERY])
        self.assertIn("Comment.objects.get[pk]", result.findings[0].fingerprint)

    def test_scoped_comment_lookup_and_explicit_object_check_are_safe(self):
        scoped = self.scan(
            """
class CommentViewSet(viewsets.ModelViewSet):
    permission_classes = [permissions.IsAuthenticated]

    @action(detail=True, methods=["get"])
    def reactions(self, request, pk=None):
        visible_items = PermissionService.get_user_item_queryset(request.user).values("id")
        return Comment.objects.get(pk=pk, item_id__in=visible_items)
"""
        )
        guarded = self.scan(
            """
class CommentViewSet(viewsets.ModelViewSet):
    permission_classes = [permissions.IsAuthenticated]

    def retrieve(self, request, pk=None):
        comment = Comment.objects.get(pk=pk)
        self.check_object_permissions(request, comment)
        return comment
"""
        )

        self.assert_rules(scoped, [])
        self.assert_rules(guarded, [])

    def test_worklog_parent_id_filter_from_query_params_is_flagged(self):
        result = self.scan(
            """
class WorkLogViewSet(viewsets.ModelViewSet):
    permission_classes = [permissions.IsAuthenticated]

    @action(detail=False, methods=["get"])
    def my_logs(self, request):
        return WorkLog.objects.filter(item_id=request.query_params.get("item"))
"""
        )

        self.assert_rules(result, [LINT.RULE_DIRECT_QUERY])

    def test_worklog_parent_scoped_queryset_is_safe(self):
        result = self.scan(
            """
class WorkLogViewSet(viewsets.ModelViewSet):
    queryset = WorkLog.objects.all()
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        visible_items = PermissionService.get_user_item_queryset(self.request.user).values("id")
        return WorkLog.objects.filter(item_id__in=visible_items)

    @action(detail=False, methods=["get"])
    def my_logs(self, request):
        visible_items = PermissionService.get_user_item_queryset(request.user).values("id")
        return WorkLog.objects.filter(
            user_id=request.user.id,
            item_id__in=visible_items,
        )
"""
        )

        self.assert_rules(result, [])

    def test_custom_field_and_comment_unscoped_generic_querysets_are_flagged(self):
        result = self.scan(
            """
class CommentViewSet(viewsets.ModelViewSet):
    queryset = Comment.objects.all()
    permission_classes = [permissions.IsAuthenticated]

class CustomFieldValueViewSet(viewsets.ModelViewSet):
    queryset = CustomFieldValue.objects.all()
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        queryset = CustomFieldValue.objects.all()
        item_id = self.request.query_params.get("item_id")
        if item_id:
            queryset = queryset.filter(item_id=item_id)
        return queryset
"""
        )

        self.assert_rules(
            result,
            [LINT.RULE_UNSCOPED_QUERYSET, LINT.RULE_UNSCOPED_QUERYSET],
        )
        self.assertTrue(all(".<class>:" in row.fingerprint for row in result.findings))

    def test_super_get_queryset_with_only_client_filter_is_not_scope_provenance(self):
        result = self.scan(
            """
class BoardQuickFilterViewSet(viewsets.ModelViewSet):
    queryset = CustomFieldValue.objects.all()
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = super().get_queryset()
        board_id = self.request.query_params.get("board_id")
        if board_id:
            qs = qs.filter(board_id=board_id)
        return qs
"""
        )

        self.assert_rules(result, [LINT.RULE_UNSCOPED_QUERYSET])

    def test_custom_permission_class_is_outside_is_authenticated_only_rule(self):
        result = self.scan(
            """
class CommentViewSet(viewsets.ModelViewSet):
    queryset = Comment.objects.all()
    permission_classes = [permissions.IsAuthenticated, TenantObjectPermission]

    def retrieve(self, request, pk=None):
        return Comment.objects.get(pk=pk)
"""
        )

        self.assert_rules(result, [])

    def test_reachable_helper_is_scanned_and_write_context_propagates(self):
        result = self.scan(
            """
class BulkView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def _load(self, request):
        visible = PermissionService.get_user_item_queryset(request.user).values("id")
        return Item.objects.filter(id__in=visible)

    def post(self, request):
        return self._load(request).delete()
"""
        )

        self.assert_rules(result, [LINT.RULE_DIRECT_QUERY])
        self.assertIn("BulkView._load", result.findings[0].fingerprint)

    def test_inline_exemption_requires_task_and_reason_and_cannot_go_stale(self):
        accepted = self.scan(
            """
class CommentViewSet(viewsets.ModelViewSet):
    permission_classes = [permissions.IsAuthenticated]

    def retrieve(self, request, pk=None):
        # drf-authz-scope: allow[PLAT-4201] -- accepted legacy endpoint pending tenant sweep
        return Comment.objects.get(pk=pk)
"""
        )
        malformed = self.scan(
            """
class CommentViewSet(viewsets.ModelViewSet):
    permission_classes = [permissions.IsAuthenticated]

    def retrieve(self, request, pk=None):
        # drf-authz-scope: ignore because legacy
        return Comment.objects.get(pk=pk)
"""
        )
        stale = self.scan(
            """
class CommentViewSet(viewsets.ModelViewSet):
    permission_classes = [permissions.IsAuthenticated]

    def retrieve(self, request, pk=None):
        # drf-authz-scope: allow[PLAT-4201] -- no unsafe query remains here
        return Comment.objects.none()
"""
        )

        self.assertEqual(accepted.findings, [])
        self.assertEqual(len(accepted.suppressed), 1)
        self.assertTrue(malformed.errors)
        self.assertTrue(stale.errors)

    def test_allowlist_is_exact_task_keyed_and_stale_entries_fail(self):
        first = self.scan(
            """
class CommentViewSet(viewsets.ModelViewSet):
    permission_classes = [permissions.IsAuthenticated]
    def retrieve(self, request, pk=None):
        return Comment.objects.get(pk=pk)
"""
        )
        fingerprint = first.findings[0].fingerprint
        accepted = self.scan(
            """
class CommentViewSet(viewsets.ModelViewSet):
    permission_classes = [permissions.IsAuthenticated]
    def retrieve(self, request, pk=None):
        return Comment.objects.get(pk=pk)
""",
            allowlist={
                "version": 1,
                "exemptions": [{
                    "fingerprint": fingerprint,
                    "task": "PLAT-4201",
                    "reason": "tracked legacy tenant sweep",
                }],
            },
        )
        stale = self.scan(
            """
class CommentViewSet(viewsets.ModelViewSet):
    permission_classes = [permissions.IsAuthenticated]
    def retrieve(self, request, pk=None):
        return Comment.objects.none()
""",
            allowlist={
                "version": 1,
                "exemptions": [{
                    "fingerprint": fingerprint,
                    "task": "PLAT-4201",
                    "reason": "tracked legacy tenant sweep",
                }],
            },
        )

        self.assertEqual(accepted.findings, [])
        self.assertEqual(len(accepted.suppressed), 1)
        self.assertTrue(stale.errors)


if __name__ == "__main__":
    unittest.main()
