# PLAT-1280: merge migration for the two 0074 leaves that landed via parallel PRs
# (0074_pls194_repair_transition_articles from PR #237, 0074_pls196_backfill_completed_at
# from PR #251). Two leaf nodes make `manage.py test`/`migrate` fail with
# "Conflicting migrations detected", which blocked every CI gate and prod deploy of main.
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0074_pls194_repair_transition_articles'),
        ('tasks', '0074_pls196_backfill_completed_at'),
    ]

    operations = []
