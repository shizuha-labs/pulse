import uuid
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("tasks", "0141_pls796_seed_autonomous_bounce_workflows"),
    ]

    operations = [
        # PLS-481 / PLS-634: Add accepted-capacity-held fields to Item
        migrations.AddField(
            model_name="item",
            name="plan_hash",
            field=models.CharField(
                blank=True, max_length=64, null=True,
                help_text="PLS-481: immutable content-addressed plan hash (set by Accept — Capacity-Held).",
            ),
        ),
        migrations.AddField(
            model_name="item",
            name="plan_generation",
            field=models.PositiveIntegerField(
                blank=True, default=0, null=True,
                help_text="PLS-481: monotonic plan generation; bumped on re-acceptance.",
            ),
        ),
        migrations.AddField(
            model_name="item",
            name="capacity_reason",
            field=models.TextField(
                blank=True, default="", null=True,
                help_text="PLS-481: architect-supplied reason for holding capacity.",
            ),
        ),
        migrations.AddField(
            model_name="item",
            name="publication_binding",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="accepted_items", to="tasks.PublicationBinding",
                help_text="PLS-481: the PublicationBinding at the time of acceptance.",
            ),
        ),
        migrations.AddField(
            model_name="item",
            name="publication_round",
            field=models.CharField(
                blank=True, max_length=128, null=True,
                help_text="PLS-481: review_round_id from the accepted PublicationAttempt.",
            ),
        ),
        migrations.AddField(
            model_name="item",
            name="publication_result",
            field=models.CharField(
                blank=True, max_length=100, null=True,
                help_text="PLS-481: publication_result from the accepted PublicationAttempt.",
            ),
        ),
        # PLS-481 / PLS-634: HeldPlanTarget model for event-driven activation cursor
        migrations.CreateModel(
            name="HeldPlanTarget",
            fields=[
                ("id", models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)),
                ("target_team_id", models.CharField(max_length=100)),
                ("next_eligible_slice", models.PositiveIntegerField(default=0)),
                ("plan_generation", models.PositiveIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("item", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="held_plan_targets", to="tasks.item",
                )),
            ],
            options={
                "db_table": "tasks_held_plan_target",
            },
        ),
        migrations.AddConstraint(
            model_name="heldplantarget",
            constraint=models.UniqueConstraint(
                fields=["item", "target_team_id", "plan_generation"],
                name="uniq_held_plan_target",
            ),
        ),
    ]
