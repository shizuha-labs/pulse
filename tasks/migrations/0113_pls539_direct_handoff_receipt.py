# Generated for PLS-539 (hiro/ADR-0010): teach the assignment receipt trigger to
# accept an authorized direct-handoff crossing OUT of a materialized guarded lane
# into a concrete owner in a (possibly unguarded) result lane. Before this, the
# trigger's ELSE branch unconditionally RAISEd assignment_receipt_required whenever
# new_team IS NULL with a new owner, so a direct operator override from any guarded
# lane was impossible at the DB layer (the PLAT-4837 / PLS-539 failure class).
from django.db import migrations, models

# Full re-CREATE OR REPLACE of tasks_guard_item_assignment() (base: migration 0099),
# with a direct_handoff branch added at the top of the non-clear ELSE. The ordinary
# assign path is unchanged and still runs when no direct_handoff receipt matches.
FORWARD_SQL = r"""
CREATE OR REPLACE FUNCTION tasks_guard_item_assignment()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    old_project_org bigint;
    new_project_org bigint;
    old_team uuid;
    new_team uuid;
    assignment_changed boolean;
    routing_context_changed boolean;
    old_assigned boolean;
    clear_write boolean;
    clear_action text;
    receipt_id uuid;
    matched_receipt uuid;
BEGIN
    IF TG_OP = 'INSERT'
       AND NEW.assignee_id IS NULL
       AND coalesce(NEW.assignee_email, '') = '' THEN
        RETURN NEW;
    END IF;

    IF TG_OP = 'UPDATE' THEN
        assignment_changed := (
            OLD.assignee_id IS DISTINCT FROM NEW.assignee_id
            OR lower(coalesce(OLD.assignee_email, ''))
               <> lower(coalesce(NEW.assignee_email, ''))
        );
        routing_context_changed := (
            OLD.project_id IS DISTINCT FROM NEW.project_id
            OR replace(lower(coalesce(OLD.assignment_group, '')), '_', '-')
               <> replace(lower(coalesce(NEW.assignment_group, '')), '_', '-')
            OR coalesce(OLD.workflow_name, '') <> coalesce(NEW.workflow_name, '')
        );
        old_assigned := (
            OLD.assignee_id IS NOT NULL OR coalesce(OLD.assignee_email, '') <> ''
        );
        IF NOT assignment_changed
           AND NOT (old_assigned AND routing_context_changed) THEN
            RETURN NEW;
        END IF;
    END IF;

    IF TG_OP = 'UPDATE' AND OLD.project_id IS NOT NULL THEN
        SELECT org_id INTO old_project_org FROM tasks_project WHERE id = OLD.project_id;
        IF coalesce(OLD.assignment_group, '') <> '' THEN
            SELECT id INTO old_team
              FROM tasks_routing_team
             WHERE organization_id = old_project_org
               AND key = replace(lower(OLD.assignment_group), '_', '-');
        END IF;
    END IF;
    IF NEW.project_id IS NOT NULL THEN
        SELECT org_id INTO new_project_org FROM tasks_project WHERE id = NEW.project_id;
        IF coalesce(NEW.assignment_group, '') <> '' THEN
            SELECT id INTO new_team
              FROM tasks_routing_team
             WHERE organization_id = new_project_org
               AND key = replace(lower(NEW.assignment_group), '_', '-');
        END IF;
    END IF;

    IF old_team IS NULL AND new_team IS NULL THEN
        RETURN NEW;
    END IF;

    clear_write := FALSE;
    IF TG_OP = 'UPDATE' THEN
        clear_write := (
            assignment_changed
            AND (OLD.assignee_id IS NOT NULL
                 OR coalesce(OLD.assignee_email, '') <> '')
            AND NEW.assignee_id IS NULL
            AND coalesce(NEW.assignee_email, '') = ''
        );
        IF clear_write THEN
            IF NOT routing_context_changed THEN
                clear_action := 'clear';
            ELSIF OLD.project_id IS NOT DISTINCT FROM NEW.project_id
                  AND coalesce(OLD.workflow_name, '') = coalesce(NEW.workflow_name, '')
                  AND coalesce(NEW.assignment_group, '') = '' THEN
                clear_action := 'unassign';
            ELSE
                clear_action := 'reroute_group_queue';
            END IF;
        END IF;
    END IF;

    BEGIN
        receipt_id := nullif(current_setting('pulse.routing_assignment_receipt', true), '')::uuid;
    EXCEPTION WHEN OTHERS THEN
        receipt_id := NULL;
    END;
    IF clear_write THEN
        SELECT id INTO matched_receipt
         FROM tasks_routing_assignment_receipt
         WHERE id = receipt_id
           AND action = clear_action
           AND item_id = OLD.id
           AND organization_id = old_project_org
           AND project_id = OLD.project_id
           AND routing_team_id IS NOT DISTINCT FROM old_team
           AND assignment_group = replace(lower(OLD.assignment_group), '_', '-')
           AND workflow_name = coalesce(OLD.workflow_name, '')
           AND subject_id IS NOT DISTINCT FROM OLD.assignee_id
           AND lower(assignee_email) = lower(coalesce(OLD.assignee_email, ''))
           AND result_organization_id = new_project_org
           AND result_project_id = NEW.project_id
           AND result_routing_team_id IS NOT DISTINCT FROM new_team
           AND result_assignment_group = replace(lower(coalesce(NEW.assignment_group, '')), '_', '-')
           AND result_workflow_name = coalesce(NEW.workflow_name, '')
           AND consumed_at IS NULL
           AND expires_at >= clock_timestamp()
         FOR UPDATE;
    ELSE
        -- PLS-539 (hiro/ADR-0010): an authorized direct handoff crosses OUT of a
        -- guarded OLD lane into a concrete NEW owner in a (possibly unguarded) NEW
        -- lane. Accept a one-use direct_handoff receipt binding the OLD guarded lane
        -- + the NEW owner + the NEW result lane. Only meaningful when LEAVING a
        -- guarded lane (old_team present); the ordinary assign path is unchanged.
        IF old_team IS NOT NULL THEN
            SELECT id INTO matched_receipt
              FROM tasks_routing_assignment_receipt
             WHERE id = receipt_id
               AND action = 'direct_handoff'
               AND item_id = NEW.id
               AND organization_id = old_project_org
               AND project_id = OLD.project_id
               AND routing_team_id IS NOT DISTINCT FROM old_team
               AND assignment_group = replace(lower(coalesce(OLD.assignment_group, '')), '_', '-')
               AND workflow_name = coalesce(OLD.workflow_name, '')
               AND subject_id IS NOT DISTINCT FROM NEW.assignee_id
               AND lower(assignee_email) = lower(coalesce(NEW.assignee_email, ''))
               AND result_organization_id = new_project_org
               AND result_project_id = NEW.project_id
               AND result_routing_team_id IS NOT DISTINCT FROM new_team
               AND result_assignment_group = replace(lower(coalesce(NEW.assignment_group, '')), '_', '-')
               AND result_workflow_name = coalesce(NEW.workflow_name, '')
               AND consumed_at IS NULL
               AND expires_at >= clock_timestamp()
             FOR UPDATE;
        END IF;
        IF matched_receipt IS NULL THEN
            IF new_team IS NULL
               OR NEW.assignee_id IS NULL
               OR coalesce(NEW.assignee_email, '') = '' THEN
                RAISE EXCEPTION 'routing_authority:assignment_receipt_required'
                  USING ERRCODE = '42501';
            END IF;
            SELECT id INTO matched_receipt
              FROM tasks_routing_assignment_receipt
             WHERE id = receipt_id
               AND action = 'assign'
               AND item_id = NEW.id
               AND organization_id = new_project_org
               AND project_id = NEW.project_id
               AND routing_team_id = new_team
               AND assignment_group = replace(lower(NEW.assignment_group), '_', '-')
               AND workflow_name = coalesce(NEW.workflow_name, '')
               AND subject_id = NEW.assignee_id
               AND lower(assignee_email) = lower(coalesce(NEW.assignee_email, ''))
               AND result_organization_id = new_project_org
               AND result_project_id = NEW.project_id
               AND result_routing_team_id = new_team
               AND result_assignment_group = replace(lower(NEW.assignment_group), '_', '-')
               AND result_workflow_name = coalesce(NEW.workflow_name, '')
               AND consumed_at IS NULL
               AND expires_at >= clock_timestamp()
             FOR UPDATE;
        END IF;
    END IF;
    IF matched_receipt IS NULL THEN
        RAISE EXCEPTION 'routing_authority:assignment_receipt_required'
          USING ERRCODE = '42501';
    END IF;
    UPDATE tasks_routing_assignment_receipt
       SET consumed_at = clock_timestamp()
     WHERE id = matched_receipt AND consumed_at IS NULL;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'routing_authority:assignment_receipt_reused'
          USING ERRCODE = '42501';
    END IF;
    RETURN NEW;
END;
$$;
"""

# Reverse: restore the 0099 body verbatim (no direct_handoff branch).
REVERSE_SQL = r"""
CREATE OR REPLACE FUNCTION tasks_guard_item_assignment()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    old_project_org bigint;
    new_project_org bigint;
    old_team uuid;
    new_team uuid;
    assignment_changed boolean;
    routing_context_changed boolean;
    old_assigned boolean;
    clear_write boolean;
    clear_action text;
    receipt_id uuid;
    matched_receipt uuid;
BEGIN
    IF TG_OP = 'INSERT'
       AND NEW.assignee_id IS NULL
       AND coalesce(NEW.assignee_email, '') = '' THEN
        RETURN NEW;
    END IF;

    IF TG_OP = 'UPDATE' THEN
        assignment_changed := (
            OLD.assignee_id IS DISTINCT FROM NEW.assignee_id
            OR lower(coalesce(OLD.assignee_email, ''))
               <> lower(coalesce(NEW.assignee_email, ''))
        );
        routing_context_changed := (
            OLD.project_id IS DISTINCT FROM NEW.project_id
            OR replace(lower(coalesce(OLD.assignment_group, '')), '_', '-')
               <> replace(lower(coalesce(NEW.assignment_group, '')), '_', '-')
            OR coalesce(OLD.workflow_name, '') <> coalesce(NEW.workflow_name, '')
        );
        old_assigned := (
            OLD.assignee_id IS NOT NULL OR coalesce(OLD.assignee_email, '') <> ''
        );
        IF NOT assignment_changed
           AND NOT (old_assigned AND routing_context_changed) THEN
            RETURN NEW;
        END IF;
    END IF;

    IF TG_OP = 'UPDATE' AND OLD.project_id IS NOT NULL THEN
        SELECT org_id INTO old_project_org FROM tasks_project WHERE id = OLD.project_id;
        IF coalesce(OLD.assignment_group, '') <> '' THEN
            SELECT id INTO old_team
              FROM tasks_routing_team
             WHERE organization_id = old_project_org
               AND key = replace(lower(OLD.assignment_group), '_', '-');
        END IF;
    END IF;
    IF NEW.project_id IS NOT NULL THEN
        SELECT org_id INTO new_project_org FROM tasks_project WHERE id = NEW.project_id;
        IF coalesce(NEW.assignment_group, '') <> '' THEN
            SELECT id INTO new_team
              FROM tasks_routing_team
             WHERE organization_id = new_project_org
               AND key = replace(lower(NEW.assignment_group), '_', '-');
        END IF;
    END IF;

    IF old_team IS NULL AND new_team IS NULL THEN
        RETURN NEW;
    END IF;

    clear_write := FALSE;
    IF TG_OP = 'UPDATE' THEN
        clear_write := (
            assignment_changed
            AND (OLD.assignee_id IS NOT NULL
                 OR coalesce(OLD.assignee_email, '') <> '')
            AND NEW.assignee_id IS NULL
            AND coalesce(NEW.assignee_email, '') = ''
        );
        IF clear_write THEN
            IF NOT routing_context_changed THEN
                clear_action := 'clear';
            ELSIF OLD.project_id IS NOT DISTINCT FROM NEW.project_id
                  AND coalesce(OLD.workflow_name, '') = coalesce(NEW.workflow_name, '')
                  AND coalesce(NEW.assignment_group, '') = '' THEN
                clear_action := 'unassign';
            ELSE
                clear_action := 'reroute_group_queue';
            END IF;
        END IF;
    END IF;

    BEGIN
        receipt_id := nullif(current_setting('pulse.routing_assignment_receipt', true), '')::uuid;
    EXCEPTION WHEN OTHERS THEN
        receipt_id := NULL;
    END;
    IF clear_write THEN
        SELECT id INTO matched_receipt
         FROM tasks_routing_assignment_receipt
         WHERE id = receipt_id
           AND action = clear_action
           AND item_id = OLD.id
           AND organization_id = old_project_org
           AND project_id = OLD.project_id
           AND routing_team_id IS NOT DISTINCT FROM old_team
           AND assignment_group = replace(lower(OLD.assignment_group), '_', '-')
           AND workflow_name = coalesce(OLD.workflow_name, '')
           AND subject_id IS NOT DISTINCT FROM OLD.assignee_id
           AND lower(assignee_email) = lower(coalesce(OLD.assignee_email, ''))
           AND result_organization_id = new_project_org
           AND result_project_id = NEW.project_id
           AND result_routing_team_id IS NOT DISTINCT FROM new_team
           AND result_assignment_group = replace(lower(coalesce(NEW.assignment_group, '')), '_', '-')
           AND result_workflow_name = coalesce(NEW.workflow_name, '')
           AND consumed_at IS NULL
           AND expires_at >= clock_timestamp()
         FOR UPDATE;
    ELSE
        IF new_team IS NULL
           OR NEW.assignee_id IS NULL
           OR coalesce(NEW.assignee_email, '') = '' THEN
            RAISE EXCEPTION 'routing_authority:assignment_receipt_required'
              USING ERRCODE = '42501';
        END IF;
        SELECT id INTO matched_receipt
          FROM tasks_routing_assignment_receipt
         WHERE id = receipt_id
           AND action = 'assign'
           AND item_id = NEW.id
           AND organization_id = new_project_org
           AND project_id = NEW.project_id
           AND routing_team_id = new_team
           AND assignment_group = replace(lower(NEW.assignment_group), '_', '-')
           AND workflow_name = coalesce(NEW.workflow_name, '')
           AND subject_id = NEW.assignee_id
           AND lower(assignee_email) = lower(coalesce(NEW.assignee_email, ''))
           AND result_organization_id = new_project_org
           AND result_project_id = NEW.project_id
           AND result_routing_team_id = new_team
           AND result_assignment_group = replace(lower(NEW.assignment_group), '_', '-')
           AND result_workflow_name = coalesce(NEW.workflow_name, '')
           AND consumed_at IS NULL
           AND expires_at >= clock_timestamp()
         FOR UPDATE;
    END IF;
    IF matched_receipt IS NULL THEN
        RAISE EXCEPTION 'routing_authority:assignment_receipt_required'
          USING ERRCODE = '42501';
    END IF;
    UPDATE tasks_routing_assignment_receipt
       SET consumed_at = clock_timestamp()
     WHERE id = matched_receipt AND consumed_at IS NULL;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'routing_authority:assignment_receipt_reused'
          USING ERRCODE = '42501';
    END IF;
    RETURN NEW;
END;
$$;
"""


class Migration(migrations.Migration):
    dependencies = [
        ('tasks', '0112_canonicalize_wip_capacity_identity'),
    ]

    operations = [
        migrations.AlterField(
            model_name='routingassignmentreceipt',
            name='action',
            field=models.CharField(
                choices=[
                    ('assign', 'Assign concrete owner'),
                    ('clear', 'Clear owner to group queue'),
                    ('unassign', 'Explicitly unassign and clear routing group'),
                    ('reroute_group_queue', 'Reroute to exact group queue'),
                    ('direct_handoff', 'Authorized direct handoff out of a guarded lane'),
                ],
                default='assign', max_length=32,
            ),
        ),
        migrations.RunSQL(sql=FORWARD_SQL, reverse_sql=REVERSE_SQL),
    ]
