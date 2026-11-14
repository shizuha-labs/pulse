from django.db import migrations


class Migration(migrations.Migration):

    # PLAT-11947 / PLS-1461: the 23:20:32Z sync merge onto main brought
    # `0204_plat13196_verified_present_close` (via 57a4eb21) while this branch
    # already carried its own merge node `0204_merge_0202_0203` (deps:
    # 0202_plat11947_sweep_work_lease + 0203_merge_escalation_receipt_plat9355_branch).
    # Both 0204 nodes depend on 0203 but do not reference each other, leaving
    # the tasks migration graph with two leaf nodes — Django's loader kills
    # `manage.py check` / `makemigrations --check` / `manage.py test` at
    # startup (PLS-699/PLS-702 two-leaf class). This merge node joins them.
    dependencies = [
        ("tasks", "0204_merge_0202_0203"),
        ("tasks", "0204_plat13196_verified_present_close"),
    ]

    operations = []
