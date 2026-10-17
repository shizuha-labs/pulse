# PLS-373 (#444) added 0089_pls373_widen_itemactivity_action on 0088_pls272_objectives,
# concurrently with 0089_merge_orig73_pls273_main (which merged the two 0088 leaves).
# Both landed on main as parallel 0089 leaves with no reconciling node, so Django's
# migration graph had two leaf nodes -> `migrate`/`makemigrations --check` failed,
# redding the deploy.yml `test` job. This merge migration reconciles them into a
# single leaf. No schema operations.
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0089_merge_orig73_pls273_main'),
        ('tasks', '0089_pls373_widen_itemactivity_action'),
    ]

    operations = []
