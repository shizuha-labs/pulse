from django.db import migrations


class Migration(migrations.Migration):

    # PLAT-4182 close-lane fix (sara, 2026-11-17): the premerge gate on
    # pulse#336 died with "Conflicting migrations detected; multiple leaf
    # nodes: (0190_merge_0169_0189, 0205_merge_0204s in tasks)". This branch
    # carries `0169_plat4182_qnt_artifact_retention_gate` (this PR's migration)
    # joined to main's history via `0190_merge_0169_0189`; main meanwhile grew
    # its own chain through `0205_merge_0204s`. The base-sync merge brought
    # both chains into one tree but nothing referenced the 0190 join node, so
    # both leaves persisted (PLS-699/PLS-702 two-leaf class). This merge node
    # joins them; content-neutral, no operations.
    dependencies = [
        ("tasks", "0190_merge_0169_0189"),
        ("tasks", "0205_merge_0204s"),
    ]

    operations = []
