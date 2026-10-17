"""PLS-712: index the canonical dual-shape task-label predicate.

Both supported containment arms compile to jsonb ``@>``.  Use the default
``jsonb_ops`` GIN opclass rather than ``jsonb_path_ops`` so object-key query
semantics remain available.  The live task table serves every Pulse request,
therefore the index build must be concurrent and the migration non-atomic.
"""
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):

    atomic = False

    dependencies = [
        ("tasks", "0161_pls900_conflicted_parties"),
    ]

    operations = [
        migrations.AlterField(
            model_name="item",
            name="labels",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text=(
                    'Dual shape: task labels are JSON string arrays such as '
                    '["repo:shizuha-labs/drive"]; alert labels may be JSON '
                    'objects such as {"host": "web-01", "service": "nginx"}.'
                ),
            ),
        ),
        AddIndexConcurrently(
            model_name="item",
            index=GinIndex(fields=["labels"], name="pls712_item_labels_gin"),
        ),
    ]
