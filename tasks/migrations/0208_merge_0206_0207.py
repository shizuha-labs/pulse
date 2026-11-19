from django.db import migrations


class Migration(migrations.Migration):
    """Union the two leaf nodes left by the PLAT-13440 branch lap.

    The branch carries 0206_plat13440_security_finding_verified_present
    (deps: 0205_merge_0204s); main independently advanced through
    0207_merge_plat11284_found_stage_reject_gate (deps: 0191_plat11284...,
    0206_merge_plat4182_branch_with_main). The lap merged main into the
    branch, leaving two leaves in the tasks migration graph — Django
    refuses to run with multiple leaves (CommandError at premerge run
    11286). Union them so the graph has a single leaf again.
    """

    dependencies = [
        ('tasks', '0206_plat13440_security_finding_verified_present'),
        ('tasks', '0207_merge_plat11284_found_stage_reject_gate'),
    ]

    operations = []
