# CI run 8695 (pulse#585, commit 77199ad4): premerge refused — Conflicting
# migrations detected; multiple leaf nodes in the migration graph:
# (0190_merge_pls1306_idx_plat10083_provider_wait, 0190_plat9355_heal_simple_ops_acceptance_mode in tasks).
# The update-endpoint rebase imported master's 0190_merge leaf alongside this
# branch's 0190_plat9355 heal. This migration reunifies the graph; no schema
# operations (the mint-rate/celery work and the acceptance-mode data heal are
# independent, each already applied by its own migration). Same shape as the
# PLS-1306 reunifier (72e65b46, CI 8475).
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0190_merge_pls1306_idx_plat10083_provider_wait'),
        ('tasks', '0190_plat9355_heal_simple_ops_acceptance_mode'),
    ]

    operations = []
