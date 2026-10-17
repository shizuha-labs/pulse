"""GET /api/items/?search= must not join tasks_comment.

Incident, 2026-07-26: Pulse went to 0/9 Ready and the Service lost every
endpoint, so MCP, shizuha-id's token mint and the admin proxy all took 502s and
read-timeouts. The cause was not the probe, the image roll, or node pressure —
`platform-db-1` showed 63 active queries against 111 connections, all of them
`SELECT DISTINCT <~95 cols> FROM tasks_task ...` running 80-93s with wait_event
IPC/ExecuteGather and no lock waits, against a table holding only 10,591 rows.

The search filter matched comments with a related-field lookup::

    Q(item_key__icontains=token) | ... | Q(comments__content__icontains=token)

Django compiles that to a join onto tasks_comment. It is ONE join, not one per
token — a single `filter()` call reuses the join for &-combined children — but
one is enough, because of what the join carries. Live EXPLAIN of the three-token
form:

    Unique  (width=3818)
      -> Sort   (Sort Key: <~95 columns>)
        -> Gather / Parallel Hash Right Join  (width=3818)
             Hash Cond: (c.item_id = t.id)
             Filter: <every ILIKE, applied AFTER the join>
             -> Parallel Seq Scan on tasks_comment c  (rows=76432 width=398)

Three things go wrong at once, and all of them are the join:

1. The join fans 10,591 items out to 192,126 rows.
2. Each fanned row carries the whole item — `width=3818`, ~95 columns,
   descriptions averaging 1.4KB — so the join materialises roughly 730MB.
3. The predicates are evaluated after the fan-out, so an 8-token search runs up
   to 32 ILIKEs per fanned row instead of per item, and then `.distinct()` has
   to Sort+Unique that entire wide set on a 95-column sort key.

The fixed form compiles to an item scan with narrow SubPlans that project
item_id (`width=8`). Item-side ILIKEs and DISTINCT run over the unfanned 10,591
rows. Token-AND comment matching stays inside one Comment subquery because the
legacy join required comment-only tokens to co-occur in the same comment.

Then amplification became an outage: :8002 is served by 5 sync workers, so a few
of these fill a pod, the readiness probe (`urlopen(/api/health/, timeout=2)`)
queues behind them and fails, the pod leaves the Service, and the survivors
absorb more load. Agents whose search timed out retried, and search-before-create
with 3-6 specific words is exactly what pulse-core and wiki-lifecycle instruct
every agent to do, so the fleet's own discipline was the load generator.

These tests assert the SQL SHAPE, not a duration. A timing assertion would pass
on an empty test database no matter how the clause is built; the presence of the
join is what actually differs, and it is what regressed.
"""
import re

from django.db import connection
from django.test.utils import CaptureQueriesContext

from tasks.models import Comment, Item, Project
from tasks.tests.base import AuthenticatedAPITestCase

ITEMS = '/api/items/'

# Matches `JOIN "tasks_comment"` in any flavour (INNER / LEFT OUTER / hash),
# which is the fan-out form. The fixed clause still mentions tasks_comment, but
# as `IN (SELECT U0."item_id" FROM "tasks_comment" ...)` — a hashed SubPlan over
# a width=8 projection — so a bare substring check would not distinguish the
# regression from the fix.
_COMMENT_JOIN = re.compile(r'JOIN\s+"?tasks_comment"?', re.IGNORECASE)


def _comment_joins(sql):
    return len(_COMMENT_JOIN.findall(sql))


class SearchCommentJoinTests(AuthenticatedAPITestCase):

    def setUp(self):
        super().setUp()
        self.project = Project.objects.create(
            name='Platform', key='PLAT', org_id=1, access_level='open')
        self.item = Item.objects.create(
            project=self.project, item_key='PLAT-9001',
            title='Bound the Pulse proxy timeout', mode='task', status='open',
            description='Admin proxies to Pulse while shizuha-id waits 1.0s.',
            created_by_email='mio@shizuha.com', created_by_id=0,
        )
        # Several comments on one item is the shape that fans out: with a join,
        # this item contributes five ~3.8KB rows to the intermediate result and
        # every search predicate is evaluated against all five.
        for n in range(5):
            Comment.objects.create(
                item=self.item, author_id=5, author_email='mio@shizuha.com',
                content=f'Investigation note {n}: gunicorn worker pool saturated.')

    def _search_sql(self, query):
        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(ITEMS, {'search': query})
            self.assertEqual(response.status_code, 200)
        return [entry['sql'] for entry in captured.captured_queries]

    def test_single_token_search_does_not_join_the_comment_table(self):
        for sql in self._search_sql('gunicorn'):
            self.assertEqual(
                _comment_joins(sql), 0,
                'searching comments must compile to `pk__in (SELECT item_id '
                'FROM tasks_comment ...)`, not a join — the join is what fanned '
                f'10.5k items to 192k rows of width=3818:\n{sql}',
            )

    def test_exact_item_key_search_uses_keyed_predicate_without_comment_scan(self):
        """Compatibility ``search=<KEY>`` must be as cheap as ``item_key=<KEY>``.

        Exact-key reads are the dominant operator/agent lookup.  Sending them
        through comment search caused live reads to hit statement_timeout even
        though ``tasks_task.item_key`` is unique and indexed.
        """
        referenced = Item.objects.create(
            project=self.project, item_key='PLAT-9006',
            title='Mentions another item', mode='task', status='open',
            description='No key in item fields.',
            created_by_email='mio@shizuha.com', created_by_id=0,
        )
        Comment.objects.create(
            item=referenced, author_id=5, author_email='mio@shizuha.com',
            content='This comment references PLAT-9001.',
        )

        for endpoint in ('/api/items/', '/api/tasks/'):
            with self.subTest(endpoint=endpoint):
                with CaptureQueriesContext(connection) as captured:
                    response = self.client.get(endpoint, {'search': 'plat-9001'})

                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    [row['item_key'] for row in response.data['results']],
                    ['PLAT-9001'],
                )
                item_sql = [
                    entry['sql'] for entry in captured.captured_queries
                    if 'FROM "tasks_task"' in entry['sql']
                ]
                self.assertTrue(item_sql)
                for statement in item_sql:
                    self.assertNotIn(
                        'FROM "tasks_comment"', statement,
                        f'exact key lookup compiled a comment subquery:\n{statement}',
                    )
                    self.assertNotIn(
                        '"tasks_task"."title"::text', statement,
                        f'exact key lookup compiled broad item text search:\n{statement}',
                    )
                    self.assertNotIn(
                        'UPPER(', statement,
                        f'exact key lookup bypassed the unique btree index:\n{statement}',
                    )

    def test_multi_token_search_does_not_join_the_comment_table(self):
        """The realistic case: pulse-core tells agents to search 3-6 words.

        Worth asserting separately from the single-token case, because the
        per-token loop builds its clause differently from the whole-phrase one
        and a partial fix could easily leave only this path joining.
        """
        for sql in self._search_sql('gunicorn worker pool saturated investigation note'):
            self.assertEqual(
                _comment_joins(sql), 0,
                'a multi-token search reintroduced a join onto tasks_comment — '
                'that is the exact shape that took Pulse to 0/9 Ready:\n'
                f'{sql}',
            )

    def test_distinct_operates_on_the_unfanned_item_set(self):
        """DISTINCT was never the expensive part — its input was.

        With the join gone, the row set DISTINCT has to Sort+Unique is bounded
        by the items table rather than by the comments table, so this asserts
        the property that made the 95-column sort key survivable rather than
        asserting that .distinct() was removed (it should not be: earlier
        filters in get_queryset can still join a multi-valued relation).
        """
        sql = [s for s in self._search_sql('gunicorn worker pool') if 'DISTINCT' in s]
        self.assertTrue(sql, 'expected the item search query to still use DISTINCT')
        for statement in sql:
            self.assertEqual(_comment_joins(statement), 0)

    def test_search_still_matches_text_that_only_exists_in_a_comment(self):
        """Semantics preserved: the semi-join must find the same items."""
        response = self.client.get(ITEMS, {'search': 'saturated'})
        self.assertEqual(response.status_code, 200)
        keys = [row['item_key'] for row in response.data['results']]
        self.assertIn('PLAT-9001', keys)

    def test_token_and_still_spans_title_and_comment_bodies(self):
        """A token in the title AND a token only in a comment must still match.

        This is the behaviour the per-token loop exists to provide (finding a
        prior decision whose wording differs slightly), so the fix has to keep
        it — one semi-join per token does; folding the tokens together would
        not.
        """
        response = self.client.get(ITEMS, {'search': 'timeout saturated'})
        self.assertEqual(response.status_code, 200)
        keys = [row['item_key'] for row in response.data['results']]
        self.assertIn('PLAT-9001', keys)

    def test_token_and_preserves_same_comment_membership_and_order(self):
        """Split comments must not witness separate tokens (Guard Design R6).

        The legacy query reused one comment join alias, so `alpha beta` matched
        a comment containing both words but not two comments containing one word
        each. The optimized query must preserve that membership set and the
        existing newest-first order.
        """
        split = Item.objects.create(
            project=self.project, item_key='PLAT-9002',
            title='Separate witnesses', mode='task', status='open',
            description='No matching search vocabulary here.',
            created_by_email='mio@shizuha.com', created_by_id=0,
        )
        Comment.objects.create(
            item=split, author_id=5, author_email='mio@shizuha.com',
            content='alpha only',
        )
        Comment.objects.create(
            item=split, author_id=5, author_email='mio@shizuha.com',
            content='beta only',
        )
        older_same = Item.objects.create(
            project=self.project, item_key='PLAT-9003',
            title='One witness older', mode='task', status='open',
            description='No matching search vocabulary here.',
            created_by_email='mio@shizuha.com', created_by_id=0,
        )
        Comment.objects.create(
            item=older_same, author_id=5, author_email='mio@shizuha.com',
            content='alpha and beta together',
        )
        newer_same = Item.objects.create(
            project=self.project, item_key='PLAT-9004',
            title='One witness newer', mode='task', status='open',
            description='No matching search vocabulary here.',
            created_by_email='mio@shizuha.com', created_by_id=0,
        )
        Comment.objects.create(
            item=newer_same, author_id=5, author_email='mio@shizuha.com',
            content='beta follows alpha in this same comment',
        )

        response = self.client.get(ITEMS, {'search': 'alpha beta'})

        self.assertEqual(response.status_code, 200)
        keys = [row['item_key'] for row in response.data['results']]
        self.assertNotIn('PLAT-9002', keys)
        self.assertEqual(keys, ['PLAT-9004', 'PLAT-9003'])

    def test_token_and_item_only_match_does_not_require_a_comment(self):
        """The same-comment subquery must not exclude zero-comment items."""
        Item.objects.create(
            project=self.project, item_key='PLAT-9005',
            title='Orphan search carrier', mode='task', status='open',
            description='This decision is independently discoverable.',
            created_by_email='mio@shizuha.com', created_by_id=0,
        )

        response = self.client.get(ITEMS, {'search': 'orphan discoverable'})

        self.assertEqual(response.status_code, 200)
        keys = [row['item_key'] for row in response.data['results']]
        self.assertIn('PLAT-9005', keys)

    def test_non_matching_search_returns_nothing(self):
        """Guards the other direction: the semi-join must not be fail-open."""
        response = self.client.get(ITEMS, {'search': 'zzzznotpresentanywhere'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['results'], [])
