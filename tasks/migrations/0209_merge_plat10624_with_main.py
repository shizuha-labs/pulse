"""Merge migration: PLAT-10624 branch leaf vs current main leaf.

Premerge run 11410 on b66e2532 (combined tree with main 6ed08ac2):
Conflicting migrations detected; multiple leaf nodes in the migration graph:
(0202_merge_plat10624_plat10787, 0208_merge_0206_0207 in tasks).
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("tasks", "0202_merge_plat10624_plat10787"),
        ("tasks", "0208_merge_0206_0207"),
    ]

    operations = []
