"""
Celery tasks for automatic task database cleanup.

These tasks handle periodic cleanup of old task data from the database.
"""

import logging
from datetime import datetime
from typing import Dict, Any, List, Optional

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from tasks.cleanup import cleanup_task_database, estimate_task_cleanup


logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=300)
def periodic_task_database_cleanup(self, dry_run: bool = False, rules: Optional[List[str]] = None) -> Dict[str, Any]:
    """
    Periodic task to clean up old task data from the database.
    
    Args:
        dry_run: If True, only estimate without deleting
        rules: List of rule descriptions to clean up (None for all)
        
    Returns:
        Dictionary with cleanup results
    """
    task_id = self.request.id
    logger.info(f"Starting periodic task database cleanup {task_id}. Dry run: {dry_run}")
    
    try:
        result = cleanup_task_database(dry_run=dry_run, rules=rules)
        result['task_id'] = task_id
        
        # Log summary
        total_deleted = result['total_deleted']
        duration = result['duration_seconds']
        
        if dry_run:
            logger.info(f"Task cleanup estimate {task_id} completed. Would delete {total_deleted} records in {duration:.2f}s")
        else:
            logger.info(f"Task cleanup {task_id} completed. Deleted {total_deleted} records in {duration:.2f}s")
        
        # Log any errors
        if result['errors']:
            for error in result['errors']:
                logger.error(f"Task cleanup {task_id} error: {error}")
        
        return result
        
    except Exception as exc:
        logger.error(f"Task cleanup {task_id} failed: {str(exc)}")
        
        # Retry on failure
        if self.request.retries < self.max_retries:
            logger.info(f"Retrying task cleanup {task_id} in {self.default_retry_delay} seconds")
            raise self.retry(exc=exc)
        else:
            logger.error(f"Task cleanup {task_id} failed after {self.max_retries} retries")
            raise


@shared_task
def estimate_task_database_cleanup() -> Dict[str, int]:
    """
    Task to estimate how many task records would be cleaned up.
    
    Returns:
        Dictionary mapping rule descriptions to record counts
    """
    logger.info("Starting task database cleanup estimation")
    
    try:
        estimates = estimate_task_cleanup()
        total_estimate = sum(estimates.values())
        
        logger.info(f"Task database cleanup estimation completed. Total records: {total_estimate}")
        
        return estimates
        
    except Exception as exc:
        logger.error(f"Task database cleanup estimation failed: {str(exc)}")
        raise


@shared_task
def weekly_task_database_cleanup() -> Dict[str, Any]:
    """
    Weekly cleanup task that runs the full cleanup.
    
    Returns:
        Dictionary with cleanup results
    """
    logger.info("Starting weekly task database cleanup")
    
    return periodic_task_database_cleanup.delay(dry_run=False).get()


@shared_task
def daily_task_cleanup_estimate() -> Dict[str, int]:
    """
    Daily task to estimate cleanup and log the results.
    
    Returns:
        Dictionary mapping rule descriptions to record counts
    """
    logger.info("Starting daily task cleanup estimate")
    
    estimates = estimate_task_database_cleanup.delay().get()
    total_estimate = sum(estimates.values())
    
    if total_estimate > 0:
        logger.info(f"Task cleanup estimate: {total_estimate} total records ready for cleanup")
        for rule_desc, count in estimates.items():
            if count > 0:
                logger.info(f"  {rule_desc}: {count} records")
    else:
        logger.debug("No task records ready for cleanup")
    
    return estimates


@shared_task
def cleanup_completed_tasks() -> Dict[str, Any]:
    """
    Specifically clean up completed tasks older than retention period.
    
    Returns:
        Dictionary with cleanup results
    """
    logger.info("Starting cleanup of completed tasks")
    
    rules = ["Clean up completed tasks older than 6 months"]
    return periodic_task_database_cleanup.delay(dry_run=False, rules=rules).get()


@shared_task
def cleanup_cancelled_tasks() -> Dict[str, Any]:
    """
    Specifically clean up cancelled tasks older than retention period.
    
    Returns:
        Dictionary with cleanup results
    """
    logger.info("Starting cleanup of cancelled tasks")
    
    rules = ["Clean up cancelled tasks older than 3 months"]
    return periodic_task_database_cleanup.delay(dry_run=False, rules=rules).get()


# Task scheduling configuration for django-celery-beat
def setup_task_cleanup_schedule():
    """
    Setup periodic task schedule for task database cleanup.
    This function should be called from apps.py ready() method.
    """
    try:
        from django_celery_beat.models import PeriodicTask, CrontabSchedule
        
        # Weekly cleanup on Sunday at 3 AM
        weekly_schedule, created = CrontabSchedule.objects.get_or_create(
            minute=0,
            hour=3,
            day_of_week=0,  # Sunday
            day_of_month='*',
            month_of_year='*',
        )
        
        PeriodicTask.objects.get_or_create(
            name='Weekly Task Database Cleanup',
            defaults={
                'crontab': weekly_schedule,
                'task': 'tasks.cleanup_tasks.weekly_task_database_cleanup',
                'enabled': True,
            }
        )
        
        # Daily cleanup estimate at 6 AM
        daily_schedule, created = CrontabSchedule.objects.get_or_create(
            minute=0,
            hour=6,
            day_of_week='*',
            day_of_month='*',
            month_of_year='*',
        )
        
        PeriodicTask.objects.get_or_create(
            name='Daily Task Cleanup Estimate',
            defaults={
                'crontab': daily_schedule,
                'task': 'tasks.cleanup_tasks.daily_task_cleanup_estimate',
                'enabled': True,
            }
        )
        
        # Monthly cleanup of completed tasks (1st of month at 4 AM)
        monthly_schedule, created = CrontabSchedule.objects.get_or_create(
            minute=0,
            hour=4,
            day_of_week='*',
            day_of_month=1,
            month_of_year='*',
        )
        
        PeriodicTask.objects.get_or_create(
            name='Monthly Completed Tasks Cleanup',
            defaults={
                'crontab': monthly_schedule,
                'task': 'tasks.cleanup_tasks.cleanup_completed_tasks',
                'enabled': True,
            }
        )
        
        logger.info("Periodic task cleanup schedule configured")
        
    except Exception as e:
        logger.warning(f"Could not setup task cleanup schedule: {e}")


# Utility functions for manual triggering
def trigger_task_cleanup(dry_run: bool = False, rules: Optional[List[str]] = None) -> str:
    """
    Manually trigger a task cleanup.
    
    Args:
        dry_run: If True, only estimate without deleting
        rules: List of rule descriptions to clean up
        
    Returns:
        Task ID
    """
    task = periodic_task_database_cleanup.delay(dry_run=dry_run, rules=rules)
    logger.info(f"Triggered task cleanup {task.id}. Dry run: {dry_run}")
    return task.id


def trigger_specific_cleanup(cleanup_type: str) -> str:
    """
    Manually trigger specific cleanup types.
    
    Args:
        cleanup_type: 'completed' or 'cancelled'
        
    Returns:
        Task ID
    """
    if cleanup_type == 'completed':
        task = cleanup_completed_tasks.delay()
    elif cleanup_type == 'cancelled':
        task = cleanup_cancelled_tasks.delay()
    else:
        raise ValueError(f"Unknown cleanup type: {cleanup_type}")
    
    logger.info(f"Triggered {cleanup_type} tasks cleanup {task.id}")
    return task.id
