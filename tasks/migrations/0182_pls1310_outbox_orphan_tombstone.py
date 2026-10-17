"""PLS-1310 — tombstone + detach orphaned item-state outbox rows.

A hard item-delete that bypasses the ORM (the raw/SQL path) leaves the item's
``tasks_item_state_event_outbox`` rows behind as orphaned *pending* mutations
for a nonexistent item (datum: item 36111, PLAT-9264 forensic verdict). This
migration:

  1. makes ``item_id`` nullable (the FK becomes SET_NULL at the model level so
     the delete-time tombstone in ``tasks.signals`` can detach rows instead of
     cascade-destroying them);
  2. tombstones + detaches the EXISTING orphans in one pass —
     ``disposition='orphaned'``, ``stale_reason='orphaned-by-hard-delete'``,
     ``item_id=NULL`` — preserving the forensic trail (envelope, event UUID,
     timestamps untouched) while removing the live-mutation hazard (consumers
     drain 'pending' only; 'orphaned' is inert);
  3. syncs the model state (``AlterField`` to ``null=True, on_delete=SET_NULL``).
     The constraint re-creation validates existing rows — safe because step 2
     already detached every orphan (NULL passes FK validation).

The raw/SQL delete path can still create orphans going forward (it bypasses
the ORM and the pre_delete receiver); the ``tombstone_orphaned_outbox``
management command is the repeatable reconciliation pass for those.

Irreversible by nature (the deleted items are gone; detached rows cannot be
re-attached) — reverse SQL is a no-op.
"""
from django.db import migrations, models
import django.db.models.deletion


BACKFILL_SQL = r"""
-- Tombstone + detach every outbox row whose owning item no longer exists.
-- One pass: disposition flip, forensic marker, and FK detach together, so the
-- subsequent AlterField constraint validation sees only live or NULL parents.
UPDATE tasks_item_state_event_outbox AS o
SET disposition = 'orphaned',
    stale_reason = 'orphaned-by-hard-delete',
    item_id = NULL
WHERE o.item_id IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM tasks_task t WHERE t.id = o.item_id);
"""


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0181_pls527_operator_wait_evidence'),
    ]

    operations = [
        # 1. Column must be nullable before orphans can be detached. (The FK
        # constraint itself does not change here — on_delete is ORM-side.)
        migrations.RunSQL(
            sql="ALTER TABLE tasks_item_state_event_outbox "
                "ALTER COLUMN item_id DROP NOT NULL;",
            reverse_sql=migrations.RunSQL.noop,
        ),
        # 2. Tombstone + detach the existing orphans (item 36111's rows at
        # minimum). Envelope/event-UUID/timestamps preserved untouched.
        migrations.RunSQL(
            sql=BACKFILL_SQL,
            reverse_sql=migrations.RunSQL.noop,
        ),
        # 3. Sync the model state. Constraint re-validation passes: every
        # orphan is already detached (NULL passes FK validation).
        migrations.AlterField(
            model_name='itemstateeventoutbox',
            name='item',
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='state_event_outbox',
                to='tasks.item',
            ),
        ),
        # 4. Sync the disposition choices (PLS-1310 adds 'orphaned'). Without
        # this AlterField the models.py choices edit is unmigrated and
        # makemigrations --check in CI auto-generates 0183 (caught on the
        # first premerge run — the check did exactly its job).
        migrations.AlterField(
            model_name='itemstateeventoutbox',
            name='disposition',
            field=models.CharField(
                choices=[
                    ('pending', 'Pending'),
                    ('applied', 'Applied'),
                    ('stale', 'Stale'),
                    ('quarantined', 'Quarantined'),
                    ('orphaned', 'Orphaned'),
                ],
                db_index=True,
                default='pending',
                max_length=16,
            ),
        ),
    ]
