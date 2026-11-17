from django.db import migrations


class Migration(migrations.Migration):
    """Tie the PLAT-11284 branch leaf into main's graph.

    main advanced past this branch's base-sync (136512ba) through
    0204_plat13196_verified_present_close, 0205_merge_0204s and
    0206_merge_plat4182_branch_with_main, leaving two leaf nodes
    (0191_plat11284..., 0206_merge...). Union them so the migration
    graph has a single leaf again.
    """

    dependencies = [
        ('tasks', '0191_plat11284_found_stage_reject_live_boundary_gate'),
        ('tasks', '0206_merge_plat4182_branch_with_main'),
    ]

    operations = []
