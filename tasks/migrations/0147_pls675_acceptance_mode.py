# PLS-675 / PLAT-1351 v4: typed acceptance_mode on Item.
# Enforcement authority for the deploy-verify close gate (code|deployed|live).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0146_alter_workflowtransition_unique_together'),
    ]

    operations = [
        migrations.AddField(
            model_name='item',
            name='acceptance_mode',
            field=models.CharField(
                blank=True,
                choices=[
                    ('code', 'Code'),
                    ('deployed', 'Deployed'),
                    ('live', 'Live'),
                ],
                db_index=True,
                help_text=(
                    "PLS-675/PLAT-1351 typed acceptance mode: code|deployed|live. "
                    "Null = unresolved; gate derives default for service-affecting/"
                    "user-facing work → live. Enforcement authority is this field, "
                    "not labels or free-form comments."
                ),
                max_length=16,
                null=True,
            ),
        ),
    ]
