"""
User notification service for Discord, Telegram, and push notification channels.
Sends notifications directly to user-configured channels and mobile push.
"""
import re
import requests
import logging
from typing import Optional, Dict, Any, List
from django.conf import settings
from django.utils import timezone
from datetime import datetime

from .connect_origin import system_message_headers

logger = logging.getLogger(__name__)

_PRIORITY_RANK = {
    'urgent': 0,
    'high': 1,
    'normal': 2,
    'medium': 2,
    'low': 3,
}
_SEVERITY_PRIORITY = {
    'critical': 'urgent',
    'error': 'high',
    'warning': 'normal',
    'info': 'low',
}


def _connect_alert_has_higher_priority_task(user_id: int, alert) -> bool:
    """Whether a Pulse task must retain the recipient's immediate attention.

    Connect system messages wake an agent immediately.  Sending a lower-priority
    alert DM to an agent holding urgent WIP therefore bypasses the canonical
    Pulse queue even though the alert remains safely visible in the alert inbox.
    Suppress only that wake/DM; normal notification records and push delivery
    remain intact, and equal-priority alerts still win the tie.
    """
    from tasks.models import Item, Status
    from tasks.services.actionable_items import filter_active_queue_items
    from tasks.services.queue_priority import (
        active_upstream_blocker_exists,
        effective_blocker_priority_rank,
    )

    priority = (getattr(alert, 'priority', '') or '').strip().lower()
    if priority not in _PRIORITY_RANK:
        severity = (getattr(alert, 'severity', '') or 'warning').strip().lower()
        priority = _SEVERITY_PRIORITY.get(severity, 'normal')
    alert_rank = _PRIORITY_RANK[priority]
    if alert_rank == 0:
        return False

    waiting_slugs = set(
        Status.objects.filter(category__in=('blocked', 'scheduled', 'backlog'))
        .values_list('slug', flat=True)
    )
    waiting_slugs.update({'blocked', 'scheduled', 'backlog'})
    tasks = filter_active_queue_items(
        Item.objects.filter(
            mode='task',
            assignee_id=user_id,
            is_archived=False,
            resolution__isnull=True,
        )
    ).annotate(
        _queue_effective_priority_rank=effective_blocker_priority_rank(),
        _queue_has_active_upstream=active_upstream_blocker_exists(),
    ).exclude(
        status__in=waiting_slugs,
    ).exclude(
        _queue_has_active_upstream=True,
    )
    return tasks.filter(
        _queue_effective_priority_rank__lt=alert_rank,
    ).exists()


# PLS-715: a busy recipient must not suppress an alert DM indefinitely. Each
# deferral increments a bounded per-(recipient, alert) counter; once it passes
# the ceiling the alert is delivered anyway (or the counter is reset by an
# actual delivery). The dedupe window (~1h) spaces attempts, so the ceiling
# bounds suppression to a few hours, not forever.
ALERT_CONNECT_DEFER_MAX_ATTEMPTS = 3
ALERT_CONNECT_DEFER_COUNTER_TTL_S = 24 * 3600


def _alert_defer_fingerprint(item) -> str:
    return str(
        getattr(item, 'item_key', None)
        or getattr(item, 'id', None)
        or getattr(item, 'source_id', None)
        or getattr(item, 'title', None)
        or 'alert'
    )


def _alert_defer_key(user_id: int, item) -> str:
    return f'pulse:alert:defer:{int(user_id)}:{_alert_defer_fingerprint(item)}'


def _alert_defer_exhausted(user_id: int, item) -> bool:
    """Whether this (recipient, alert) has been deferred past the ceiling.

    PLS-715: once the ceiling is reached the alert must be delivered anyway —
    no alert is suppressible indefinitely by one busy seat. Fail open on a
    cache error (deliver rather than silently drop).
    """
    from django.core.cache import cache
    try:
        attempts = int(cache.get(_alert_defer_key(user_id, item), 0) or 0)
    except (TypeError, ValueError):
        attempts = 0
    except Exception:  # noqa: BLE001
        logger.exception('alert defer ceiling read failed; delivering anyway')
        return True
    return attempts >= ALERT_CONNECT_DEFER_MAX_ATTEMPTS


def _record_alert_defer(user_id: int, item) -> None:
    from django.core.cache import cache
    key = _alert_defer_key(user_id, item)
    try:
        attempts = int(cache.get(key, 0) or 0)
        cache.set(key, attempts + 1, ALERT_CONNECT_DEFER_COUNTER_TTL_S)
    except Exception:  # noqa: BLE001
        logger.exception('alert defer counter update failed')


def _clear_alert_defer(user_id: int, item) -> None:
    """Reset the deferral budget after an actual delivery (fresh episode)."""
    from django.core.cache import cache
    try:
        cache.delete(_alert_defer_key(user_id, item))
    except Exception:  # noqa: BLE001
        logger.exception('alert defer counter clear failed')


class DiscordNotifier:
    """Send notifications via Discord webhooks"""

    @staticmethod
    def send(webhook_url: str, message: str, embed: Dict = None) -> tuple[bool, str]:
        """
        Send a message to a Discord webhook.
        Returns (success, error_message)
        """
        try:
            payload = {}

            if embed:
                payload['embeds'] = [embed]
            else:
                payload['content'] = message

            response = requests.post(
                webhook_url,
                json=payload,
                timeout=10,
                headers={'Content-Type': 'application/json'}
            )

            if response.status_code in [200, 204]:
                return True, ''
            else:
                error = f"Discord API error: {response.status_code} - {response.text}"
                logger.error(error)
                return False, error

        except requests.Timeout:
            error = "Discord webhook timeout"
            logger.error(error)
            return False, error
        except requests.RequestException as e:
            error = f"Discord request error: {str(e)}"
            logger.error(error)
            return False, error

    @staticmethod
    def create_embed(
        title: str,
        description: str = None,
        color: int = None,
        fields: List[Dict] = None,
        footer: str = None,
        timestamp: datetime = None
    ) -> Dict:
        """Create a Discord embed object"""
        embed = {'title': title}

        if description:
            embed['description'] = description

        if color is not None:
            embed['color'] = color

        if fields:
            embed['fields'] = fields

        if footer:
            embed['footer'] = {'text': footer}

        if timestamp:
            embed['timestamp'] = timestamp.isoformat()

        return embed


class TelegramNotifier:
    """Send notifications via Telegram bot API"""

    @staticmethod
    def send(bot_token: str, chat_id: str, message: str, parse_mode: str = 'HTML') -> tuple[bool, str]:
        """
        Send a message to a Telegram chat.
        Returns (success, error_message)
        """
        try:
            url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

            payload = {
                'chat_id': chat_id,
                'text': message,
                'parse_mode': parse_mode,
                'disable_web_page_preview': True
            }

            response = requests.post(url, json=payload, timeout=10)
            data = response.json()

            if data.get('ok'):
                return True, ''
            else:
                error = f"Telegram API error: {data.get('description', 'Unknown error')}"
                logger.error(error)
                return False, error

        except requests.Timeout:
            error = "Telegram API timeout"
            logger.error(error)
            return False, error
        except requests.RequestException as e:
            error = f"Telegram request error: {str(e)}"
            logger.error(error)
            return False, error


class UserNotificationService:
    """
    Service to send notifications to users via their configured channels.
    """

    # Discord embed colors
    COLORS = {
        'info': 0x3498DB,      # Blue
        'warning': 0xF39C12,   # Orange
        'error': 0xE74C3C,     # Red
        'critical': 0x9B59B6,  # Purple
        'success': 0x2ECC71,   # Green
        'reminder': 0x1ABC9C,  # Teal
        'overdue': 0xE74C3C,   # Red
    }

    # Priority emoji mapping
    PRIORITY_EMOJI = {
        'low': '🔵',
        'normal': '🟢',
        'high': '🟠',
        'urgent': '🔴',
    }

    def __init__(self):
        self.discord = DiscordNotifier()
        self.telegram = TelegramNotifier()

    def get_user_channels(self, user_id: int, event_type: str = None) -> List:
        """
        Get all active notification channels for a user.
        Optionally filter by event type (reminder, overdue, alert, action).
        """
        from tasks.models import UserNotificationChannel

        channels = UserNotificationChannel.objects.filter(
            user_id=user_id,
            is_active=True
        )

        # Filter by event type if specified
        if event_type == 'reminder':
            channels = channels.filter(notify_on_reminder=True)
        elif event_type == 'overdue':
            channels = channels.filter(notify_on_overdue=True)
        elif event_type == 'alert':
            channels = channels.filter(notify_on_alert=True)
        elif event_type == 'action':
            channels = channels.filter(notify_on_action=True)

        # Filter out channels in quiet hours
        return [ch for ch in channels if not ch.is_within_quiet_hours()]

    def notify_user(
        self,
        user_id: int,
        title: str,
        message: str,
        event_type: str = None,
        severity: str = 'info',
        item: Any = None,
        fields: List[Dict] = None,
        exclude_delegate_user_ids: Optional[set] = None
    ) -> Dict[str, Any]:
        """
        Send notification to all of a user's active channels.

        Args:
            user_id: User ID to notify
            title: Notification title
            message: Notification body
            event_type: One of 'reminder', 'overdue', 'alert', 'action'
            severity: Severity level for color coding
            item: Optional Item model instance for additional context
            fields: Additional fields for Discord embeds
            exclude_delegate_user_ids: Extra user IDs that should not receive
                delegated copies (e.g. other direct recipients in a fan-out).

        Returns:
            Dict with results: {'success': int, 'failed': int, 'channels': [...]}
        """
        # Persist an in-app notification record
        self._create_notification_record(user_id, title, message, event_type, item)

        # Always attempt push notification (fires for any user with registered devices)
        self._send_push(user_id, title, message, event_type, severity, item)

        channels = self.get_user_channels(user_id, event_type)

        results = {
            'success': 0,
            'failed': 0,
            'channels': []
        }

        # Delegate notifications BEFORE checking channels (delegation is independent of channel config)
        try:
            self._create_delegated_notifications(
                original_user_id=user_id,
                event_type=event_type or '',
                title=title,
                message=message,
                severity=severity,
                item=item,
                fields=fields,
                exclude_user_ids={user_id, *(exclude_delegate_user_ids or set())},
            )
        except Exception as e:
            logger.error(f'Delegation notification error for user {user_id}: {e}')

        if not channels:
            logger.info(f"No active notification channels for user {user_id} (event_type={event_type})")
            return results

        for channel in channels:
            # Check severity filter for alerts
            if event_type == 'alert' and severity:
                if not channel.should_notify_for_severity(severity):
                    logger.debug(f"Skipping channel {channel.id}: severity {severity} below threshold")
                    continue

            success = False
            error = ''

            if channel.channel_type == 'discord':
                success, error = self._send_discord(
                    channel, title, message, severity, fields
                )
            elif channel.channel_type == 'telegram':
                success, error = self._send_telegram(
                    channel, title, message, event_type, severity
                )

            # Update channel stats
            channel.last_notification_at = timezone.now()
            if success:
                channel.error_count = 0
                channel.last_error = ''
                results['success'] += 1
            else:
                channel.error_count += 1
                channel.last_error = error
                results['failed'] += 1
            channel.save(update_fields=['last_notification_at', 'error_count', 'last_error'])

            results['channels'].append({
                'channel_id': channel.id,
                'channel_type': channel.channel_type,
                'name': channel.name,
                'success': success,
                'error': error
            })

        return results

    def _send_discord(
        self,
        channel,
        title: str,
        message: str,
        severity: str,
        fields: List[Dict] = None
    ) -> tuple[bool, str]:
        """Send notification via Discord webhook"""
        if not channel.discord_webhook_url:
            return False, "No Discord webhook URL configured"

        color = self.COLORS.get(severity, self.COLORS['info'])

        embed = self.discord.create_embed(
            title=title,
            description=message,
            color=color,
            fields=fields,
            footer='Shizuha Hub',
            timestamp=timezone.now()
        )

        return self.discord.send(channel.discord_webhook_url, message, embed)

    def _send_telegram(
        self,
        channel,
        title: str,
        message: str,
        event_type: str,
        severity: str
    ) -> tuple[bool, str]:
        """Send notification via Telegram"""
        if not channel.telegram_bot_token or not channel.telegram_chat_id:
            return False, "Telegram bot token or chat ID not configured"

        # Format message for Telegram with HTML
        emoji = self._get_event_emoji(event_type, severity)
        formatted = f"<b>{emoji} {title}</b>\n\n{message}"

        return self.telegram.send(
            channel.telegram_bot_token,
            channel.telegram_chat_id,
            formatted
        )

    def _get_event_emoji(self, event_type: str, severity: str) -> str:
        """Get appropriate emoji for event type"""
        if event_type == 'reminder':
            return '⏰'
        elif event_type == 'overdue':
            return '🚨'
        elif event_type == 'alert':
            severity_emoji = {
                'info': 'ℹ️',
                'warning': '⚠️',
                'error': '❌',
                'critical': '🔥'
            }
            return severity_emoji.get(severity, '🔔')
        elif event_type == 'action':
            return '⚡'
        return '📋'

    def _create_notification_record(self, user_id, title, message, event_type, item):
        """Persist an in-app notification so users can review past notifications."""
        try:
            from tasks.models import Notification, Item
            # Strip HTML tags for a clean plain-text message
            clean_msg = re.sub(r'<[^>]+>', '', message)
            # DM-only alert packets are not Item rows — skip FK.
            item_fk = item if isinstance(item, Item) and getattr(item, 'pk', None) else None
            Notification.objects.create(
                user_id=user_id,
                item=item_fk,
                notification_type=event_type or 'system',
                title=title,
                message=clean_msg,
            )
        except Exception as e:
            logger.error(f"Failed to create notification record for user {user_id}: {e}")

    def _send_push(self, user_id, title, body, event_type, severity, item=None):
        """Send push notification via shizuha-id internal push endpoint."""
        # Strip HTML tags for plain-text push body
        clean_body = re.sub(r'<[^>]+>', '', body)

        # Map severity to Android channel + priority
        if severity in ('critical', 'error') or event_type == 'alert':
            channel_id = 'pulse_urgent'
            priority = 'high'
        else:
            channel_id = 'pulse_alerts'
            priority = 'high'  # Always high for visibility

        data = {
            'type': f'pulse_{event_type}' if event_type else 'pulse_notification',
            'screen': 'PulseTab',
        }
        if item is not None and getattr(item, 'id', None):
            data['item_id'] = item.id
            if getattr(item, 'item_key', None):
                data['item_key'] = item.item_key

        payload = {
            'user_id': user_id,
            'title': title,
            'body': clean_body[:200],
            'data': data,
            'priority': priority,
            'channel_id': channel_id,
        }

        try:
            push_base_url = getattr(settings, 'SHIZUHA_ID_API_URL', 'http://shizuha-id:8001/api').rstrip('/')
            response = requests.post(
                f"{push_base_url}/internal/push/send/",
                json=payload,
                headers={
                    'X-Internal-Service': 'shizuha-pulse',
                    'Content-Type': 'application/json',
                },
                timeout=10,
            )
            if response.ok:
                result = response.json()
                logger.info(f"Push sent to user {user_id}: {result.get('sent', 0)} devices")
                return True
            else:
                logger.warning(f"Push endpoint returned {response.status_code}: {response.text}")
                return False
        except Exception as e:
            logger.error(f"Push notification error for user {user_id}: {e}")
            return False

    # ===== Multi-recipient helpers =====

    def get_item_recipients(self, item, exclude_user_id=None) -> List[int]:
        """
        Return deduplicated list of user IDs to notify for an item event.
        Includes: assignee + all watchers, minus the actor (exclude_user_id)
        and any other user IDs passed as a set/list.
        """
        from tasks.models import ItemWatcher

        user_ids = set()

        # Assignee (by ID or resolved from email)
        if item.assignee_id:
            user_ids.add(item.assignee_id)
        elif item.assignee_email:
            # Resolve email to user_id via shizuha-id
            try:
                from tasks.user_service import user_service
                resolved = user_service.resolve_user_info(email=item.assignee_email)
                if resolved and resolved.get('id'):
                    user_ids.add(resolved['id'])
            except Exception:
                pass

        # Watchers
        watcher_ids = ItemWatcher.objects.filter(
            item=item
        ).values_list('user_id', flat=True)
        user_ids.update(watcher_ids)

        # Exclude the actor (and any additional users a caller wants to skip,
        # e.g. the assignee when they already received a direct DM).
        if isinstance(exclude_user_id, (set, list, tuple)):
            for uid in exclude_user_id:
                if uid:
                    user_ids.discard(uid)
        elif exclude_user_id:
            user_ids.discard(exclude_user_id)

        return list(user_ids)

    # ── Notification Delegation ──

    def _get_delegates(self, user_id: int, event_type: str = '') -> list:
        """Get active delegates for a user, filtered by event type.

        PLS-228: ORM over the NotificationDelegate model (was raw SQL against a
        prod-only table with no migration — a missing table on a fresh DB raised
        ProgrammingError and aborted the enclosing transaction). The model +
        create-if-absent migration guarantee the table exists everywhere.
        """
        import json as _json
        from tasks.models import NotificationDelegate

        delegates = []
        rows = (NotificationDelegate.objects
                .filter(user_id=user_id, is_active=True)
                .values('delegate_id', 'delegate_email', 'delegated_events'))
        for row in rows:
            delegated_events = row['delegated_events']
            # Filter by event type if the delegation is scoped to specific events.
            # delegated_events is a JSON string (or already a list); empty = all.
            events = delegated_events if isinstance(delegated_events, list) else []
            if isinstance(delegated_events, str) and delegated_events:
                try:
                    events = _json.loads(delegated_events)
                except Exception:
                    events = []
            if events and event_type and event_type not in events:
                continue
            delegates.append({'id': row['delegate_id'], 'email': row['delegate_email']})
        return delegates

    def _create_delegated_notifications(self, original_user_id: int, event_type: str,
                                         title: str, message: str, severity: str = 'info',
                                         item=None, fields=None, exclude_user_ids=None):
        """Create notification copies for all delegates of the original user."""
        exclude = set(exclude_user_ids or [])
        delegates = self._get_delegates(original_user_id, event_type)

        for delegate in delegates:
            did = delegate['id']
            # Skip if delegate was already notified (as direct recipient or actor)
            if did in exclude:
                continue

            # Create delegated notification
            from tasks.models import Notification
            from django.db import IntegrityError, transaction
            try:
                with transaction.atomic():
                    Notification.objects.create(
                        user_id=did,
                        item=item,
                        notification_type=event_type,
                        title=f'{title}',
                        message=message,
                        delegated_from_id=original_user_id,
                    )
                # Deliver as a message to the agent's inbox (if delegate is an agent)
                self._deliver_to_agent(delegate['email'], title, message, event_type, item)
            except IntegrityError:
                # Benign race: the item was deleted between the triggering event and
                # notification creation. Nothing left to notify about, so log quietly
                # (debug) instead of error — this used to fire a spurious alert.
                logger.debug(f'Skipped delegated notification for user {did}: item no longer exists')
            except Exception as e:
                logger.error(f'Failed to create delegated notification for user {did}: {e}')


    def _deliver_to_agent(self, agent_email: str, title: str, message: str, event_type: str, item=None):
        """Deliver notification as a message to an agent via the agent service's internal API."""
        import os
        import urllib.request
        import json as _json
        from tasks.net_utils import safe_urlopen  # PLAT-1572: scheme-guarded urlopen

        # Only deliver to agent emails (@shizuha.com or @agents.shizuha.io)
        if not agent_email or ('@shizuha.com' not in agent_email and '@agents.shizuha.io' not in agent_email):
            return

        # Format the notification as a chat message with workflow context
        item_key = ''
        workflow_context = ''
        if item:
            item_key = getattr(item, 'item_key', '') or f'#{getattr(item, "id", "")}'
            wf_name = getattr(item, 'workflow_name', None)
            wf_status = getattr(item, 'workflow_status', None)
            ag = getattr(item, 'assignment_group', None)
            if wf_name and wf_status:
                workflow_context = '\nWorkflow: ' + str(wf_name) + ' | Current status: ' + str(wf_status)
                if ag:
                    workflow_context += f' | Team: {ag}'
                # Get available transitions
                try:
                    from tasks.models import Workflow, WorkflowTransition
                    wf = None
                    for slug in [wf_name, wf_name.replace('-', '_'), wf_name.replace('_', '-')]:
                        try:
                            wf = Workflow.objects.get(slug__iexact=slug)
                            break
                        except Workflow.DoesNotExist:
                            continue
                    if wf:
                        transitions = WorkflowTransition.objects.filter(workflow=wf, from_status=wf_status)
                        if transitions.exists():
                            trans_list = ', '.join(f'{t.name} (→ {t.to_status})' for t in transitions)
                            workflow_context += "\nAvailable transitions: " + trans_list
                except Exception:
                    pass

        content = f"""[Notification — {event_type}]
{title}

{message}
{workflow_context}
{f'Task: {item_key}' if item_key else ''}
This notification was delegated to you from the user you assist. Review it and inform them — offer to advance the workflow if applicable.""".strip()

        try:
            # Call shizuha-agent's internal dispatch API (same Docker network)
            agent_service_url = getattr(settings, 'SHIZUHA_AGENT_API_URL', '').rstrip('/')
            if not agent_service_url:
                logger.debug('Legacy shizuha-agent dispatch is disabled; skipping delegate delivery to %s', agent_email)
                return
            orchestrator_token = os.environ.get('ORCHESTRATOR_TOKEN', '').strip()

            # Use the chatbot/send/ endpoint (same path as user chat messages)
            # Auth: service JWT signed with shared secret
            import jwt as _jwt, datetime as _dt
            jwt_secret = os.environ.get('JWT_SECRET_KEY', '').strip()
            if not orchestrator_token or not jwt_secret:
                logger.warning('Legacy agent dispatch requires configured ORCHESTRATOR_TOKEN and JWT_SECRET_KEY')
                return
            service_jwt = _jwt.encode({
                'user_id': 999, 'email': 'system@shizuha.com', 'username': 'system',
                'is_staff': True, 'is_superuser': True,
                'exp': _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=1),
                'iat': _dt.datetime.now(_dt.timezone.utc),
                'token_type': 'access',
            }, jwt_secret, algorithm='HS256')

            # Find agent UUID from email
            from tasks.user_service import user_service as _us
            agent_info = _us.resolve_user_info(email=agent_email)
            # Resolve agent UUID from agent service
            agent_uuid = None
            try:
                resolve_url = f'{agent_service_url}/api/internal/agents/by-email/?email={agent_email}'
                resolve_req = urllib.request.Request(resolve_url, headers={
                    'Authorization': f'Bearer {orchestrator_token}',
                })
                resolve_resp = safe_urlopen(resolve_req, timeout=3)
                agent_data = _json.loads(resolve_resp.read())
                agent_uuid = agent_data.get('id')
            except Exception:
                pass
            if not agent_uuid:
                logger.warning(f'Could not resolve agent UUID for {agent_email}')
                return

            # Use a stable session ID for notifications (avoids creating new sessions per notification)
            import hashlib
            # usedforsecurity=False: this md5 only derives a *stable* session id from the
            # agent UUID (deterministic dedup key, not a security primitive) — declaring
            # intent clears bandit B324; the digest is unchanged so existing ids stay identical.
            stable_session_id = str(__import__('uuid').UUID(hashlib.md5(f'notification-{agent_uuid}'.encode(), usedforsecurity=False).hexdigest()))

            url = f'{agent_service_url}/api/chatbot/send/'
            data = _json.dumps({
                'agent_id': agent_uuid,
                'content': content,
                'source_service': 'pulse-notification',
                'session_id': stable_session_id,
            }).encode()
            req = urllib.request.Request(url, data=data, headers={
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {service_jwt}',
            }, method='POST')
            resp = safe_urlopen(req, timeout=5)
            result = _json.loads(resp.read())
            logger.info(f'Dispatched notification to {agent_email}: {result}')
        except Exception as e:
            logger.warning(f'Could not deliver notification to {agent_email}: {e}')

    def notify_item_event(
        self,
        item,
        event_type: str,
        title: str,
        message: str,
        exclude_user_id=None,
        severity: str = 'info',
        fields: List[Dict] = None,
    ) -> Dict[str, Any]:
        """
        Send a notification to all recipients (assignee + watchers) for an item event.
        """
        recipients = self.get_item_recipients(item, exclude_user_id)
        results = {'recipients': len(recipients), 'success': 0, 'failed': 0}

        for user_id in recipients:
            r = self.notify_user(
                user_id=user_id,
                title=title,
                message=message,
                event_type=event_type,
                severity=severity,
                item=item,
                fields=fields,
            )
            results['success'] += r['success']
            results['failed'] += r['failed']

        return results

    # ===== Item lifecycle notifications =====

    def notify_status_changed(self, item, old_status: str, new_status: str, changed_by: str = '', exclude_user_id: int = None) -> Dict[str, Any]:
        """Notify when an item's status changes.

        Delivery model:
          * The assignee receives ONE direct `[Task Update]` message via
            Connect (primary, actionable — they own the work).
          * Watchers / other participants receive the generic item-event
            notification path. The assignee is excluded from that path so
            they don't get two messages for the same transition.
        """
        item_key = item.item_key or f'#{item.id}'
        title = f"{item_key}: Status Changed"
        old_display = old_status.replace('_', ' ').title()
        new_display = new_status.replace('_', ' ').title()
        message = f"<b>{item.title}</b>\n{old_display} → {new_display}"
        if changed_by:
            message += f"\nBy: {changed_by}"

        fields = [
            {'name': 'From', 'value': old_display, 'inline': True},
            {'name': 'To', 'value': new_display, 'inline': True},
        ]

        # SCLI-48: resolve assignee_email → user_id when assignee_id is unset,
        # MIRRORING notify_assignment_changed. assign_team post-functions set
        # assignee_email but leave assignee_id=None (shizuha-id is the identity
        # source-of-truth), so without this resolution a status change — e.g. a
        # reviewer's "Request Changes" routing the task back to its team-routed
        # assignee — fired NO [Task Update] and the agent was never re-poked,
        # sitting idle on a ready queue. The assignment path already resolved;
        # the status path did not. Now both do.
        recipient_user_id = item.assignee_id
        if not recipient_user_id and item.assignee_email:
            try:
                from tasks.user_service import user_service
                info = user_service.resolve_user_info(email=item.assignee_email)
                if info:
                    recipient_user_id = info.get('id')
            except Exception:
                logger.debug("Could not resolve assignee_email for status notification", exc_info=True)

        direct_delivered_to = None
        # SCLI-48 (Codex P2): honor exclude_user_id on the direct path too. When the
        # actor making the status change IS the (email-resolved) assignee — e.g. an
        # agent transitioning its OWN task to in_progress/in_review — callers pass that
        # actor as exclude_user_id. Without this guard the direct POST below would send
        # the agent a [Task Update] about its own action (self-poke noise); the later
        # extra_exclude only covers notify_item_event, not this POST.
        # Subsumes PLS-49 (skip self-notification): this uses recipient_user_id (the
        # email-resolved id) instead of item.assignee_id, so it ALSO covers team-routed
        # assignees whose assignee_id is None — the case PLS-49's item.assignee_id guard
        # silently skipped, which is the SCLI-48 bug.
        if recipient_user_id and recipient_user_id != exclude_user_id:
            try:
                import requests as req
                from tasks.models import Comment
                by_line = f'\nBy: {changed_by}' if changed_by else ''
                comment_line = ''
                try:
                    latest_comment = Comment.objects.filter(item_id=item.id).order_by('-created_at').first()
                    if latest_comment:
                        author = latest_comment.author_name or latest_comment.author_email or 'unknown'
                        comment_line = f'\n\nLatest comment from {author}:\n"{latest_comment.content[:300]}"'
                except Exception:
                    pass
                connect_base_url = getattr(settings, 'SHIZUHA_CONNECT_API_URL', 'http://shizuha-connect:8019/api').rstrip('/')
                _sm_body = {
                    'recipient_user_id': recipient_user_id,
                    'content': f'[Task Update] {item_key}: {item.title}\nStatus: {old_display} → {new_display}{by_line}{comment_line}\nUse pulse_get_task("{item_key}") for full details and comments.',
                    'sender_name': 'Pulse',
                    'sender_service': 'shizuha-pulse',
                }
                resp = req.post(f'{connect_base_url}/internal/system-message/', json=_sm_body,
                                headers=system_message_headers(_sm_body), timeout=5)
                if 200 <= resp.status_code < 300:
                    direct_delivered_to = recipient_user_id
            except Exception:
                logger.debug("Failed to send Connect status notification", exc_info=True)

        # Watchers / followers go through notify_item_event. Skip the
        # assignee here if they already got the direct message — otherwise
        # they'd see the same transition twice.
        extra_exclude = {exclude_user_id} if exclude_user_id else set()
        if direct_delivered_to:
            extra_exclude.add(direct_delivered_to)

        return self.notify_item_event(
            item=item, event_type='status_change', title=title,
            message=message, exclude_user_id=extra_exclude or None, fields=fields,
        )

    def notify_task_completed(self, item, completed_by: str = '', exclude_user_id: int = None) -> Dict[str, Any]:
        """Notify when a task is completed."""
        item_key = item.item_key or f'#{item.id}'
        title = f"{item_key}: Completed"
        message = f"<b>{item.title}</b>\nMarked as complete"
        if completed_by:
            message += f" by {completed_by}"

        return self.notify_item_event(
            item=item, event_type='status_change', title=title,
            message=message, exclude_user_id=exclude_user_id, severity='success',
        )

    def notify_assignment_changed(self, item, assigned_by: str = '', exclude_user_id: int = None) -> Dict[str, Any]:
        """Notify when an item is assigned or reassigned."""
        item_key = item.item_key or f'#{item.id}'
        assignee_name = item.assignee_email or f'user {item.assignee_id}'
        title = f"{item_key}: Assigned to {assignee_name}"
        message = f"<b>{item.title}</b>\nAssigned to {assignee_name}"
        if assigned_by:
            message += f"\nBy: {assigned_by}"

        fields = [
            {'name': 'Assignee', 'value': assignee_name, 'inline': True},
        ]
        if item.priority:
            fields.append({'name': 'Priority', 'value': item.priority.title(), 'inline': True})

        # Send via Connect so agents receive assignment in real-time.
        # Post-functions (assign_team) set assignee_email but leave assignee_id=None
        # because shizuha-id is the source-of-truth for user identity. Resolve
        # the email → user_id on the fly so notifications still fire.
        recipient_user_id = item.assignee_id
        if not recipient_user_id and item.assignee_email:
            try:
                from tasks.user_service import user_service
                info = user_service.resolve_user_info(email=item.assignee_email)
                if info:
                    recipient_user_id = info.get('id')
            except Exception:
                logger.debug("Could not resolve assignee_email for notification", exc_info=True)

        # PLS-49: skip Connect DM if the new assignee is the actor (self-notification).
        if recipient_user_id and recipient_user_id != exclude_user_id:
            try:
                import requests as req
                from tasks.models import Comment
                priority = getattr(item, 'priority', 'normal') or 'normal'
                status = getattr(item, 'status', 'pending') or 'pending'
                by_line = f'\nAssigned by: {assigned_by}' if assigned_by else ''
                # Include latest comment so agent sees the context
                comment_line = ''
                try:
                    latest_comment = Comment.objects.filter(item_id=item.id).order_by('-created_at').first()
                    if latest_comment:
                        author = latest_comment.author_name or latest_comment.author_email or 'unknown'
                        comment_line = f'\n\nLatest comment from {author}:\n"{latest_comment.content[:300]}"'
                except Exception:
                    pass
                recurring_line = ''
                if getattr(item, 'is_recurring', False):
                    try:
                        from tasks.services.recurring_cycle_state import notification_state_line
                        recurring_line = f'\n{notification_state_line(item)}'
                    except Exception:
                        logger.debug("Could not render recurring assignment fence", exc_info=True)
                connect_base_url = getattr(settings, 'SHIZUHA_CONNECT_API_URL', 'http://shizuha-connect:8019/api').rstrip('/')
                _sm_body = {
                    'recipient_user_id': recipient_user_id,
                    'content': f'[Task Assigned] {item_key}: {item.title}\nPriority: {priority} | Status: {status}{by_line}{comment_line}{recurring_line}\nUse pulse_get_task("{item_key}") for full details and comments.',
                    'sender_name': 'Pulse',
                    'sender_service': 'shizuha-pulse',
                }
                req.post(f'{connect_base_url}/internal/system-message/', json=_sm_body,
                         headers=system_message_headers(_sm_body), timeout=5)
            except Exception:
                logger.debug("Failed to send Connect assignment notification", exc_info=True)

        return self.notify_item_event(
            item=item, event_type='assignment', title=title,
            message=message, exclude_user_id=exclude_user_id, fields=fields,
        )

    def notify_priority_changed(self, item, old_priority: str, new_priority: str, changed_by: str = '', exclude_user_id: int = None) -> Dict[str, Any]:
        """Notify when an item's priority changes."""
        item_key = item.item_key or f'#{item.id}'
        old_emoji = self.PRIORITY_EMOJI.get(old_priority, '')
        new_emoji = self.PRIORITY_EMOJI.get(new_priority, '')
        title = f"{item_key}: Priority Changed"
        message = f"<b>{item.title}</b>\n{old_emoji} {old_priority.title()} → {new_emoji} {new_priority.title()}"
        if changed_by:
            message += f"\nBy: {changed_by}"

        fields = [
            {'name': 'From', 'value': f"{old_emoji} {old_priority.title()}", 'inline': True},
            {'name': 'To', 'value': f"{new_emoji} {new_priority.title()}", 'inline': True},
        ]

        return self.notify_item_event(
            item=item, event_type='status_change', title=title,
            message=message, exclude_user_id=exclude_user_id, fields=fields,
        )

    def notify_comment_added(self, item, comment_text: str, author_name: str = '', exclude_user_id: int = None) -> Dict[str, Any]:
        """Notify when a comment is added to an item."""
        item_key = item.item_key or f'#{item.id}'
        title = f"{item_key}: New Comment"
        preview = comment_text[:200] + '...' if len(comment_text) > 200 else comment_text
        message = f"<b>{item.title}</b>\n💬 {preview}"
        if author_name:
            message = f"<b>{item.title}</b>\n💬 <i>{author_name}</i>: {preview}"

        return self.notify_item_event(
            item=item, event_type='comment', title=title,
            message=message, exclude_user_id=exclude_user_id,
        )

    # ===== Convenience methods for common notifications =====

    def notify_task_reminder(self, item) -> Dict[str, Any]:
        """Send task reminder notification"""
        title = "Task Reminder"

        priority_emoji = self.PRIORITY_EMOJI.get(item.priority, '')
        message = f"{priority_emoji} <b>{item.title}</b>"

        if item.due_date:
            message += f"\n📅 Due: {item.due_date.strftime('%b %d, %Y at %I:%M %p')}"

        if item.description:
            desc = item.description[:200] + '...' if len(item.description) > 200 else item.description
            message += f"\n\n{desc}"

        fields = []
        if item.priority:
            fields.append({'name': 'Priority', 'value': item.priority.title(), 'inline': True})
        if item.tags:
            fields.append({'name': 'Tags', 'value': item.tags, 'inline': True})

        return self.notify_user(
            user_id=item.assignee_id,
            title=title,
            message=message,
            event_type='reminder',
            severity='reminder',
            item=item,
            fields=fields
        )

    def notify_task_overdue(self, item) -> Dict[str, Any]:
        """Send task overdue notification"""
        title = "Task Overdue!"

        priority_emoji = self.PRIORITY_EMOJI.get(item.priority, '')
        message = f"{priority_emoji} <b>{item.title}</b>"
        message += f"\n\n⚠️ This task is now overdue!"

        if item.due_date:
            message += f"\n📅 Was due: {item.due_date.strftime('%b %d, %Y at %I:%M %p')}"

        fields = [
            {'name': 'Priority', 'value': item.priority.title(), 'inline': True},
            {'name': 'Status', 'value': item.status.replace('_', ' ').title(), 'inline': True}
        ]

        return self.notify_user(
            user_id=item.assignee_id,
            title=title,
            message=message,
            event_type='overdue',
            severity='overdue',
            item=item,
            fields=fields
        )

    def notify_alert(
        self, item, user_ids: Optional[List[int]] = None,
        *, allow_connect_defer: bool = True,
    ) -> Dict[str, Any]:
        """Send alert notification.

        By default alerts notify their concrete assignee. Project/group-routed
        Alertmanager alerts are intentionally unassigned until someone claims
        them, so callers may pass an explicit fallback recipient set.
        """
        severity = item.severity or 'warning'
        title = f"Alert: {item.title}"

        message = item.description or "No description provided"

        if item.source:
            message += f"\n\n📡 Source: {item.source}"

        fields = [
            {'name': 'Severity', 'value': severity.title(), 'inline': True},
            {'name': 'Type', 'value': item.item_type, 'inline': True}
        ]

        if item.labels:
            label_str = ', '.join(f"{k}={v}" for k, v in item.labels.items())
            if len(label_str) < 100:
                fields.append({'name': 'Labels', 'value': label_str, 'inline': False})

        recipients = user_ids if user_ids is not None else [item.assignee_id]
        recipients = [uid for uid in dict.fromkeys(recipients or []) if uid]
        if not recipients:
            return {'success': 0, 'failed': 0, 'channels': [], 'recipients': [], 'connect_success': 0}

        aggregate = {
            'success': 0,
            'failed': 0,
            'channels': [],
            'recipients': recipients,
            'connect_success': 0,
            'connect_deferred': 0,
        }
        for user_id in recipients:
            if (
                allow_connect_defer
                and not _alert_defer_exhausted(user_id, item)
                and _connect_alert_has_higher_priority_task(user_id, item)
            ):
                aggregate['connect_deferred'] += 1
                _record_alert_defer(user_id, item)
                logger.info(
                    'Deferred lower-priority alert Connect wake item=%s recipient=%s',
                    getattr(item, 'item_key', None) or getattr(item, 'id', None),
                    user_id,
                )
            elif self._send_connect_alert_message(user_id, item):
                aggregate['connect_success'] += 1
                _clear_alert_defer(user_id, item)

            result = self.notify_user(
                user_id=user_id,
                title=title,
                message=message,
                event_type='alert',
                severity=severity,
                item=item,
                fields=fields,
                exclude_delegate_user_ids=set(recipients),
            )
            aggregate['success'] += result.get('success', 0)
            aggregate['failed'] += result.get('failed', 0)
            aggregate['channels'].extend(result.get('channels', []))
        return aggregate

    def _send_connect_alert_message(self, user_id: int, item) -> bool:
        """Post an actionable alert packet into the recipient's system DM.

        ``item`` may be a real ``Item`` or a lightweight packet (DM-only path —
        no Pulse row). Do not require item_key/id for notification content.
        """
        item_ref = getattr(item, 'item_key', None) or (
            f'#{item.id}' if getattr(item, 'id', None) else (getattr(item, 'source_id', None) or 'alert')
        )
        # PLS-706: read EVERY optional field defensively, not just the one that
        # broke. The docstring already promises a lightweight packet is
        # acceptable, but `priority` was read directly and the DM-only packet
        # did not set it — so `notify_alert` raised AttributeError *before*
        # `notify_user`, losing the alert on every channel while the Celery
        # task still reported `succeeded`. 41 alerts were dropped that way,
        # including `AlertmanagerFailedToSendAlerts` itself.
        #
        # Fixing only `priority` would leave the class open: the packet and
        # this consumer are coupled by convention, so the next field added
        # here breaks the DM path again, just as silently.
        severity = getattr(item, 'severity', None) or 'warning'
        priority = (
            getattr(item, 'priority', None)
            or _SEVERITY_PRIORITY.get(str(severity).strip().lower(), 'normal')
        )
        status = getattr(item, 'status', None) or 'firing'
        team = getattr(item, 'assignment_group', None) or 'unassigned'
        raw_labels = getattr(item, 'labels', None)
        labels = raw_labels if isinstance(raw_labels, dict) else {}
        raw_payload = getattr(item, 'payload', None)
        payload = raw_payload if isinstance(raw_payload, dict) else {}
        annotations = payload.get('annotations') if isinstance(payload.get('annotations'), dict) else {}

        description = (
            annotations.get('summary')
            or getattr(item, 'description', None)
            or annotations.get('description')
            or 'No description provided'
        ).strip()
        if len(description) > 500:
            description = f'{description[:497]}...'

        interesting = (
            'alertname', 'namespace', 'service', 'deployment', 'pod',
            'node', 'instance', 'job', 'model', 'assignment_group',
        )
        label_pairs = [f'{key}={labels[key]}' for key in interesting if labels.get(key)]
        label_line = ', '.join(label_pairs)
        if len(label_line) > 300:
            label_line = f'{label_line[:297]}...'

        heading = 'Alert Resolved' if str(status).lower() == 'resolved' else 'Alert'
        lines = [
            f'[{heading}] {item_ref}: {getattr(item, "title", None) or "alert"}',
            f'Severity: {severity} | Priority: {priority} | Status: {status} | Team: {team}',
            '',
            description,
        ]
        if label_line:
            lines.extend(['', f'Labels: {label_line}'])
        source_url = getattr(item, 'source_url', None)
        if source_url:
            lines.append(f'Source: {source_url}')
        if payload.get('starts_at'):
            lines.append(f'Started: {payload.get("starts_at")}')
        runbook_url = annotations.get('runbook_url')
        lines.extend(['', 'Alerts are DM-only (not Pulse tasks).'])
        if getattr(item, 'id', None):
            lines.append(f'Use pulse_get_task("{item_ref}") for the Pulse item and comments.')
        lines.extend([
            f'Runbook: {runbook_url}' if runbook_url else 'Runbook: deploy/k3s/monitoring/agent-alert-remediation-runbook.md',
            'Call pulse_get_my_work (alerts + tasks in one snapshot) and choose what to advance. Empty alerts does not mean an empty queue.',
            'Start with live state/logs. Do not perform hardware, data-loss, or destructive actions automatically.',
        ])
        try:
            connect_base_url = getattr(
                settings,
                'SHIZUHA_CONNECT_API_URL',
                'http://shizuha-connect:8019/api',
            ).rstrip('/')
            _sm_body = {
                'recipient_user_id': user_id,
                'content': '\n'.join(lines),
                'sender_name': 'Pulse Alerts',
                'sender_service': 'shizuha-pulse',
            }
            resp = requests.post(
                f'{connect_base_url}/internal/system-message/',
                json=_sm_body,
                headers=system_message_headers(_sm_body),
                timeout=5,
            )
            return 200 <= resp.status_code < 300
        except Exception:
            logger.debug("Failed to send Connect alert notification", exc_info=True)
            return False

    def notify_action_pending(self, item) -> Dict[str, Any]:
        """Send pending action notification"""
        title = "Action Pending Approval"

        message = f"<b>{item.title}</b>"

        if item.description:
            message += f"\n\n{item.description[:300]}"

        if item.due_date:
            message += f"\n\n⏱️ Expires: {item.due_date.strftime('%b %d, %Y at %I:%M %p')}"

        fields = [
            {'name': 'Type', 'value': item.item_type, 'inline': True},
            {'name': 'Priority', 'value': item.priority.title(), 'inline': True}
        ]

        if item.source:
            fields.append({'name': 'Source', 'value': item.source, 'inline': True})

        return self.notify_user(
            user_id=item.assignee_id,
            title=title,
            message=message,
            event_type='action',
            severity='warning',
            item=item,
            fields=fields
        )


# Global instance
notification_service = UserNotificationService()
