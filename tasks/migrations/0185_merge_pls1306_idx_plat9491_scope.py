# PLS-1306 lap fix (haru, run 7101): the sanctioned lap brought main's
# 0184_plat9491_scope_assertions (pulse#612 scope-stamps leg) against this
# branch's 0182_pls1306_urgent_unassigned_idx — multiple leaf nodes in the
# migration graph, `manage.py makemigrations --merge` class. This merge
# migration reunifies the graph; no schema operations (the covering index and
# the scope-assertion stamps are independent, both already applied by their
# own migrations).
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0182_pls1306_urgent_unassigned_idx'),
        ('tasks', '0184_plat9491_scope_assertions'),
    ]

    operations = []
