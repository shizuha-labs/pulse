# Merge migration: converges the PLS-273 customer-portal lane
# (0088_sync_taskdraftproposal_field_metadata) with main's objectives lane
# (0088_pls272_objectives) after merging main into the PR branch (ORIG-73).
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("tasks", "0088_pls272_objectives"),
        ("tasks", "0088_sync_taskdraftproposal_field_metadata"),
    ]

    operations = []
