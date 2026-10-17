# HIVE-1153 P1 #3 arming (HIVE-1174 decision (b); akira enforce-contract confirm on
# HIVE-1176): split the 0113 combined guard into two triggers so a BigAutoField Item
# INSERT can be receipt-bound after its pk is generated, and make the write-receipt's
# ``item`` FK nullable for the pre-insert arm.
#
#   * ``tasks_item_substrate_update_guard`` — BEFORE UPDATE OF the guarded columns.
#     Concrete-owner authorizing UPDATE must consume the ORM-armed one-use receipt,
#     matched by the tx-local GUC UUID + exact NEW tuple + assignment_generation +
#     requirement_key + write_input_digest. A ``SET x=x`` (no guarded column actually
#     changed OLD≠NEW) is a no-op early-return (the ORM did not (re)authorize a seat,
#     so no receipt is required) — the conservative-arm counterpart akira accepted.
#   * ``tasks_item_substrate_insert_guard`` — AFTER INSERT. Atomically claims the
#     pre-armed receipt (``item_id IS NULL``) and backfills ``item_id = NEW.id`` +
#     consumes it, matched by the GUC UUID + the full context tuple. Exactly one row
#     (id=rid is the PK ⇒ 0-or-1); zero ⇒ 42501 rolls back the INSERT.
#
# SAFE BY DEFAULT: both triggers gate on ``pulse.substrate_cap_mode`` and are strict
# no-ops unless it == 'enforce'. Activation is OFF (mode=off), so ordinary writes are
# never blocked; the production connection-level set of that GUC is a separate, still
# -deferred enforce-activation step. 0113's checks stored assignment_generation /
# requirement_key / write_input_digest but did not verify them — this migration verifies all
# three against the ORM-set GUCs, so a raw/bulk writer that guesses the receipt row
# still fails closed.
from django.db import migrations, models


# Drop 0113's combined BEFORE INSERT OR UPDATE trigger + function, then install the
# two split guards.
FORWARD_SQL = r"""
DROP TRIGGER IF EXISTS tasks_item_substrate_write_guard ON tasks_task;
DROP FUNCTION IF EXISTS tasks_guard_item_substrate_write();

-- HIVE-1153 (review P1 #3 + re-review P1 #2): the single canonical digest of the
-- resolver's requirement inputs (labels/tags/metadata/title/description), OLD ->
-- NEW. Both the guard triggers and the ORM armer call THIS function, so the digest
-- is computed from the ACTUAL row values via PostgreSQL — no Python re-serialisation
-- — so a stale/forged digest or a post-arm pre_save mutation of a guarded input
-- cannot match. The ten values are encoded as a jsonb ARRAY (jsonb_build_array):
-- an INJECTIVE, typed, self-delimiting structure. JSON escapes every control byte,
-- and NULL (JSON null) is distinct from '' (JSON ""), so an attacker-controlled
-- title/description carrying delimiter bytes can NOT straddle a field boundary to
-- alias a different (OLD,NEW) tuple (the flaw in the earlier \x01/\x02 concat).
-- Hashed with SHA-256 (64 hex chars).
CREATE OR REPLACE FUNCTION tasks_substrate_reqinput_digest(
    old_labels jsonb, old_tags text, old_metadata jsonb, old_title text, old_description text,
    new_labels jsonb, new_tags text, new_metadata jsonb, new_title text, new_description text
) RETURNS text LANGUAGE sql IMMUTABLE AS $fn$
    SELECT encode(
        sha256(convert_to(
            jsonb_build_array(
                old_labels, old_tags, old_metadata, old_title, old_description,
                new_labels, new_tags, new_metadata, new_title, new_description
            )::text, 'UTF8')),
        'hex')
$fn$;

CREATE OR REPLACE FUNCTION tasks_guard_item_substrate_update()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    cap_mode text;
    new_org bigint;
    rid uuid;
    matched uuid;
    changed boolean;
BEGIN
    -- SAFE default: only ENFORCE gates writes. off/shadow/unset -> no-op.
    cap_mode := current_setting('pulse.substrate_cap_mode', true);
    IF cap_mode IS DISTINCT FROM 'enforce' THEN
        RETURN NEW;
    END IF;
    -- owner-clears are exempt: no concrete owner is installed by this write.
    IF NEW.assignee_id IS NULL AND coalesce(NEW.assignee_email, '') = '' THEN
        RETURN NEW;
    END IF;
    -- No-op early-return for SET x=x: if no guarded column actually changed, the ORM
    -- did not (re)authorize a seat and armed no receipt, so require none. Mirrors the
    -- ORM's conservative-arm decision (akira HIVE-1176 confirm #1).
    changed := (
        OLD.assignee_id IS DISTINCT FROM NEW.assignee_id
        OR coalesce(OLD.assignee_email, '') IS DISTINCT FROM coalesce(NEW.assignee_email, '')
        OR OLD.project_id IS DISTINCT FROM NEW.project_id
        OR coalesce(OLD.assignment_group, '') IS DISTINCT FROM coalesce(NEW.assignment_group, '')
        OR coalesce(OLD.workflow_name, '') IS DISTINCT FROM coalesce(NEW.workflow_name, '')
        OR coalesce(OLD.status, '') IS DISTINCT FROM coalesce(NEW.status, '')
        OR OLD.labels IS DISTINCT FROM NEW.labels
        OR OLD.tags IS DISTINCT FROM NEW.tags
        OR OLD.metadata IS DISTINCT FROM NEW.metadata
        OR coalesce(OLD.title, '') IS DISTINCT FROM coalesce(NEW.title, '')
        OR coalesce(OLD.description, '') IS DISTINCT FROM coalesce(NEW.description, '')
    );
    IF NOT changed THEN
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
    -- Raw SQL / QuerySet.update never arm a receipt nor set the GUCs, so they fail
    -- closed under enforce. assignment_generation / requirement_key / write_input_digest
    -- are verified against the ORM-set GUCs (0113 stored but did not check them).
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
       AND assignment_generation = coalesce(
             nullif(current_setting('pulse.substrate_write_generation', true), '')::bigint, 0)
       AND requirement_key = coalesce(current_setting('pulse.substrate_write_requirement_key', true), '')
       -- Verify the digest against the ACTUAL OLD and NEW requirement inputs of
       -- THIS row (not an ORM-supplied GUC), so a guarded input mutated after the
       -- ORM armed cannot be consumed under a stale digest (review P1 #3).
       AND write_input_digest = tasks_substrate_reqinput_digest(
             OLD.labels, OLD.tags, OLD.metadata, OLD.title, OLD.description,
             NEW.labels, NEW.tags, NEW.metadata, NEW.title, NEW.description)
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

CREATE TRIGGER tasks_item_substrate_update_guard
BEFORE UPDATE OF assignee_id, assignee_email, project_id, assignment_group,
    workflow_name, status, labels, tags, metadata, title, description
ON tasks_task
FOR EACH ROW EXECUTE FUNCTION tasks_guard_item_substrate_update();

CREATE OR REPLACE FUNCTION tasks_guard_item_substrate_insert()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    cap_mode text;
    new_org bigint;
    rid uuid;
    matched uuid;
BEGIN
    cap_mode := current_setting('pulse.substrate_cap_mode', true);
    IF cap_mode IS DISTINCT FROM 'enforce' THEN
        RETURN NULL;
    END IF;
    -- owner-clears are exempt (no concrete owner installed by this INSERT).
    IF NEW.assignee_id IS NULL AND coalesce(NEW.assignee_email, '') = '' THEN
        RETURN NULL;
    END IF;
    IF NEW.project_id IS NOT NULL THEN
        SELECT org_id INTO new_org FROM tasks_project WHERE id = NEW.project_id;
    END IF;
    BEGIN
        rid := nullif(current_setting('pulse.substrate_write_receipt', true), '')::uuid;
    EXCEPTION WHEN OTHERS THEN
        rid := NULL;
    END;
    -- Atomic claim/backfill: bind the pre-armed (item_id IS NULL) receipt to the new
    -- pk and consume it in one statement. id=rid is the PK, so this matches 0 or 1
    -- rows; a second same-tuple INSERT finds item_id already set -> 0 rows -> 42501,
    -- which rolls the INSERT back. No context-only lookup: the GUC UUID is primary.
    UPDATE tasks_substrate_write_receipt
       SET item_id = NEW.id, consumed_at = clock_timestamp()
     WHERE id = rid
       AND item_id IS NULL
       AND consumed_at IS NULL
       AND organization_id = coalesce(new_org, 0)
       AND project_id IS NOT DISTINCT FROM NEW.project_id
       AND assignment_group = replace(lower(coalesce(NEW.assignment_group, '')), '_', '-')
       AND workflow_name = coalesce(NEW.workflow_name, '')
       AND status = coalesce(NEW.status, '')
       AND subject_id IS NOT DISTINCT FROM NEW.assignee_id
       AND lower(assignee_email) = lower(coalesce(NEW.assignee_email, ''))
       AND assignment_generation = coalesce(
             nullif(current_setting('pulse.substrate_write_generation', true), '')::bigint, 0)
       AND requirement_key = coalesce(current_setting('pulse.substrate_write_requirement_key', true), '')
       -- INSERT has no OLD row: bind the digest to the NEW requirement inputs only
       -- (OLD block is NULL/empty), computed from the actual inserted values.
       AND write_input_digest = tasks_substrate_reqinput_digest(
             NULL, NULL, NULL, NULL, NULL,
             NEW.labels, NEW.tags, NEW.metadata, NEW.title, NEW.description)
       AND mode = 'enforce'
       AND expires_at >= clock_timestamp()
     RETURNING id INTO matched;
    IF matched IS NULL THEN
        RAISE EXCEPTION 'substrate_capability:substrate_write_receipt_required'
          USING ERRCODE = '42501';
    END IF;
    RETURN NULL;
END;
$$;

CREATE TRIGGER tasks_item_substrate_insert_guard
AFTER INSERT ON tasks_task
FOR EACH ROW EXECUTE FUNCTION tasks_guard_item_substrate_insert();
"""

# Reverse: drop the split guards and restore 0113's combined trigger verbatim.
REVERSE_SQL = r"""
DROP TRIGGER IF EXISTS tasks_item_substrate_update_guard ON tasks_task;
DROP FUNCTION IF EXISTS tasks_guard_item_substrate_update();
DROP TRIGGER IF EXISTS tasks_item_substrate_insert_guard ON tasks_task;
DROP FUNCTION IF EXISTS tasks_guard_item_substrate_insert();
DROP FUNCTION IF EXISTS tasks_substrate_reqinput_digest(
    jsonb, text, jsonb, text, text, jsonb, text, jsonb, text, text);

CREATE OR REPLACE FUNCTION tasks_guard_item_substrate_write()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    cap_mode text;
    new_org bigint;
    matched uuid;
    rid uuid;
BEGIN
    cap_mode := current_setting('pulse.substrate_cap_mode', true);
    IF cap_mode IS DISTINCT FROM 'enforce' THEN
        RETURN NEW;
    END IF;
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


class Migration(migrations.Migration):
    dependencies = [
        ('tasks', '0120_hive1153_substrate_write_receipt_trigger'),
    ]

    operations = [
        migrations.AlterField(
            model_name='substratewritereceipt',
            name='item',
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=models.PROTECT,
                related_name='substrate_write_receipts',
                to='tasks.item'),
        ),
        migrations.RunSQL(sql=FORWARD_SQL, reverse_sql=REVERSE_SQL),
    ]
