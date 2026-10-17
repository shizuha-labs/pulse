# Seed default statuses, resolutions, and workflows
from django.db import migrations
import uuid


def seed_defaults(apps, schema_editor):
    Status = apps.get_model('tasks', 'Status')
    Resolution = apps.get_model('tasks', 'Resolution')
    Workflow = apps.get_model('tasks', 'Workflow')
    WorkflowStatus = apps.get_model('tasks', 'WorkflowStatus')
    WorkflowTransition = apps.get_model('tasks', 'WorkflowTransition')
    WorkflowScheme = apps.get_model('tasks', 'WorkflowScheme')

    # --- Seed Global Statuses ---
    status_map = {}  # slug → Status instance
    default_statuses = [
        # To Do category
        {'name': 'Open', 'slug': 'open', 'category': 'todo', 'color': '#6b7280', 'position': 0, 'is_system': True,
         'description': 'Item is created but work has not started'},
        {'name': 'To Do', 'slug': 'todo', 'category': 'todo', 'color': '#6b7280', 'position': 1, 'is_system': True,
         'description': 'Item is ready to be worked on'},
        {'name': 'Reopened', 'slug': 'reopened', 'category': 'todo', 'color': '#f59e0b', 'position': 2, 'is_system': True,
         'description': 'Item was closed but has been reopened'},
        {'name': 'Backlog', 'slug': 'backlog', 'category': 'todo', 'color': '#9ca3af', 'position': 3, 'is_system': False,
         'description': 'Item is in the backlog, not yet prioritized'},
        # In Progress category
        {'name': 'In Progress', 'slug': 'in_progress', 'category': 'in_progress', 'color': '#3b82f6', 'position': 10, 'is_system': True,
         'description': 'Work is actively being done'},
        {'name': 'In Review', 'slug': 'in_review', 'category': 'in_progress', 'color': '#8b5cf6', 'position': 11, 'is_system': False,
         'description': 'Work is complete and awaiting review'},
        {'name': 'In QA', 'slug': 'in_qa', 'category': 'in_progress', 'color': '#ec4899', 'position': 12, 'is_system': False,
         'description': 'Item is being tested by QA'},
        {'name': 'Blocked', 'slug': 'blocked', 'category': 'in_progress', 'color': '#ef4444', 'position': 13, 'is_system': False,
         'description': 'Work is blocked by an external dependency'},
        {'name': 'Awaiting Triage', 'slug': 'awaiting_triage', 'category': 'in_progress', 'color': '#f97316', 'position': 14, 'is_system': False,
         'description': 'Bug/issue awaiting triage by QA or lead'},
        {'name': 'Design', 'slug': 'design', 'category': 'in_progress', 'color': '#14b8a6', 'position': 15, 'is_system': False,
         'description': 'Item is in the design/architecture phase'},
        # Done category
        {'name': 'Done', 'slug': 'done', 'category': 'done', 'color': '#22c55e', 'position': 20, 'is_system': True,
         'description': 'Work is complete'},
        {'name': 'Closed', 'slug': 'closed', 'category': 'done', 'color': '#16a34a', 'position': 21, 'is_system': True,
         'description': 'Item is closed (with resolution)'},
        {'name': 'Cancelled', 'slug': 'cancelled', 'category': 'done', 'color': '#9ca3af', 'position': 22, 'is_system': False,
         'description': 'Item was cancelled without completion'},
        # Legacy compatibility
        {'name': 'Pending', 'slug': 'pending', 'category': 'todo', 'color': '#6b7280', 'position': 4, 'is_system': True,
         'description': 'Legacy: equivalent to Open/To Do'},
        {'name': 'Completed', 'slug': 'completed', 'category': 'done', 'color': '#22c55e', 'position': 23, 'is_system': True,
         'description': 'Legacy: equivalent to Done'},
    ]

    for s in default_statuses:
        obj, _ = Status.objects.get_or_create(
            slug=s['slug'],
            defaults={k: v for k, v in s.items() if k != 'slug'}
        )
        status_map[s['slug']] = obj

    # --- Seed Resolutions ---
    default_resolutions = [
        {'name': 'Fixed', 'slug': 'fixed', 'position': 0, 'is_system': True,
         'description': 'A fix for this issue has been implemented'},
        {'name': "Won't Fix", 'slug': 'wont_fix', 'position': 1, 'is_system': True,
         'description': 'This issue will not be fixed'},
        {'name': 'Duplicate', 'slug': 'duplicate', 'position': 2, 'is_system': True,
         'description': 'This issue is a duplicate of an existing issue'},
        {'name': 'Cannot Reproduce', 'slug': 'cannot_reproduce', 'position': 3, 'is_system': True,
         'description': 'The issue cannot be reproduced'},
        {'name': 'Incomplete', 'slug': 'incomplete', 'position': 4, 'is_system': True,
         'description': 'The issue is incomplete'},
        {'name': 'Done', 'slug': 'done', 'position': 5, 'is_system': True,
         'description': 'Work on this issue is complete'},
        {'name': "Won't Do", 'slug': 'wont_do', 'position': 6, 'is_system': False,
         'description': 'This issue will not be actioned'},
        {'name': 'Known Error', 'slug': 'known_error', 'position': 7, 'is_system': False,
         'description': 'This is a known error being tracked'},
    ]

    for r in default_resolutions:
        Resolution.objects.get_or_create(
            slug=r['slug'],
            defaults={k: v for k, v in r.items() if k != 'slug'}
        )

    # --- Create Default Workflows ---

    # 1. Simple Workflow (To Do → In Progress → Done)
    simple_wf, _ = Workflow.objects.get_or_create(
        slug='simple',
        defaults={
            'name': 'Simple Workflow',
            'description': 'Basic three-step workflow for simple tasks',
            'is_system': True,
            'is_active': True,
            'initial_status': status_map['todo'],
        }
    )
    simple_statuses = ['todo', 'in_progress', 'done']
    for i, slug in enumerate(simple_statuses):
        WorkflowStatus.objects.get_or_create(
            workflow=simple_wf,
            status=status_map[slug],
            defaults={'position': i}
        )

    # Simple workflow transitions
    simple_transitions = [
        {'name': 'Start Progress', 'from_status': 'todo', 'to_status': 'in_progress', 'position': 0},
        {'name': 'Done', 'from_status': 'in_progress', 'to_status': 'done', 'position': 1},
        {'name': 'Reopen', 'from_status': 'done', 'to_status': 'todo', 'position': 2,
         'post_functions': [{'type': 'clear_resolution'}]},
    ]
    for t in simple_transitions:
        WorkflowTransition.objects.get_or_create(
            jira_workflow=simple_wf,
            from_status=t['from_status'],
            to_status=t['to_status'],
            defaults={
                'name': t['name'],
                'position': t.get('position', 0),
                'post_functions': t.get('post_functions', []),
            }
        )

    # 2. Software Development Workflow (Open → In Progress → In Review → Done / Reopened)
    dev_wf, _ = Workflow.objects.get_or_create(
        slug='software-development',
        defaults={
            'name': 'Software Development Workflow',
            'description': 'Standard workflow for stories, tasks, and features',
            'is_system': True,
            'is_active': True,
            'initial_status': status_map['open'],
        }
    )
    dev_statuses = ['open', 'in_progress', 'in_review', 'done', 'reopened']
    for i, slug in enumerate(dev_statuses):
        WorkflowStatus.objects.get_or_create(
            workflow=dev_wf,
            status=status_map[slug],
            defaults={'position': i}
        )

    dev_transitions = [
        {'name': 'Start Progress', 'from_status': 'open', 'to_status': 'in_progress', 'position': 0},
        {'name': 'Start Progress', 'from_status': 'reopened', 'to_status': 'in_progress', 'position': 1},
        {'name': 'Submit for Review', 'from_status': 'in_progress', 'to_status': 'in_review', 'position': 2},
        {'name': 'Approve', 'from_status': 'in_review', 'to_status': 'done', 'position': 3,
         'post_functions': [{'type': 'set_resolution', 'resolution': 'done'}]},
        {'name': 'Request Changes', 'from_status': 'in_review', 'to_status': 'in_progress', 'position': 4},
        {'name': 'Reopen', 'from_status': 'done', 'to_status': 'reopened', 'position': 5,
         'post_functions': [{'type': 'clear_resolution'}]},
        {'name': 'Stop Progress', 'from_status': 'in_progress', 'to_status': 'open', 'position': 6},
    ]
    for t in dev_transitions:
        WorkflowTransition.objects.get_or_create(
            jira_workflow=dev_wf,
            from_status=t['from_status'],
            to_status=t['to_status'],
            defaults={
                'name': t['name'],
                'position': t.get('position', 0),
                'post_functions': t.get('post_functions', []),
            }
        )

    # 3. Bug Workflow (Open → Awaiting Triage → Design → In Progress → In QA → Closed / Reopened)
    bug_wf, _ = Workflow.objects.get_or_create(
        slug='bug-workflow',
        defaults={
            'name': 'Bug Workflow',
            'description': 'Full bug lifecycle: triage → design → implement → QA verify → close',
            'is_system': True,
            'is_active': True,
            'initial_status': status_map['open'],
        }
    )
    bug_statuses = ['open', 'awaiting_triage', 'design', 'in_progress', 'in_review', 'in_qa', 'closed', 'reopened', 'cancelled']
    for i, slug in enumerate(bug_statuses):
        WorkflowStatus.objects.get_or_create(
            workflow=bug_wf,
            status=status_map[slug],
            defaults={'position': i}
        )

    bug_transitions = [
        # Open → Triage
        {'name': 'Send to Triage', 'from_status': 'open', 'to_status': 'awaiting_triage', 'position': 0,
         'post_functions': [{'type': 'add_comment', 'body': 'Bug sent to QA for triage'}]},
        # Triage → Confirmed (Design) or Reject (Closed)
        {'name': 'Confirm Bug', 'from_status': 'awaiting_triage', 'to_status': 'design', 'position': 1,
         'post_functions': [{'type': 'add_comment', 'body': 'Bug confirmed. Sending to architect for design.'}]},
        {'name': 'Not a Bug', 'from_status': 'awaiting_triage', 'to_status': 'closed', 'position': 2,
         'post_functions': [{'type': 'set_resolution', 'resolution': 'cannot_reproduce'}],
         'screen_fields': [{'field': 'resolution', 'required': True, 'label': 'Resolution'},
                           {'field': 'comment', 'required': True, 'label': 'Reason'}]},
        # Design → Ready for Development
        {'name': 'Design Complete', 'from_status': 'design', 'to_status': 'in_progress', 'position': 3,
         'post_functions': [{'type': 'add_comment', 'body': 'Design complete. Ready for implementation.'}],
         'required_fields': ['assignee_id']},
        # Implementation → Code Review
        {'name': 'Submit for Review', 'from_status': 'in_progress', 'to_status': 'in_review', 'position': 4},
        # Code Review → QA or back to dev
        {'name': 'Code Approved', 'from_status': 'in_review', 'to_status': 'in_qa', 'position': 5,
         'post_functions': [{'type': 'add_comment', 'body': 'Code review passed. Sending to QA.'}]},
        {'name': 'Request Changes', 'from_status': 'in_review', 'to_status': 'in_progress', 'position': 6},
        # QA → Close or Reopen
        {'name': 'QA Passed', 'from_status': 'in_qa', 'to_status': 'closed', 'position': 7,
         'post_functions': [{'type': 'set_resolution', 'resolution': 'fixed'}],
         'screen_fields': [{'field': 'comment', 'required': False, 'label': 'QA Notes'}]},
        {'name': 'QA Failed', 'from_status': 'in_qa', 'to_status': 'in_progress', 'position': 8,
         'post_functions': [{'type': 'add_comment', 'body': 'QA verification failed. Returning to developer.'}]},
        # Reopen
        {'name': 'Reopen', 'from_status': 'closed', 'to_status': 'reopened', 'position': 9,
         'post_functions': [{'type': 'clear_resolution'}]},
        {'name': 'Resume Work', 'from_status': 'reopened', 'to_status': 'in_progress', 'position': 10},
        # Cancel from anywhere (global-like transitions)
        {'name': 'Cancel', 'from_status': 'open', 'to_status': 'cancelled', 'position': 20,
         'post_functions': [{'type': 'set_resolution', 'resolution': 'wont_fix'}]},
        {'name': 'Cancel', 'from_status': 'awaiting_triage', 'to_status': 'cancelled', 'position': 21,
         'post_functions': [{'type': 'set_resolution', 'resolution': 'wont_fix'}]},
    ]
    for t in bug_transitions:
        WorkflowTransition.objects.get_or_create(
            jira_workflow=bug_wf,
            from_status=t['from_status'],
            to_status=t['to_status'],
            defaults={
                'name': t['name'],
                'position': t.get('position', 0),
                'post_functions': t.get('post_functions', []),
                'required_fields': t.get('required_fields', []),
                'screen_fields': t.get('screen_fields', []),
            }
        )

    # 4. Default Workflow Scheme (maps task types to workflows)
    WorkflowScheme.objects.get_or_create(
        slug='default-software',
        defaults={
            'name': 'Default Software Development Scheme',
            'description': 'Maps bug types to Bug Workflow, everything else to Software Development Workflow',
            'is_system': True,
            'is_active': True,
            'default_workflow': dev_wf,
        }
    )


def reverse_seed(apps, schema_editor):
    # Just remove seeded data — don't drop tables
    Status = apps.get_model('tasks', 'Status')
    Resolution = apps.get_model('tasks', 'Resolution')
    Workflow = apps.get_model('tasks', 'Workflow')
    WorkflowScheme = apps.get_model('tasks', 'WorkflowScheme')

    WorkflowScheme.objects.filter(is_system=True).delete()
    Workflow.objects.filter(is_system=True).delete()
    Resolution.objects.filter(is_system=True).delete()
    Status.objects.filter(is_system=True).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0022_add_jira_workflow_models'),
    ]

    operations = [
        migrations.RunPython(seed_defaults, reverse_seed),
    ]
