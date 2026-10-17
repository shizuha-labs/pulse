"""Audited exact-row repair for the evidenced DOJO-15 auto-unblock comment."""
import hashlib
import json
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils.dateparse import parse_datetime

from tasks.models import Comment, Item, ItemActivity, ItemLink
from tasks.services.mechanical_comments import MECHANICAL_COMMENT_SPECS
from tasks.services.workflow_service import WorkflowService


class Command(BaseCommand):
    help = 'Repair one verified PLS-478 legacy auto-unblock row; dry-run unless --apply.'

    def add_arguments(self, parser):
        parser.add_argument('--item-key', required=True)
        parser.add_argument('--comment-id', required=True, type=int)
        parser.add_argument('--comment-created-at', required=True)
        parser.add_argument('--expected-author-id', required=True, type=int)
        parser.add_argument('--expected-author-email', required=True)
        parser.add_argument('--blocker-key', required=True)
        parser.add_argument('--activity-id', required=True)
        parser.add_argument('--expected-old-status', required=True)
        parser.add_argument('--expected-restored-status', required=True)
        parser.add_argument('--expected-assignee-email', default='')
        parser.add_argument('--merge-sod-reason', default='')
        parser.add_argument('--expected-content-sha256', required=True)
        parser.add_argument('--apply', action='store_true')

    def handle(self, *args, **opts):
        expected_at = parse_datetime(opts['comment_created_at'])
        if expected_at is None:
            raise CommandError('--comment-created-at must be an ISO-8601 timestamp')

        with transaction.atomic():
            try:
                item = Item.objects.select_for_update().get(item_key=opts['item_key'])
                blocker = Item.objects.get(item_key=opts['blocker_key'])
                comment = Comment.objects.select_for_update().get(
                    pk=opts['comment_id'], item=item,
                )
                activity = ItemActivity.objects.get(pk=opts['activity_id'], item=item)
            except (Item.DoesNotExist, Comment.DoesNotExist, ItemActivity.DoesNotExist) as exc:
                raise CommandError(f'Exact repair target not found: {exc}')

            failures = []
            if comment.created_at != expected_at:
                failures.append(f'created_at={comment.created_at.isoformat()}')
            if comment.author_id != opts['expected_author_id']:
                failures.append(f'author_id={comment.author_id}')
            if comment.author_email.lower() != opts['expected_author_email'].lower():
                failures.append(f'author_email={comment.author_email}')
            if comment.origin_kind != Comment.ORIGIN_LEGACY_UNTYPED:
                failures.append(f'origin_kind={comment.origin_kind}')
            if any((comment.origin_event, comment.template_version, comment.origin_event_id)):
                failures.append('row already has typed provenance')
            digest = hashlib.sha256(comment.content.encode()).hexdigest()
            if digest != opts['expected_content_sha256'].lower():
                failures.append(f'content_sha256={digest}')
            if activity.action != 'status_changed':
                failures.append(f'activity.action={activity.action}')
            if activity.old_value != opts['expected_old_status']:
                failures.append(f'activity.old_value={activity.old_value}')
            if activity.new_value != opts['expected_restored_status']:
                failures.append(f'activity.new_value={activity.new_value}')
            if activity.user_id != opts['expected_author_id']:
                failures.append(f'activity.user_id={activity.user_id}')
            if activity.user_email.lower() != opts['expected_author_email'].lower():
                failures.append(f'activity.user_email={activity.user_email}')
            if activity.created_at > comment.created_at:
                failures.append('activity occurs after comment')
            if (comment.created_at - activity.created_at).total_seconds() > 30:
                failures.append('activity/comment separation exceeds 30 seconds')
            if item.status != opts['expected_restored_status']:
                failures.append(f'item.status={item.status}')
            last_unblock = (item.metadata or {}).get('last_auto_unblock') or {}
            if last_unblock.get('blocker') != blocker.item_key:
                failures.append(
                    f'last_auto_unblock.blocker={last_unblock.get("blocker")!r}'
                )
            if last_unblock.get('old_status') != opts['expected_old_status']:
                failures.append(
                    f'last_auto_unblock.old_status={last_unblock.get("old_status")!r}'
                )
            if last_unblock.get('restored_status') != opts['expected_restored_status']:
                failures.append(
                    'last_auto_unblock.restored_status='
                    f'{last_unblock.get("restored_status")!r}'
                )
            candidates = Comment.objects.filter(
                item=item,
                author_id=opts['expected_author_id'],
                author_email__iexact=opts['expected_author_email'],
                content=comment.content,
                origin_kind=Comment.ORIGIN_LEGACY_UNTYPED,
                created_at__gte=activity.created_at,
                created_at__lte=activity.created_at + timedelta(seconds=30),
            ).count()
            if candidates != 1:
                failures.append(f'event/comment mapping is ambiguous ({candidates} candidates)')
            linked = ItemLink.objects.filter(
                source_item=blocker, target_item=item, link_type='blocks',
            ).exists() or ItemLink.objects.filter(
                source_item=item, target_item=blocker, link_type='is_blocked_by',
            ).exists()
            if not linked:
                failures.append('exact blocker link is absent')
            if not WorkflowService._status_releases_blocks(blocker.status):
                failures.append(f'blocker status {blocker.status!r} does not release blocks')

            payload = {
                'blocker_key': str(blocker.item_key),
                'old_status': opts['expected_old_status'],
                'restored_status': opts['expected_restored_status'],
                'assignee_email': opts['expected_assignee_email'],
                **(
                    {'merge_sod_reason': opts['merge_sod_reason']}
                    if opts['merge_sod_reason'] else {}
                ),
            }
            spec = MECHANICAL_COMMENT_SPECS[
                (Comment.ORIGIN_WORKFLOW_LIFECYCLE, 'link.auto_unblock', 1)
            ]
            if spec.render(payload) != comment.content:
                failures.append('structured payload does not render the exact preserved body')

            event_id = f'item-activity:{activity.pk}:link.auto_unblock'
            if Comment.objects.exclude(pk=comment.pk).filter(origin_event_id=event_id).exists():
                failures.append('origin_event_id is already bound to another comment')
            if failures:
                raise CommandError('Repair precondition mismatch; aborting: ' + '; '.join(failures))

            before = {
                'id': comment.pk,
                'item_key': item.item_key,
                'created_at': comment.created_at.isoformat(),
                'author_id': comment.author_id,
                'author_email': comment.author_email,
                'content_sha256': digest,
                'origin_kind': comment.origin_kind,
                'origin_event': comment.origin_event,
                'template_version': comment.template_version,
                'origin_event_id': comment.origin_event_id,
                'last_auto_unblock': last_unblock,
            }
            if not opts['apply']:
                self.stdout.write(json.dumps({'dry_run': True, 'before': before}, sort_keys=True))
                transaction.set_rollback(True)
                return

            updated = Comment.objects.filter(
                pk=comment.pk,
                item=item,
                created_at=expected_at,
                author_id=opts['expected_author_id'],
                author_email__iexact=opts['expected_author_email'],
                origin_kind=Comment.ORIGIN_LEGACY_UNTYPED,
                origin_event='',
                template_version__isnull=True,
                origin_event_id__isnull=True,
            ).update(
                author_id=0,
                author_email='workflow@pulse',
                author_name='Pulse Workflow',
                origin_kind=Comment.ORIGIN_WORKFLOW_LIFECYCLE,
                origin_event='link.auto_unblock',
                template_version=1,
                origin_event_id=event_id,
                origin_payload=payload,
                caused_by_user_id=activity.user_id,
                caused_by_email_snapshot=activity.user_email,
            )
            if updated != 1:
                raise CommandError(f'Expected one exact row update; got {updated}')
            comment.refresh_from_db()
            after = {
                **before,
                'author_id': comment.author_id,
                'author_email': comment.author_email,
                'origin_kind': comment.origin_kind,
                'origin_event': comment.origin_event,
                'template_version': comment.template_version,
                'origin_event_id': comment.origin_event_id,
                'caused_by_user_id': comment.caused_by_user_id,
                'caused_by_email_snapshot': comment.caused_by_email_snapshot,
                'origin_payload': comment.origin_payload,
            }
            self.stdout.write(json.dumps({'dry_run': False, 'before': before, 'after': after}, sort_keys=True))
