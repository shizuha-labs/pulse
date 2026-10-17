"""Merge the two 0175 leaves landed from parallel lines.

Main landed 0175_plat5355_triage_to_devops (PLAT-5355) while this slice
landed 0175_plat8342_mutation_receipt_outbox (PLAT-8342 s3); the review-board
lap merge brought both into one tree, giving the tasks migration graph two
leaf nodes (CommandError: Conflicting migrations detected). This empty merge
node unifies them, mirroring 0174_merge_0173_leaves.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("tasks", "0175_plat5355_triage_to_devops"),
        ("tasks", "0175_plat8342_mutation_receipt_outbox"),
    ]

    operations = []
