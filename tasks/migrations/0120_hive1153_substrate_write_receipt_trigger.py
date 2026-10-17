# HIVE-1153 P1 #3 (HIVE-1174 decision (b)): a one-use PG substrate-write receipt + a
# BEFORE INSERT/UPDATE trigger so a QuerySet.update / raw SQL concrete-owner authorizing
# write cannot bypass the ORM's signed-evidence commit fence under ENFORCE.
#
# SAFE BY DEFAULT: the trigger gates on the session GUC pulse.substrate_cap_mode; it is a
# strict no-op unless that GUC == 'enforce'. Activation is OFF (mode=off), so ordinary
# writes are never blocked — the trigger only enforces once the ORM sets the GUC to
# 'enforce' AND arms a matching one-use receipt (arming lands in a follow-up commit).
import uuid

import django.db.models.deletion
from django.db import migrations, models


TRIGGER_SQL = r"""
CREATE OR REPLACE FUNCTION tasks_guard_item_substrate_write()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    cap_mode text;
    new_org bigint;
    matched uuid;
    rid uuid;
BEGIN
    -- SAFE default: only ENFORCE mode gates writes. off/shadow/unset -> no-op, so
    -- ordinary writes are never blocked while substrate activation is OFF.
    cap_mode := current_setting('pulse.substrate_cap_mode', true);
    IF cap_mode IS DISTINCT FROM 'enforce' THEN
        RETURN NEW;
    END IF;
    -- owner-clears are exempt: no concrete owner is installed by this write.
    IF NEW.assignee_id IS NULL AND coalesce(NEW.assignee_email, '') = '' THEN
        RETURN NEW;
    END IF;
    IF NEW.project_id IS NOT NULL THEN
        SELECT org_id INTO new_org FROM tasks_project WHERE id = NEW.project_id;
    END IF;
    BEGIN
        rid := nullif(current_setting('pulse.substrate_write_receipt', true), '')::uuid;
    EXCEPTION WHEN OTHERS THEN
        rid := NULL;
    END;
    -- Require the ONE-USE receipt the ORM armed for this exact concrete-owner write.
    -- Raw SQL / QuerySet.update never arm a receipt, so they fail closed under enforce.
    SELECT id INTO matched
      FROM tasks_substrate_write_receipt
     WHERE id = rid
       AND item_id = NEW.id
       AND organization_id = coalesce(new_org, 0)
       AND project_id IS NOT DISTINCT FROM NEW.project_id
       AND assignment_group = replace(lower(coalesce(NEW.assignment_group, '')), '_', '-')
       AND workflow_name = coalesce(NEW.workflow_name, '')
       AND status = coalesce(NEW.status, '')
       AND subject_id IS NOT DISTINCT FROM NEW.assignee_id
       AND lower(assignee_email) = lower(coalesce(NEW.assignee_email, ''))
       AND mode = 'enforce'
       AND consumed_at IS NULL
       AND expires_at >= clock_timestamp()
     FOR UPDATE;
    IF matched IS NULL THEN
        RAISE EXCEPTION 'substrate_capability:substrate_write_receipt_required'
          USING ERRCODE = '42501';
    END IF;
    UPDATE tasks_substrate_write_receipt SET consumed_at = clock_timestamp()
     WHERE id = matched AND consumed_at IS NULL;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'substrate_capability:substrate_write_receipt_reused'
          USING ERRCODE = '42501';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER tasks_item_substrate_write_guard
BEFORE INSERT OR UPDATE OF assignee_id, assignee_email, project_id, assignment_group,
    workflow_name, status, labels, tags, metadata, title, description
ON tasks_task
FOR EACH ROW EXECUTE FUNCTION tasks_guard_item_substrate_write();
"""

REVERSE_SQL = r"""
DROP TRIGGER IF EXISTS tasks_item_substrate_write_guard ON tasks_task;
DROP FUNCTION IF EXISTS tasks_guard_item_substrate_write();
"""


class Migration(migrations.Migration):
    dependencies = [
        ('tasks', '0119_hive1153_substrate_materialization_high_water'),
    ]

    operations = [
        migrations.CreateModel(
            name='SubstrateWriteReceipt',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False,
                                        primary_key=True, serialize=False)),
                ('organization_id', models.PositiveIntegerField()),
                ('project_id', models.PositiveBigIntegerField(blank=True, null=True)),
                ('assignment_group', models.CharField(blank=True, default='', max_length=100)),
                ('workflow_name', models.CharField(blank=True, default='', max_length=100)),
                ('status', models.CharField(blank=True, default='', max_length=64)),
                ('subject_id', models.PositiveBigIntegerField(blank=True, null=True)),
                ('assignee_email', models.EmailField(blank=True, default='', max_length=254)),
                ('assignment_generation', models.PositiveBigIntegerField(default=0)),
                ('requirement_key', models.CharField(blank=True, default='', max_length=128)),
                ('write_input_digest', models.CharField(max_length=64)),
                ('mode', models.CharField(default='enforce', max_length=16)),
                ('expires_at', models.DateTimeField()),
                ('consumed_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('item', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                                           related_name='substrate_write_receipts',
                                           to='tasks.item')),
            ],
            options={'db_table': 'tasks_substrate_write_receipt'},
        ),
        migrations.AddIndex(
            model_name='substratewritereceipt',
            index=models.Index(fields=['item', 'consumed_at', 'expires_at'],
                               name='substrate_write_consume_idx'),
        ),
        migrations.RunSQL(sql=TRIGGER_SQL, reverse_sql=REVERSE_SQL),
    ]
