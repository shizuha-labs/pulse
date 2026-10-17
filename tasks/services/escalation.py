"""
Escalation Service

Handles escalation of items that haven't been handled within specified timeframes.
"""

import logging
from typing import Optional, List
from django.utils import timezone
from datetime import timedelta

from tasks.models import Item, EscalationRule

logger = logging.getLogger(__name__)


class EscalationService:
    """
    Service for handling item escalation.

    Escalation rules define what happens when items aren't handled in time:
    - Bump severity
    - Add notification channels
    - Reassign to different user/team
    - Create follow-up action items
    """

    SEVERITY_ORDER = ['info', 'warning', 'error', 'critical']

    @classmethod
    def find_matching_rule(cls, item: Item) -> Optional[EscalationRule]:
        """
        Find an escalation rule that matches the given item.
        """
        rules = EscalationRule.objects.filter(is_active=True)

        for rule in rules:
            if cls._rule_matches(rule, item):
                return rule

        return None

    @classmethod
    def _rule_matches(cls, rule: EscalationRule, item: Item) -> bool:
        """
        Check if an escalation rule matches an item.
        """
        import fnmatch

        # Check modes
        if rule.modes and item.mode not in rule.modes:
            return False

        # Check item_types (supports wildcards)
        if rule.item_types:
            if not any(fnmatch.fnmatch(item.item_type, pattern) for pattern in rule.item_types):
                return False

        # Check sources
        if rule.sources and item.source not in rule.sources:
            return False

        # Check minimum severity
        if rule.min_severity and item.severity:
            min_idx = cls.SEVERITY_ORDER.index(rule.min_severity) if rule.min_severity in cls.SEVERITY_ORDER else 0
            item_idx = cls.SEVERITY_ORDER.index(item.severity) if item.severity in cls.SEVERITY_ORDER else 0
            if item_idx < min_idx:
                return False

        # Check label matchers
        if rule.label_matchers:
            for key, value in rule.label_matchers.items():
                if item.labels.get(key) != value:
                    return False

        return True

    @classmethod
    def get_next_escalation_stage(cls, item: Item, rule: EscalationRule) -> Optional[dict]:
        """
        Get the next escalation stage for an item based on its current level.

        Returns:
            The stage configuration dict or None if no more stages
        """
        current_level = item.escalation_level
        stages = rule.stages or []

        if current_level < len(stages):
            return stages[current_level]

        return None

    @classmethod
    def should_escalate(cls, item: Item, rule: EscalationRule) -> bool:
        """
        Check if an item should be escalated based on timing.
        """
        stage = cls.get_next_escalation_stage(item, rule)
        if not stage:
            return False

        delay_minutes = stage.get('delay_minutes', 0)

        # Calculate time since item was created or last escalated
        if item.last_escalated_at:
            time_since_action = timezone.now() - item.last_escalated_at
        else:
            # For alerts, use first_fired_at; for others, use created_at
            reference_time = item.first_fired_at if item.mode == 'alert' else item.created_at
            if reference_time:
                time_since_action = timezone.now() - reference_time
            else:
                time_since_action = timedelta(0)

        return time_since_action >= timedelta(minutes=delay_minutes)

    @classmethod
    def escalate_item(cls, item: Item, rule: EscalationRule) -> bool:
        """
        Escalate an item to the next stage.

        Returns:
            True if escalation was performed, False otherwise
        """
        stage = cls.get_next_escalation_stage(item, rule)
        if not stage:
            logger.info(f"Item {item.id} has no more escalation stages")
            return False

        actions = stage.get('actions', [])
        escalated = False

        for action in actions:
            action_type = action.get('type')

            if action_type == 'bump_severity':
                new_severity = action.get('to')
                if new_severity and new_severity in cls.SEVERITY_ORDER:
                    old_severity = item.severity
                    item.severity = new_severity
                    logger.info(f"Item {item.id} severity bumped: {old_severity} -> {new_severity}")
                    escalated = True

            elif action_type == 'add_channels':
                channels = action.get('channels', [])
                # Store escalation channels in notification_preferences
                prefs = item.notification_preferences or {}
                existing_channels = prefs.get('escalation_channels', [])
                prefs['escalation_channels'] = list(set(existing_channels + channels))
                item.notification_preferences = prefs
                logger.info(f"Item {item.id} channels added: {channels}")
                escalated = True

            elif action_type == 'reassign':
                new_assignee = action.get('to')
                if new_assignee:
                    # Could be a user ID, email, or special value like "oncall"
                    item.metadata['escalation_reassign'] = new_assignee
                    logger.info(f"Item {item.id} marked for reassignment to: {new_assignee}")
                    escalated = True

            elif action_type == 'create_action':
                # Create a new action item for follow-up
                cls._create_escalation_action(item, action)
                escalated = True

            elif action_type == 'set_priority':
                new_priority = action.get('to')
                if new_priority:
                    item.priority = new_priority
                    logger.info(f"Item {item.id} priority set to: {new_priority}")
                    escalated = True

        if escalated:
            item.escalation_level += 1
            item.last_escalated_at = timezone.now()
            item.escalation_rule = rule
            item.save()
            logger.info(f"Item {item.id} escalated to level {item.escalation_level}")

        return escalated

    @classmethod
    def _create_escalation_action(cls, original_item: Item, action_config: dict) -> Item:
        """
        Create a new action item as part of escalation.
        """
        action_type = action_config.get('action_type', 'action.escalation')
        title = action_config.get('title', f'Escalation: {original_item.title}')

        action = Item.objects.create(
            mode='action',
            item_type=action_type,
            title=title,
            description=f"Auto-created due to escalation of item {original_item.id}",
            source='escalation',
            source_id=str(original_item.id),
            priority='high',
            requires_approval=True,
            payload={
                'original_item_id': original_item.id,
                'original_item_type': original_item.item_type,
                'escalation_level': original_item.escalation_level,
            },
            labels=original_item.labels,
        )

        logger.info(f"Created escalation action {action.id} for item {original_item.id}")
        return action

    @classmethod
    def process_pending_escalations(cls) -> List[Item]:
        """
        Process all items that need escalation.
        Should be called periodically by a Celery task.

        Returns:
            List of items that were escalated
        """
        escalated_items = []

        # Get active items that might need escalation
        active_items = Item.objects.filter(
            status__in=['firing', 'acknowledged', 'pending', 'in_progress']
        ).exclude(
            silenced_until__gt=timezone.now()
        )

        for item in active_items:
            rule = item.escalation_rule or cls.find_matching_rule(item)
            if not rule:
                continue

            if cls.should_escalate(item, rule):
                if cls.escalate_item(item, rule):
                    escalated_items.append(item)

        if escalated_items:
            logger.info(f"Escalated {len(escalated_items)} items")

        return escalated_items

    @classmethod
    def schedule_escalation(cls, item: Item) -> Optional[dict]:
        """
        Determine when the next escalation should occur for an item.

        Returns:
            Dict with 'delay_minutes' and 'stage' info, or None if no escalation scheduled
        """
        rule = item.escalation_rule or cls.find_matching_rule(item)
        if not rule:
            return None

        stage = cls.get_next_escalation_stage(item, rule)
        if not stage:
            return None

        return {
            'delay_minutes': stage.get('delay_minutes', 0),
            'stage': item.escalation_level + 1,
            'rule_name': rule.name,
        }
