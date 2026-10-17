"""PLS-705 AC2: make search indexable instead of a full table scan.

`/api/items/?search=` builds `item_key/title/description __icontains` plus a
narrow Comment subquery on `content__icontains`. `icontains` compiles to
`ILIKE '%needle%'` -- a LEADING wildcard, which no btree index can serve,
because a btree is ordered by prefix and a leading `%` discards that ordering.

So every search was a sequential scan with per-row predicate evaluation, and
the cost was paid in full *before* anything matched. That is exactly the
selectivity-independence @reika measured: 0 matches (15.2-16.1s), 1 match
(15.2-26.6s) and 8,160 matches (20.9s) all cost the same, because selectivity
cannot affect a cost incurred before selection.

PLS-705's earlier fix (#135) removed the comment-join fan-out -- 10,591 items
fanning to 192,126 rows at width=3818, ~730MB -- which was real and was the
80-93s regime. It could not remove the scan, because the scan is imposed by the
predicate SHAPE, not by the join. Two stacked costs; that PR addressed one.

`pg_trgm` GIN indexes make `LIKE`/`ILIKE` with leading wildcards indexable, so
the predicate stops being O(table).

Two deliberate choices:

* **CONCURRENTLY.** `tasks_task` serves every Pulse request. A plain
  `CREATE INDEX` takes an ACCESS EXCLUSIVE lock for the build; on the table
  whose saturation caused this outage, that trades a latency incident for an
  availability one. Concurrent builds do not block reads or writes. This is why
  the migration is non-atomic -- `CREATE INDEX CONCURRENTLY` cannot run inside
  a transaction block.
* **Extension first, as its own operation.** `TrigramExtension()` issues
  `CREATE EXTENSION IF NOT EXISTS pg_trgm`, which requires privileges the
  application role may not hold in every environment. Keeping it as the first
  discrete operation makes a privilege failure fail loudly and obviously there,
  rather than surfacing as a confusing index error.

Known limit, recorded rather than hidden: trigram indexes cannot serve search
terms shorter than 3 characters, since there are no full trigrams to look up.
Those queries still scan. That is acceptable and is not a gap in the fix -- a
1-2 character term matches most of the table, so it is inherently O(table)
output and has no cheap plan to find.
"""
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.operations import AddIndexConcurrently, TrigramExtension
from django.db import migrations


class Migration(migrations.Migration):

    # CREATE INDEX CONCURRENTLY cannot run inside a transaction.
    atomic = False

    dependencies = [
        ("tasks", "0134_pls698_active_queue_owner_index"),
    ]

    operations = [
        TrigramExtension(),
        AddIndexConcurrently(
            model_name="item",
            index=GinIndex(
                fields=["title"],
                opclasses=["gin_trgm_ops"],
                name="pls705_item_title_trgm",
            ),
        ),
        AddIndexConcurrently(
            model_name="item",
            index=GinIndex(
                fields=["description"],
                opclasses=["gin_trgm_ops"],
                name="pls705_item_desc_trgm",
            ),
        ),
        AddIndexConcurrently(
            model_name="item",
            index=GinIndex(
                fields=["item_key"],
                opclasses=["gin_trgm_ops"],
                name="pls705_item_key_trgm",
            ),
        ),
        # The comment predicate runs inside the narrow `pk__in` subquery from
        # #135. That subquery is still an unindexed ILIKE over tasks_comment,
        # so leaving it out would cap the win at whatever the comment scan costs.
        AddIndexConcurrently(
            model_name="comment",
            index=GinIndex(
                fields=["content"],
                opclasses=["gin_trgm_ops"],
                name="pls705_comment_content_trgm",
            ),
        ),
    ]
