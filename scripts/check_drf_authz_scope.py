#!/usr/bin/env python3
"""Fail on high-confidence DRF queries that bypass tenant/object scope.

This is deliberately a small, deterministic AST check rather than a semantic
Python engine.  It focuses on a dangerous and recurring shape:

* a DRF ``APIView``/``ViewSet`` protected only by ``IsAuthenticated``; and
* a request path that starts again from ``Model.objects`` using ``all()``,
  ``get()``, or an ID-bearing ``filter()`` without a user/tenant scope or an
  object-authorisation check.

It also catches generic viewsets whose class-level ``queryset`` is unscoped and
which do not replace it with a user-scoped ``get_queryset()``.  DRF does not
apply object permissions to every row of list querysets, so authentication
alone is not an isolation boundary.

Suppressions are intentionally reviewable.  An inline suppression must use:

    # drf-authz-scope: allow[PLAT-1234] -- why this exact query is safe/accepted

on the finding line or immediately above it.  A JSON allowlist may instead be
passed with ``--allowlist``; every entry needs the exact emitted fingerprint,
task key, and reason.  Malformed or stale suppressions fail the check.

Exit status: 0 clean, 1 findings, 2 invalid source/configuration.
"""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
import io
import json
from pathlib import Path
import re
import sys
import tokenize
from typing import Iterable, Iterator, Mapping, Sequence


RULE_DIRECT_QUERY = "DRF-AUTHZ001"
RULE_UNSCOPED_QUERYSET = "DRF-AUTHZ002"

TASK_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]{1,15}-[1-9][0-9]*$")
MARKER_TOKEN = "drf-authz-scope:"
MARKER_RE = re.compile(
    r"#\s*drf-authz-scope:\s*allow\[([A-Z][A-Z0-9]{1,15}-[1-9][0-9]*)\]"
    r"\s*--\s*(\S.*)$"
)

EXCLUDED_PARTS = {
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "media",
    "migrations",
    "node_modules",
    "static",
    "tests",
    "test_scripts",
    "venv",
}

HTTP_ROOT_METHODS = {
    "get",
    "post",
    "put",
    "patch",
    "delete",
    "head",
    "options",
    "list",
    "retrieve",
    "create",
    "update",
    "partial_update",
    "destroy",
    "get_object",
}
WRITE_ROOT_METHODS = {
    "post",
    "put",
    "patch",
    "delete",
    "create",
    "update",
    "partial_update",
    "destroy",
    "perform_create",
    "perform_update",
    "perform_destroy",
}
MUTATING_HTTP_METHODS = {"post", "put", "patch", "delete"}

_PROVENANCE_RANK = {"none": 0, "user": 1, "visibility": 2, "authority": 3}


@dataclass(frozen=True, order=True)
class Finding:
    path: str
    line: int
    column: int
    rule: str
    fingerprint: str
    message: str


@dataclass(frozen=True)
class Exemption:
    fingerprint: str
    task: str
    reason: str


@dataclass
class ScanResult:
    findings: list[Finding]
    errors: list[str]
    suppressed: list[tuple[Finding, str, str]]


@dataclass
class MethodInfo:
    node: ast.FunctionDef | ast.AsyncFunctionDef
    roots: set[str]
    calls: set[str]
    is_write_action: bool


def _dotted_name(node: ast.AST | None) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _terminal_name(node: ast.AST | None) -> str:
    if isinstance(node, ast.Call):
        return _terminal_name(node.func)
    dotted = _dotted_name(node)
    return dotted.rsplit(".", 1)[-1] if dotted else ""


def _target_names(node: ast.AST) -> Iterator[str]:
    if isinstance(node, (ast.Tuple, ast.List)):
        for item in node.elts:
            yield from _target_names(item)
    elif isinstance(node, ast.Name):
        yield node.id


def _permission_names(value: ast.AST) -> list[str]:
    values = value.elts if isinstance(value, (ast.List, ast.Tuple, ast.Set)) else [value]
    return [_terminal_name(item) for item in values]


def _own_permission_names(cls: ast.ClassDef) -> list[str] | None:
    for node in cls.body:
        value: ast.AST | None = None
        targets: Sequence[ast.AST] = ()
        if isinstance(node, ast.Assign):
            value, targets = node.value, node.targets
        elif isinstance(node, ast.AnnAssign):
            value, targets = node.value, (node.target,)
        if value is not None and any(
            isinstance(target, ast.Name) and target.id == "permission_classes"
            for target in targets
        ):
            return _permission_names(value)
    return None


def _is_drf_base_name(name: str) -> bool:
    terminal = name.rsplit(".", 1)[-1]
    return terminal.endswith("APIView") or terminal.endswith("ViewSet")


def _class_maps(module: ast.Module) -> tuple[dict[str, ast.ClassDef], set[str]]:
    classes = {node.name: node for node in module.body if isinstance(node, ast.ClassDef)}
    drf_classes = {
        name
        for name, cls in classes.items()
        if any(_is_drf_base_name(_dotted_name(base)) for base in cls.bases)
    }
    changed = True
    while changed:
        changed = False
        for name, cls in classes.items():
            if name in drf_classes:
                continue
            if any(_terminal_name(base) in drf_classes for base in cls.bases):
                drf_classes.add(name)
                changed = True
    return classes, drf_classes


def _inherited_permissions(
    cls: ast.ClassDef,
    classes: Mapping[str, ast.ClassDef],
    seen: set[str] | None = None,
) -> list[str] | None:
    own = _own_permission_names(cls)
    if own is not None:
        return own
    seen = set() if seen is None else seen
    if cls.name in seen:
        return None
    seen.add(cls.name)
    for base in cls.bases:
        parent = classes.get(_terminal_name(base))
        if parent is not None:
            inherited = _inherited_permissions(parent, classes, seen)
            if inherited is not None:
                return inherited
    return None


def _is_is_authenticated_only(names: list[str] | None) -> bool:
    return names == ["IsAuthenticated"]


def _action_methods(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str] | None:
    for decorator in fn.decorator_list:
        call = decorator if isinstance(decorator, ast.Call) else None
        target = call.func if call is not None else decorator
        if _terminal_name(target) != "action":
            continue
        if call is None:
            return {"get"}
        for kw in call.keywords:
            if kw.arg != "methods":
                continue
            if isinstance(kw.value, (ast.List, ast.Tuple, ast.Set)):
                methods = {
                    value.value.lower()
                    for value in kw.value.elts
                    if isinstance(value, ast.Constant) and isinstance(value.value, str)
                }
                return methods or {"get"}
            return {"unknown"}
        return {"get"}
    return None


def _method_infos(cls: ast.ClassDef) -> dict[str, MethodInfo]:
    methods = {
        node.name: node
        for node in cls.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    infos: dict[str, MethodInfo] = {}
    for name, node in methods.items():
        action_methods = _action_methods(node)
        roots: set[str] = set()
        if name in HTTP_ROOT_METHODS or name.startswith("perform_"):
            roots.add(name)
        if action_methods is not None:
            roots.add(name)
        calls: set[str] = set()
        for descendant in ast.walk(node):
            if not isinstance(descendant, ast.Call) or not isinstance(descendant.func, ast.Attribute):
                continue
            if isinstance(descendant.func.value, ast.Name) and descendant.func.value.id in {"self", "cls"}:
                calls.add(descendant.func.attr)
        infos[name] = MethodInfo(
            node=node,
            roots=roots,
            calls=calls & methods.keys(),
            is_write_action=(
                name in WRITE_ROOT_METHODS
                or (action_methods is not None and bool(action_methods & MUTATING_HTTP_METHODS))
            ),
        )

    # A helper called by a request entry point is part of that request path.
    frontier = [name for name, info in infos.items() if info.roots]
    reached_by: dict[str, set[str]] = {name: {name} for name in frontier}
    while frontier:
        caller = frontier.pop()
        for callee in infos[caller].calls:
            old = reached_by.setdefault(callee, set())
            new = reached_by[caller] - old
            if new:
                old.update(new)
                frontier.append(callee)
    for name, roots in reached_by.items():
        infos[name].roots.update(roots)
        if any(infos[root].is_write_action for root in roots if root in infos):
            infos[name].is_write_action = True
    return infos


def _manager_chain(call: ast.Call) -> tuple[str, list[tuple[str, ast.Call]]] | None:
    """Return ``(Model, [(operation, call), ...])`` from root to outer call."""
    operations: list[tuple[str, ast.Call]] = []
    cursor: ast.AST = call
    while isinstance(cursor, ast.Call) and isinstance(cursor.func, ast.Attribute):
        operations.append((cursor.func.attr, cursor))
        cursor = cursor.func.value
    if not (
        isinstance(cursor, ast.Attribute)
        and cursor.attr == "objects"
        and _dotted_name(cursor.value)
    ):
        return None
    return _dotted_name(cursor.value), list(reversed(operations))


def _is_nested_receiver_call(node: ast.Call, parents: Mapping[ast.AST, ast.AST]) -> bool:
    parent = parents.get(node)
    grandparent = parents.get(parent) if parent is not None else None
    return (
        isinstance(parent, ast.Attribute)
        and parent.value is node
        and isinstance(grandparent, ast.Call)
        and grandparent.func is parent
    )


def _lookup_is_identifier(name: str | None) -> bool:
    if not name:
        return False
    field = name.split("__", 1)[0].lower()
    return field in {"id", "pk"} or field.endswith("_id")


def _expr_contains_identifier_lookup(node: ast.AST) -> bool:
    for descendant in ast.walk(node):
        if isinstance(descendant, ast.keyword) and _lookup_is_identifier(descendant.arg):
            return True
        if isinstance(descendant, ast.Name) and descendant.id.lower() in {
            "id_q",
            "ids_q",
            "pk_q",
            "pks_q",
        }:
            return True
    return False


def _call_name(node: ast.Call) -> str:
    return _terminal_name(node.func).lower()


def _is_self_method_call(node: ast.Call, method_name: str) -> bool:
    """Return whether ``node`` is an explicit ``self.<method>()`` call.

    A superclass queryset is only the model's class-level default; it carries
    no caller scope by itself.  Treating ``super().get_queryset()`` as visible
    provenance hid exactly the unscoped-queryset pattern this gate exists to
    stop.
    """
    return (
        isinstance(node.func, ast.Attribute)
        and node.func.attr == method_name
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "self"
    )


def _max_provenance(*values: str) -> str:
    return max(values, key=lambda value: _PROVENANCE_RANK[value], default="none")


def _expression_provenance(node: ast.AST, known: Mapping[str, str]) -> str:
    provenance = "none"
    for descendant in ast.walk(node):
        if isinstance(descendant, ast.Name):
            provenance = _max_provenance(provenance, known.get(descendant.id, "none"))
        elif isinstance(descendant, ast.Attribute):
            dotted = _dotted_name(descendant).lower()
            if dotted.endswith("request.user") or dotted.endswith("request.user.id"):
                provenance = _max_provenance(provenance, "user")
        elif isinstance(descendant, ast.Call):
            name = _call_name(descendant)
            if name in {"has_object_permission", "check_object_permissions"} or any(
                token in name for token in ("authorize", "authorise", "mutation_allowed")
            ):
                provenance = _max_provenance(provenance, "authority")
            elif (
                _is_self_method_call(descendant, "get_queryset")
                or name == "get_object"
                or name.startswith("get_user_")
                or any(
                    token in name
                    for token in (
                        "visible",
                        "accessible",
                        "for_user",
                        "for_request",
                        "org_membership",
                        "organization_membership",
                        "explicit_org",
                    )
                )
            ):
                provenance = _max_provenance(provenance, "visibility")
    return provenance


def _known_provenance(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> dict[str, str]:
    known: dict[str, str] = {}
    # A few fixed-point passes handle simple aliases without pretending to be a
    # full control-flow engine.  Only explicit evidence raises provenance.
    assignments: list[tuple[list[str], ast.AST]] = []
    for descendant in ast.walk(fn):
        if isinstance(descendant, ast.Assign):
            names = [name for target in descendant.targets for name in _target_names(target)]
            assignments.append((names, descendant.value))
        elif isinstance(descendant, ast.AnnAssign) and descendant.value is not None:
            assignments.append((list(_target_names(descendant.target)), descendant.value))
        elif isinstance(descendant, (ast.For, ast.comprehension)):
            names = list(_target_names(descendant.target))
            assignments.append((names, descendant.iter))
    for _ in range(max(1, len(assignments) + 1)):
        changed = False
        for names, value in assignments:
            provenance = _expression_provenance(value, known)
            for name in names:
                combined = _max_provenance(known.get(name, "none"), provenance)
                if known.get(name) != combined:
                    known[name] = combined
                    changed = True
        if not changed:
            break
    return known


def _is_owner_lookup(keyword: str | None) -> bool:
    if not keyword:
        return False
    field = keyword.split("__", 1)[0].lower()
    return field in {
        "user",
        "user_id",
        "owner",
        "owner_id",
        "created_by",
        "created_by_id",
        "uploaded_by",
        "uploaded_by_id",
    }


def _chain_provenance(
    operations: Sequence[tuple[str, ast.Call]], known: Mapping[str, str]
) -> str:
    provenance = "none"
    for operation, call in operations:
        if operation not in {"filter", "exclude", "get"}:
            continue
        for argument in call.args:
            provenance = _max_provenance(
                provenance, _expression_provenance(argument, known)
            )
        for keyword in call.keywords:
            value_provenance = _expression_provenance(keyword.value, known)
            # An explicit owner=user constraint is mutation authority for the
            # owned row.  A mere user-derived tenant/project list is visibility.
            if value_provenance == "user" and _is_owner_lookup(keyword.arg):
                value_provenance = "authority"
            provenance = _max_provenance(provenance, value_provenance)
    return provenance


def _has_object_authority_guard(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for descendant in ast.walk(fn):
        if not isinstance(descendant, ast.Call):
            continue
        name = _call_name(descendant)
        if (
            name in {"has_object_permission", "check_object_permissions"}
            or name.startswith(("can_", "_can_", "require_", "_require_", "authorize_", "_authorize_"))
            or any(token in name for token in ("authorize", "authorise", "mutation_allowed"))
        ):
            return True
    return False


def _guard_provenance(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Recognise fail-closed guards around a later direct ORM lookup.

    ``if not scope(...): return/raise`` is materially different from merely
    assigning a visibility queryset and then bulk-mutating it.  Keep the
    recognition narrow: the denied branch must terminate and the predicate
    must be an explicit access/admin/scope check.
    """
    if _has_object_authority_guard(fn):
        return "authority"
    result = "none"
    for node in ast.walk(fn):
        if not isinstance(node, ast.If) or not node.body:
            continue
        if not all(isinstance(statement, (ast.Return, ast.Raise)) for statement in node.body):
            continue
        names = {
            _call_name(call)
            for call in ast.walk(node.test)
            if isinstance(call, ast.Call)
        }
        if any(
            name.startswith(("is_admin", "_is_admin", "is_staff", "_is_staff"))
            for name in names
        ):
            result = _max_provenance(result, "authority")
        elif any(
            name.startswith("get_user_")
            or any(token in name for token in ("visible", "accessible", "org_membership"))
            for name in names
        ):
            result = _max_provenance(result, "visibility")
    return result


def _risky_operation(
    operations: Sequence[tuple[str, ast.Call]],
) -> tuple[str, ast.Call] | None:
    for operation, call in reversed(operations):
        if operation == "get":
            return operation, call
        if operation == "filter" and (
            any(_lookup_is_identifier(keyword.arg) for keyword in call.keywords)
            or any(_expr_contains_identifier_lookup(argument) for argument in call.args)
        ):
            return operation, call
    for operation, call in operations:
        if operation == "all":
            return operation, call
    return None


def _lookup_signature(call: ast.Call) -> str:
    names = sorted(keyword.arg or "**" for keyword in call.keywords)
    if call.args:
        names.append("<positional>")
    return ",".join(names)


def _query_is_scoped(
    operations: Sequence[tuple[str, ast.Call]],
    known: Mapping[str, str],
    *,
    operation: str,
    write_path: bool,
    guard_provenance: str,
) -> bool:
    provenance = _max_provenance(
        _chain_provenance(operations, known), guard_provenance
    )
    if write_path:
        # A scoped single-row lookup closes cross-tenant IDOR.  In contrast, a
        # visibility-derived bulk filter is not mutation authority (PLAT-4195).
        return provenance == "authority" or (
            operation == "get" and provenance == "visibility"
        )
    return _PROVENANCE_RANK[provenance] >= _PROVENANCE_RANK["visibility"]


def _method_has_scoped_queryset(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    known = _known_provenance(fn)
    for descendant in ast.walk(fn):
        if isinstance(descendant, ast.Call) and _call_name(descendant) == "none":
            chain = _manager_chain(descendant)
            if chain is not None:
                return True
        if not isinstance(descendant, ast.Call):
            continue
        chain = _manager_chain(descendant)
        if chain is None:
            continue
        _, operations = chain
        if _PROVENANCE_RANK[_chain_provenance(operations, known)] >= _PROVENANCE_RANK["visibility"]:
            return True
    # Returning a named queryset produced by a visibility helper is also safe.
    for node in ast.walk(fn):
        if isinstance(node, ast.Return) and node.value is not None:
            if _PROVENANCE_RANK[_expression_provenance(node.value, known)] >= _PROVENANCE_RANK["visibility"]:
                return True
    return False


def _class_queryset_assignment(cls: ast.ClassDef) -> tuple[ast.AST, ast.AST] | None:
    for node in cls.body:
        targets: Sequence[ast.AST] = ()
        value: ast.AST | None = None
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign):
            targets, value = (node.target,), node.value
        if value is not None and any(
            isinstance(target, ast.Name) and target.id == "queryset" for target in targets
        ):
            return node, value
    return None


def _display_path(path: Path, cwd: Path) -> str:
    try:
        return path.resolve().relative_to(cwd.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _finding_for_query(
    *,
    path: str,
    cls: ast.ClassDef,
    method: str,
    model: str,
    operation: str,
    call: ast.Call,
    write_path: bool,
) -> Finding:
    signature = _lookup_signature(call)
    expression = f"{model}.objects.{operation}"
    if signature:
        expression += f"[{signature}]"
    fingerprint = f"{path}:{cls.name}.{method}:{expression}"
    path_kind = "write" if write_path else "read"
    return Finding(
        path=path,
        line=call.lineno,
        column=call.col_offset + 1,
        rule=RULE_DIRECT_QUERY,
        fingerprint=fingerprint,
        message=(
            f"IsAuthenticated-only {path_kind} path {cls.name}.{method} uses "
            f"unscoped {model}.objects.{operation}(); start from a caller-scoped "
            "queryset or enforce object authorisation"
        ),
    )


def _scan_class(
    path: str,
    cls: ast.ClassDef,
    classes: Mapping[str, ast.ClassDef],
) -> list[Finding]:
    if not _is_is_authenticated_only(_inherited_permissions(cls, classes)):
        return []

    findings: list[Finding] = []
    methods = _method_infos(cls)

    queryset_assignment = _class_queryset_assignment(cls)
    if queryset_assignment is not None:
        assignment, value = queryset_assignment
        get_queryset = methods.get("get_queryset")
        chain = value if isinstance(value, ast.Call) else None
        manager = _manager_chain(chain) if chain is not None else None
        if manager is not None:
            model, operations = manager
            risk = _risky_operation(operations)
            if risk is not None and (
                get_queryset is None or not _method_has_scoped_queryset(get_queryset.node)
            ):
                operation, call = risk
                expression = f"{model}.objects.{operation}"
                fingerprint = f"{path}:{cls.name}.<class>:{expression}"
                findings.append(
                    Finding(
                        path=path,
                        line=getattr(assignment, "lineno", cls.lineno),
                        column=getattr(assignment, "col_offset", cls.col_offset) + 1,
                        rule=RULE_UNSCOPED_QUERYSET,
                        fingerprint=fingerprint,
                        message=(
                            f"IsAuthenticated-only {cls.name} exposes an unscoped "
                            f"{expression} class queryset without a caller-scoped "
                            "get_queryset(); DRF list views do not apply per-object permissions"
                        ),
                    )
                )

    for name, info in methods.items():
        if not info.roots or name == "get_queryset":
            continue
        fn = info.node
        known = _known_provenance(fn)
        guard_provenance = _guard_provenance(fn)
        parents = {
            child: parent
            for parent in ast.walk(fn)
            for child in ast.iter_child_nodes(parent)
        }
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call) or _is_nested_receiver_call(node, parents):
                continue
            chain = _manager_chain(node)
            if chain is None:
                continue
            model, operations = chain
            risk = _risky_operation(operations)
            if risk is None:
                continue
            operation, call = risk
            if _query_is_scoped(
                operations,
                known,
                operation=operation,
                write_path=info.is_write_action,
                guard_provenance=guard_provenance,
            ):
                continue
            findings.append(
                _finding_for_query(
                    path=path,
                    cls=cls,
                    method=name,
                    model=model,
                    operation=operation,
                    call=call,
                    write_path=info.is_write_action,
                )
            )
    return findings


def _load_markers(path: str, source: str) -> tuple[dict[int, tuple[str, str]], list[str]]:
    markers: dict[int, tuple[str, str]] = {}
    errors: list[str] = []
    try:
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)
        comments = (
            (token.start[0], token.string)
            for token in tokens
            if token.type == tokenize.COMMENT
        )
    except (IndentationError, tokenize.TokenError) as exc:
        return {}, [f"{path}: cannot tokenize suppression comments: {exc}"]
    for line_number, comment in comments:
        if MARKER_TOKEN not in comment.lower():
            continue
        match = MARKER_RE.search(comment)
        if match is None:
            errors.append(
                f"{path}:{line_number}: invalid suppression; expected "
                "# drf-authz-scope: allow[PLAT-1234] -- specific reason"
            )
            continue
        task, reason = match.groups()
        if not TASK_KEY_RE.fullmatch(task) or len(reason.strip()) < 8:
            errors.append(
                f"{path}:{line_number}: suppression needs a valid task key and "
                "a reason of at least 8 characters"
            )
            continue
        markers[line_number] = (task, reason.strip())
    return markers, errors


def _load_allowlist(path: Path | None) -> tuple[dict[str, Exemption], list[str]]:
    if path is None:
        return {}, []
    errors: list[str] = []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return {}, [f"{path}: cannot read allowlist: {exc}"]
    if not isinstance(payload, dict) or payload.get("version") != 1:
        return {}, [f"{path}: allowlist must be an object with version=1"]
    entries = payload.get("exemptions")
    if not isinstance(entries, list):
        return {}, [f"{path}: allowlist.exemptions must be a list"]
    exemptions: dict[str, Exemption] = {}
    for index, raw in enumerate(entries):
        where = f"{path}:exemptions[{index}]"
        if not isinstance(raw, dict):
            errors.append(f"{where}: entry must be an object")
            continue
        fingerprint = raw.get("fingerprint")
        task = raw.get("task")
        reason = raw.get("reason")
        if not isinstance(fingerprint, str) or not fingerprint.strip():
            errors.append(f"{where}: fingerprint is required")
            continue
        if not isinstance(task, str) or not TASK_KEY_RE.fullmatch(task):
            errors.append(f"{where}: task must be a Pulse key such as PLAT-1234")
            continue
        if not isinstance(reason, str) or len(reason.strip()) < 8:
            errors.append(f"{where}: reason must contain at least 8 characters")
            continue
        if fingerprint in exemptions:
            errors.append(f"{where}: duplicate fingerprint {fingerprint!r}")
            continue
        exemptions[fingerprint] = Exemption(fingerprint, task, reason.strip())
    return exemptions, errors


def _iter_python_files(paths: Sequence[Path]) -> Iterator[Path]:
    seen: set[Path] = set()
    for supplied in paths:
        if supplied.is_file():
            candidates: Iterable[Path] = (supplied,) if supplied.suffix == ".py" else ()
        elif supplied.is_dir():
            candidates = supplied.rglob("*.py")
        else:
            continue
        for candidate in candidates:
            if supplied.is_dir() and any(part in EXCLUDED_PARTS for part in candidate.parts):
                continue
            resolved = candidate.resolve()
            if resolved not in seen:
                seen.add(resolved)
                yield candidate


def scan(
    paths: Sequence[Path],
    *,
    allowlist_path: Path | None = None,
    allow_unused_exemptions: bool = False,
    cwd: Path | None = None,
) -> ScanResult:
    cwd = Path.cwd() if cwd is None else cwd
    exemptions, errors = _load_allowlist(allowlist_path)
    used_exemptions: set[str] = set()
    findings: list[Finding] = []
    suppressed: list[tuple[Finding, str, str]] = []

    supplied_missing = [path for path in paths if not path.exists()]
    errors.extend(f"{path}: scan path does not exist" for path in supplied_missing)

    for file_path in sorted(_iter_python_files(paths), key=lambda path: path.as_posix()):
        display = _display_path(file_path, cwd)
        try:
            source = file_path.read_text(encoding="utf-8")
            module = ast.parse(source, filename=display)
        except (OSError, UnicodeError, SyntaxError) as exc:
            errors.append(f"{display}: cannot parse source: {exc}")
            continue
        markers, marker_errors = _load_markers(display, source)
        errors.extend(marker_errors)
        used_marker_lines: set[int] = set()
        classes, drf_classes = _class_maps(module)
        file_findings: list[Finding] = []
        for class_name in sorted(drf_classes, key=lambda name: classes[name].lineno):
            file_findings.extend(_scan_class(display, classes[class_name], classes))

        for finding in sorted(set(file_findings)):
            marker_line = next(
                (
                    line
                    for line in (finding.line, finding.line - 1)
                    if line in markers and line not in used_marker_lines
                ),
                None,
            )
            if marker_line is not None:
                task, reason = markers[marker_line]
                used_marker_lines.add(marker_line)
                suppressed.append((finding, task, reason))
                continue
            exemption = exemptions.get(finding.fingerprint)
            if exemption is not None:
                used_exemptions.add(finding.fingerprint)
                suppressed.append((finding, exemption.task, exemption.reason))
                continue
            findings.append(finding)

        for line, (task, _reason) in sorted(markers.items()):
            if line not in used_marker_lines:
                errors.append(
                    f"{display}:{line}: stale/unused inline suppression for {task}; "
                    "remove it or attach it to the exact finding"
                )

    if not allow_unused_exemptions:
        for fingerprint, exemption in sorted(exemptions.items()):
            if fingerprint not in used_exemptions:
                errors.append(
                    f"{allowlist_path}: stale/unused exemption {fingerprint!r} "
                    f"({exemption.task})"
                )

    return ScanResult(sorted(set(findings)), sorted(set(errors)), sorted(suppressed, key=lambda row: row[0]))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths",
        nargs="*",
        default=["."],
        help="Python files/directories to scan (default: current directory)",
    )
    parser.add_argument(
        "--allowlist",
        type=Path,
        help=(
            "versioned JSON allowlist with task-keyed exact fingerprints "
            "(default: .drf-authz-scope-allowlist.json when present)"
        ),
    )
    parser.add_argument(
        "--allow-unused-exemptions",
        action="store_true",
        help="permit allowlist entries outside a deliberately partial scan",
    )
    parser.add_argument(
        "--show-suppressed",
        action="store_true",
        help="print accepted findings as informational lines",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    allowlist_path = args.allowlist
    default_allowlist = Path(".drf-authz-scope-allowlist.json")
    if allowlist_path is None and default_allowlist.is_file():
        allowlist_path = default_allowlist
    result = scan(
        [Path(path) for path in args.paths],
        allowlist_path=allowlist_path,
        allow_unused_exemptions=args.allow_unused_exemptions,
    )
    for error in result.errors:
        print(f"ERROR {error}", file=sys.stderr)
    if result.errors:
        return 2
    for finding in result.findings:
        print(
            f"{finding.path}:{finding.line}:{finding.column}: {finding.rule} "
            f"{finding.message} [fingerprint: {finding.fingerprint}]"
        )
    if args.show_suppressed:
        for finding, task, reason in result.suppressed:
            print(
                f"SUPPRESSED {finding.path}:{finding.line}: {finding.rule} "
                f"[{task}] {reason} [fingerprint: {finding.fingerprint}]"
            )
    if result.findings:
        print(
            f"DRF authz scope check failed: {len(result.findings)} finding(s), "
            f"{len(result.suppressed)} suppressed.",
            file=sys.stderr,
        )
        return 1
    print(f"DRF authz scope check passed ({len(result.suppressed)} reviewed suppression(s)).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
