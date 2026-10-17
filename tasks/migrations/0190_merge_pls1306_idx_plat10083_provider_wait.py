# CI run 8475 (pulse#580, commit 8b51fd665): premerge refused — Conflicting
# migrations detected; multiple leaf nodes in the migration graph:
# (0185_merge_pls1306_idx_plat9491_scope, 0189_plat10083_provider_wait_marker).
# Branch-side leaf 0185_merge reunites the sanctioned-lap main 0184 with this
# branch's 0182; main then advanced 0185_plat9681 -> ... -> 0189
# (plat10083_provider_wait_marker). This migration reunifies the graph after
# the main merge; no schema operations (the outbox marker and the covering
# index are independent, each already applied by its own migration).
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0185_merge_pls1306_idx_plat9491_scope'),
        ('tasks', '0189_plat10083_provider_wait_marker'),
    ]

    operations = []
