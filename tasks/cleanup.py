"""
Database cleanup framework for the tasks service.

This module provides cleanup functionality specifically for the tasks service,
with different retention policies appropriate for task management.
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any
from django.db import models, transaction
from django.utils import timezone
from django.conf import settings


logger = logging.getLogger(__name__)


class TaskCleanupRule:
    """
    Defines a cleanup rule specifically for task-related models.
    """
    
    def __init__(
        self,
        model_class: models.Model,
        retention_days: int = 90,
        date_field: str = 'created_at',
        conditions: Optional[Dict[str, Any]] = None,
        batch_size: int = 1000,
        description: str = ""
    ):
        """
        Initialize a task cleanup rule.
        
        Args:
            model_class: The Django model class to clean up
            retention_days: How many days to retain data
            date_field: The datetime field to use for age calculation
            conditions: Additional filter conditions for the cleanup
            batch_size: Number of records to delete in each batch
            description: Human-readable description of the cleanup rule
        """
        self.model_class = model_class
        self.retention_days = retention_days
        self.date_field = date_field
        self.conditions = conditions or {}
        self.batch_size = batch_size
        self.description = description or f"Clean up {model_class._meta.verbose_name}"
        
    def get_cutoff_time(self) -> datetime:
        """Get the cutoff time for this cleanup rule."""
        return timezone.now() - timedelta(days=self.retention_days)
    
    def get_queryset(self) -> models.QuerySet:
        """Get the queryset of records to be deleted."""
        cutoff_time = self.get_cutoff_time()
        filter_conditions = {
            f"{self.date_field}__lt": cutoff_time,
            **self.conditions
        }
        return self.model_class.objects.filter(**filter_conditions)
    
    def count_records_to_delete(self) -> int:
        """Count how many records would be deleted."""
        return self.get_queryset().count()
    
    def execute_cleanup(self, dry_run: bool = False) -> Tuple[int, Dict[str, int]]:
        """
        Execute the cleanup for this rule.
        
        Args:
            dry_run: If True, only count records without deleting
            
        Returns:
            Tuple of (total_deleted, per_model_deleted_count)
        """
        if dry_run:
            count = self.count_records_to_delete()
            logger.info(f"[DRY RUN] Would delete {count} records from {self.model_class._meta.label}")
            return count, {self.model_class._meta.label: count}
        
        queryset = self.get_queryset()
        model_label = self.model_class._meta.label
        
        logger.info(f"Starting cleanup for {model_label}. Found {queryset.count()} records to delete.")
        
        deleted_count = 0
        while True:
            # Get a batch of IDs to delete
            batch_ids = list(queryset.values_list('pk', flat=True)[:self.batch_size])
            if not batch_ids:
                break
                
            # Delete the batch
            with transaction.atomic():
                batch_deleted, _ = self.model_class.objects.filter(pk__in=batch_ids).delete()
                deleted_count += batch_deleted
                
            logger.debug(f"Deleted batch of {batch_deleted} records from {model_label}")
        
        logger.info(f"Completed cleanup for {model_label}. Deleted {deleted_count} records.")
        
        return deleted_count, {model_label: deleted_count}


class TaskDatabaseCleaner:
    """
    Database cleaner specifically for the tasks service.
    """
    
    def __init__(self):
        self.rules: List[TaskCleanupRule] = []
        self._setup_default_rules()
    
    def _setup_default_rules(self):
        """Setup default cleanup rules for the tasks service."""
        from django.conf import settings
        from tasks.models import Task, Item

        # Completed tasks cleanup (keep for 6 months)
        self.add_rule(TaskCleanupRule(
            model_class=Task,
            retention_days=180,  # 6 months
            date_field='completed_at',
            conditions={'status': 'completed'},
            description="Clean up completed tasks older than 6 months"
        ))
        
        # Cancelled tasks cleanup (keep for 3 months)
        self.add_rule(TaskCleanupRule(
            model_class=Task,
            retention_days=90,  # 3 months
            date_field='updated_at',
            conditions={'status': 'cancelled'},
            description="Clean up cancelled tasks older than 3 months"
        ))
        
        # Old one-time tasks that are overdue (keep for 1 month)
        self.add_rule(TaskCleanupRule(
            model_class=Task,
            retention_days=30,  # 1 month
            date_field='due_date',
            conditions={
                'status__in': ['pending', 'in_progress'],
                'frequency': 'one_time',
                'due_date__isnull': False
            },
            description="Clean up overdue one-time tasks older than 1 month"
        ))
        
        # Old recurring task instances that are completed
        self.add_rule(TaskCleanupRule(
            model_class=Task,
            retention_days=90,  # 3 months
            date_field='completed_at',
            conditions={
                'status': 'completed',
                'is_recurring': True,
                'parent_item__isnull': False
            },
            description="Clean up completed recurring task instances older than 3 months"
        ))
        
        # Tasks with no activity for a very long time
        self.add_rule(TaskCleanupRule(
            model_class=Task,
            retention_days=365,  # 1 year
            date_field='updated_at',
            conditions={
                'status__in': ['pending', 'blocked']
            },
            description="Clean up inactive tasks with no updates for over 1 year"
        ))

        # Alertmanager bloat (2026-07 audit): mode=alert rows flooded Pulse
        # (~10k items). Resolved/cancelled alerts are disposable after a short TTL.
        resolved_days = int(getattr(settings, 'PULSE_ALERT_RESOLVED_RETENTION_DAYS', 7) or 7)
        firing_days = int(getattr(settings, 'PULSE_ALERT_FIRING_RETENTION_DAYS', 14) or 14)
        self.add_rule(TaskCleanupRule(
            model_class=Item,
            retention_days=resolved_days,
            date_field='updated_at',
            conditions={
                'mode': 'alert',
                'status__in': ['resolved', 'cancelled', 'canceled', 'completed', 'closed'],
            },
            description=f"Purge terminal mode=alert items older than {resolved_days}d",
            batch_size=2000,
        ))
        # Stale firing rows left behind when Alertmanager forgot a resolve or
        # after DM-only cutover — do not keep forever.
        self.add_rule(TaskCleanupRule(
            model_class=Item,
            retention_days=firing_days,
            date_field='updated_at',
            conditions={
                'mode': 'alert',
                'status__in': ['firing', 'acknowledged'],
            },
            description=f"Purge stale firing/acked mode=alert items older than {firing_days}d",
            batch_size=2000,
        ))
    
    def add_rule(self, rule: TaskCleanupRule):
        """Add a cleanup rule to the cleaner."""
        self.rules.append(rule)
        logger.debug(f"Added cleanup rule: {rule.description}")
    
    def remove_rule(self, model_class: models.Model):
        """Remove cleanup rules for a specific model."""
        self.rules = [rule for rule in self.rules if rule.model_class != model_class]
    
    def get_rule(self, model_class: models.Model) -> Optional[TaskCleanupRule]:
        """Get the cleanup rule for a specific model."""
        for rule in self.rules:
            if rule.model_class == model_class:
                return rule
        return None
    
    def estimate_cleanup(self) -> Dict[str, int]:
        """
        Estimate how many records would be deleted by each rule.
        
        Returns:
            Dictionary mapping rule descriptions to record counts
        """
        estimates = {}
        for rule in self.rules:
            try:
                count = rule.count_records_to_delete()
                estimates[rule.description] = count
                logger.debug(f"Cleanup estimate for '{rule.description}': {count} records")
            except Exception as e:
                logger.error(f"Error estimating cleanup for '{rule.description}': {e}")
                estimates[rule.description] = 0
        
        return estimates
    
    def execute_cleanup(self, dry_run: bool = False, specific_rules: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        Execute cleanup for all rules or specific rules.
        
        Args:
            dry_run: If True, only estimate without deleting
            specific_rules: List of rule descriptions to clean up (None for all)
            
        Returns:
            Dictionary with cleanup results
        """
        start_time = timezone.now()
        total_deleted = 0
        rule_breakdown = {}
        errors = []
        
        rules_to_execute = self.rules
        if specific_rules:
            rules_to_execute = [
                rule for rule in self.rules 
                if rule.description in specific_rules
            ]
        
        logger.info(f"Starting tasks database cleanup. Dry run: {dry_run}")
        
        for rule in rules_to_execute:
            try:
                rule_deleted, rule_detail = rule.execute_cleanup(dry_run=dry_run)
                total_deleted += rule_deleted
                rule_breakdown[rule.description] = {
                    'deleted': rule_deleted,
                    'details': rule_detail
                }
                
            except Exception as e:
                error_msg = f"Error in rule '{rule.description}': {str(e)}"
                logger.error(error_msg)
                errors.append(error_msg)
        
        end_time = timezone.now()
        duration = (end_time - start_time).total_seconds()
        
        result = {
            'service': 'shizuha-pulse',
            'dry_run': dry_run,
            'start_time': start_time.isoformat(),
            'end_time': end_time.isoformat(),
            'duration_seconds': duration,
            'total_deleted': total_deleted,
            'rule_breakdown': rule_breakdown,
            'errors': errors,
            'success': len(errors) == 0
        }
        
        if dry_run:
            logger.info(f"Tasks cleanup estimate completed in {duration:.2f}s. Would delete {total_deleted} total records.")
        else:
            logger.info(f"Tasks cleanup completed in {duration:.2f}s. Deleted {total_deleted} total records.")
        
        return result


# Global cleaner instance
task_cleaner = TaskDatabaseCleaner()


def get_task_cleaner() -> TaskDatabaseCleaner:
    """Get the global task database cleaner instance."""
    return task_cleaner


def cleanup_task_database(dry_run: bool = False, rules: Optional[List[str]] = None) -> Dict[str, Any]:
    """
    Convenience function to clean up the task database.
    
    Args:
        dry_run: If True, only estimate without deleting
        rules: List of rule descriptions to clean up (None for all)
        
    Returns:
        Dictionary with cleanup results
    """
    return get_task_cleaner().execute_cleanup(dry_run=dry_run, specific_rules=rules)


def estimate_task_cleanup() -> Dict[str, int]:
    """
    Convenience function to estimate task cleanup.
    
    Returns:
        Dictionary mapping rule descriptions to record counts
    """
    return get_task_cleaner().estimate_cleanup()
