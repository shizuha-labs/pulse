"""Merge the two migration-graph leaves on the PLS-832 update lap.

Main landed 0178_pls1244_itemactivity_action_created_at_index (PLS-1244) while
this slice carried 0173_pls832_deactivate_duplicate_project_memberships
(PLS-832); the sanctioned update lap (c77b7e65, merge of main f4918651) brought
both into one tree, giving the tasks migration graph two leaf nodes
(CommandError: Conflicting migrations detected — premerge run 6394, the same
class 0174/0176 already unified). This empty merge node unifies them, mirroring
0176_merge_0175_leaves.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("tasks", "0173_pls832_deactivate_duplicate_project_memberships"),
        ("tasks", "0178_pls1244_itemactivity_action_created_at_index"),
    ]

    operations = []
