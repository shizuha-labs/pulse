"""Merge migration: PLAT-10624 leaf vs PLAT-10787 leaf.

Premerge run 9994 on 5ff5169:
Conflicting migrations detected; multiple leaf nodes in the migration graph:
(0191_plat10624_creator_scoped_request_id, 0201_plat10787_attestation_provider_key).

Premerge run 10163 on 389d7627 (PLAT-10624):
Conflicting migrations detected; multiple leaf nodes in the migration graph:
(0202_escalation_receipt_consumed_at, 0202_merge_plat10624_plat10787 in tasks).
Main reparented 0202_escalation_receipt_consumed_at onto 0201_plat10787
(44791588, PLAT-11158), creating a second leaf alongside this merge migration.
Adding it as a dependency collapses the graph back to a single leaf.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("tasks", "0191_plat10624_creator_scoped_request_id"),
        ("tasks", "0201_plat10787_attestation_provider_key"),
        ("tasks", "0202_escalation_receipt_consumed_at"),
    ]

    operations = []
