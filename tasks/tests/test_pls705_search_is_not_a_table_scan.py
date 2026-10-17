"""PLS-705/PLS-712: assert hot JSON/text predicates are index-servable.

@hiro's framing, which this implements: a latency threshold is R7 in performance
clothing. "under 3s" passes today at 8,160 rows and fails silently at 20,000,
presenting exactly as this incident did. The invariant is `search does not scan
the table`, and the mechanical way to assert that is the query plan.

Why the plan is asserted with `enable_seqscan = off` rather than by reading the
default plan: on a Django test database the table has a handful of rows, and at
that size Postgres correctly prefers a sequential scan whether or not an index
exists. Asserting `Bitmap Index Scan` against the default plan would therefore
fail for a reason that has nothing to do with the defect. Disabling seqscan does
not FORCE an index -- it makes a sequential scan expensive, so the planner uses
an index if one *can* serve the predicate and falls back to the scan if none can.
That makes this a capability assertion, and capability is precisely what was
missing: before 0135 no index could serve `ILIKE '%x%'` at any table size.

The distinction matters for how the result should be read, so it is stated
rather than left implicit:

    this test proves   an index-servable path EXISTS for the search predicate
    it does not prove  the planner CHOOSES it at production scale

The second is acceptance-time work -- double the rows and show cost does not
double. Rows, not token count: 4 tokens over 0 matches cost the same as 1 token
over 0 matches (~15.2s both), so a "harder query" test measures the wrong axis
and would pass a table-scan fix.

`Bitmap Index Scan`, not `Index Scan`: GIN trigram lookups produce bitmap scans.
A guard spelled `Index Scan` would go red against a correct fix.
"""
import ast
import json
from pathlib import Path

from django.db import connection
from django.test import SimpleTestCase, TestCase

# >= 3 characters on purpose. Trigram indexes cannot serve shorter terms -- there
# are no full trigrams to look up -- so a 1-2 char probe would report a seq scan
# and read as a regression when it is a documented limit of the index type.
PROBE = "convergence"


def _plan_for(sql, params):
    """EXPLAIN with seqscan disabled -> can any index serve this predicate?"""
    with connection.cursor() as cursor:
        cursor.execute("SET LOCAL enable_seqscan = off")
        cursor.execute("EXPLAIN " + sql, params)
        return "\n".join(row[0] for row in cursor.fetchall())


class SearchPredicateIsIndexServable(TestCase):
    """The search predicate must have an index-servable path, not only a scan."""

    def test_item_title_ilike_can_use_an_index(self):
        plan = _plan_for(
            "SELECT id FROM tasks_task WHERE title ILIKE %s", ["%" + PROBE + "%"]
        )
        self.assertIn("Bitmap Index Scan", plan, plan)
        self.assertIn("trgm", plan.lower(), plan)

    def test_item_description_ilike_can_use_an_index(self):
        plan = _plan_for(
            "SELECT id FROM tasks_task WHERE description ILIKE %s", ["%" + PROBE + "%"]
        )
        self.assertIn("Bitmap Index Scan", plan, plan)

    def test_item_key_ilike_can_use_an_index(self):
        plan = _plan_for(
            "SELECT id FROM tasks_task WHERE item_key ILIKE %s", ["%" + PROBE + "%"]
        )
        self.assertIn("Bitmap Index Scan", plan, plan)

    def test_comment_content_ilike_can_use_an_index(self):
        """#135 narrowed the comment join to a `pk__in` subquery, but that
        subquery is still an ILIKE over tasks_comment. Unindexed, it re-caps the
        whole search at the comment scan."""
        plan = _plan_for(
            "SELECT item_id FROM tasks_comment WHERE content ILIKE %s",
            ["%" + PROBE + "%"],
        )
        self.assertIn("Bitmap Index Scan", plan, plan)

    def test_control_an_unindexed_text_column_still_scans(self):
        """The control that makes the four assertions above mean something.

        If `enable_seqscan = off` alone were enough to produce a bitmap scan --
        or if EXPLAIN output happened to contain these strings for unrelated
        reasons -- every test here would pass with the migration reverted. This
        probes a text column deliberately NOT covered by 0135 and requires it to
        still report a sequential scan, proving the assertions above are
        detecting the indexes rather than the setting.
        """
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'tasks_task' AND data_type IN ('text','character varying') "
                "AND column_name NOT IN ('title','description','item_key') LIMIT 1"
            )
            row = cursor.fetchone()
        self.assertIsNotNone(row, "no uncovered text column to use as a control")
        plan = _plan_for(
            'SELECT id FROM tasks_task WHERE "%s" ILIKE %%s' % row[0],
            ["%" + PROBE + "%"],
        )
        self.assertIn("Seq Scan", plan, plan)
        self.assertNotIn("Bitmap Index Scan", plan, plan)


class LabelPredicateIsIndexServable(TestCase):
    """The two PLS-712 jsonb-containment arms must share a GIN path."""

    def test_array_or_object_label_containment_uses_the_labels_gin(self):
        plan = _plan_for(
            "SELECT id FROM tasks_task "
            "WHERE labels @> %s::jsonb OR labels @> %s::jsonb",
            [
                json.dumps(['repo:shizuha-labs/drive']),
                json.dumps({'repo': 'shizuha-labs/drive'}),
            ],
        )
        self.assertIn("Bitmap Index Scan", plan, plan)
        self.assertIn("pls712_item_labels_gin", plan, plan)

    def test_control_unindexed_jsonb_field_still_scans(self):
        """Prove the plan instrument can report the missing-index shape."""
        plan = _plan_for(
            "SELECT id FROM tasks_task WHERE metadata @> %s::jsonb",
            [json.dumps({'repo': 'shizuha-labs/drive'})],
        )
        self.assertIn("Seq Scan", plan, plan)
        self.assertNotIn("pls712_item_labels_gin", plan, plan)


class TaskLabelConsumerGuard(SimpleTestCase):
    """New task-label ORM consumers must use tasks.label_utils (PLS-712)."""

    def test_direct_label_lookups_are_centralized(self):
        root = Path(__file__).resolve().parents[2]
        violations = []
        for path in (root / 'tasks').rglob('*.py'):
            relative = path.relative_to(root).as_posix()
            if (
                '/tests/' in f'/{relative}'
                or '/migrations/' in f'/{relative}'
                or relative == 'tasks/label_utils.py'
            ):
                continue
            tree = ast.parse(path.read_text(), filename=relative)
            parents = {
                child: parent
                for parent in ast.walk(tree)
                for child in ast.iter_child_nodes(parent)
            }
            for node in ast.walk(tree):
                if not (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and 'labels__' in node.value
                ):
                    continue
                scope = node
                class_name = function_name = None
                while scope in parents:
                    scope = parents[scope]
                    if function_name is None and isinstance(
                        scope, (ast.FunctionDef, ast.AsyncFunctionDef)
                    ):
                        function_name = scope.name
                    if class_name is None and isinstance(scope, ast.ClassDef):
                        class_name = scope.name
                # Alert labels have a separate, object-only Prometheus contract;
                # Architecture explicitly kept this bulk-ack filter unchanged.
                if (
                    relative == 'tasks/views.py'
                    and class_name == 'AlertBulkAcknowledgeView'
                    and function_name == 'post'
                ):
                    continue
                violations.append(f'{relative}:{node.lineno}')

        self.assertEqual(
            violations,
            [],
            'Direct task-label JSON lookups bypass the canonical dual-shape '
            f'helper: {violations}',
        )
