"""Source guard for direct production ``Comment.objects.create`` writers.

PLS-478 makes the closed MechanicalCommentFactory the only way to mint a row
that the direction scanner may exclude.  Existing direct writers remain an
explicit, fail-closed LEGACY_UNTYPED inventory while they are drained.  This
guard makes that inventory closed: a newly introduced direct writer fails CI
until it is routed through the typed factory (or is deliberately reviewed as a
user-authored writer and the inventory is updated in the same change).
"""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path


BASELINE_PATH = Path(__file__).resolve().parent / 'legacy_direct_comment_writers.json'


def _is_direct_comment_create(node: ast.Call) -> bool:
    func = node.func
    return bool(
        isinstance(func, ast.Attribute)
        and func.attr == 'create'
        and isinstance(func.value, ast.Attribute)
        and func.value.attr == 'objects'
        and isinstance(func.value.value, ast.Name)
        and func.value.value.id in {'Comment', '_Comment'}
    )


class _WriterVisitor(ast.NodeVisitor):
    def __init__(self, relative_path: str):
        self.relative_path = relative_path
        self.scope = ['<module>']
        self.rows: list[tuple[str, str]] = []

    def _visit_scope(self, node):
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_FunctionDef = _visit_scope
    visit_AsyncFunctionDef = _visit_scope

    def visit_ClassDef(self, node):
        self._visit_scope(node)

    def visit_Call(self, node):
        if _is_direct_comment_create(node):
            normalized = ast.dump(node, annotate_fields=True, include_attributes=False)
            digest = hashlib.sha256(normalized.encode()).hexdigest()[:20]
            self.rows.append((f'{self.relative_path}::{".".join(self.scope)}', digest))
        self.generic_visit(node)


def direct_comment_writer_inventory(repository_root: Path) -> list[str]:
    """Return stable fingerprints for production direct Comment create calls."""
    tasks_root = Path(repository_root) / 'tasks'
    raw: list[tuple[str, str]] = []
    for path in sorted(tasks_root.rglob('*.py')):
        relative = path.relative_to(repository_root).as_posix()
        if '/tests/' in f'/{relative}' or '/migrations/' in f'/{relative}':
            continue
        if relative.endswith('/services/mechanical_comments.py'):
            continue
        tree = ast.parse(path.read_text(), filename=relative)
        visitor = _WriterVisitor(relative)
        visitor.visit(tree)
        raw.extend(visitor.rows)

    # Identical calls in one scope retain a stable occurrence discriminator.
    counts: dict[tuple[str, str], int] = {}
    inventory = []
    for location, digest in sorted(raw):
        key = (location, digest)
        counts[key] = counts.get(key, 0) + 1
        inventory.append(f'{location}::{digest}::{counts[key]}')
    return sorted(inventory)


def load_baseline(path: Path = BASELINE_PATH) -> list[str]:
    data = json.loads(Path(path).read_text())
    if not isinstance(data, list) or not all(isinstance(row, str) for row in data):
        raise ValueError(f'Invalid direct-comment writer baseline: {path}')
    return sorted(data)


def assert_no_direct_comment_writer_drift(repository_root: Path) -> None:
    observed = direct_comment_writer_inventory(Path(repository_root))
    expected = load_baseline()
    if observed != expected:
        added = sorted(set(observed) - set(expected))
        removed = sorted(set(expected) - set(observed))
        raise AssertionError(
            'Direct Comment.objects.create inventory drifted. New production '
            'writers must use MechanicalCommentFactory (or explicitly remain '
            'USER_AUTHORED after review). '
            f'added={added}; removed={removed}'
        )
