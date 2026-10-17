"""
Django management command for task database cleanup.

Usage:
    python manage.py cleanup_task_database
    python manage.py cleanup_task_database --dry-run
    python manage.py cleanup_task_database --rules "Clean up completed tasks older than 6 months"
    python manage.py cleanup_task_database --estimate-only
"""

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
import json
import logging

from tasks.cleanup import get_task_cleaner, cleanup_task_database, estimate_task_cleanup


logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Clean up old task data from the database according to retention policies'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be deleted without actually deleting',
        )
        parser.add_argument(
            '--estimate-only',
            action='store_true',
            help='Only show estimates of records to be deleted',
        )
        parser.add_argument(
            '--rules',
            nargs='+',
            help='Specific cleanup rules to execute (by description)',
        )
        parser.add_argument(
            '--verbose',
            action='store_true',
            help='Enable verbose output',
        )
        parser.add_argument(
            '--json',
            action='store_true',
            help='Output results in JSON format',
        )

    def handle(self, *args, **options):
        # Configure logging level
        if options['verbose']:
            logging.getLogger('tasks.cleanup').setLevel(logging.DEBUG)
        
        try:
            if options['estimate_only']:
                self.handle_estimate_only(options)
            else:
                self.handle_cleanup(options)
                
        except Exception as e:
            logger.exception("Error during task database cleanup")
            raise CommandError(f"Cleanup failed: {str(e)}")

    def handle_estimate_only(self, options):
        """Handle estimate-only mode."""
        self.stdout.write("Estimating task database cleanup...")
        
        estimates = estimate_task_cleanup()
        total_estimate = sum(estimates.values())
        
        if options['json']:
            result = {
                'service': 'shizuha-pulse',
                'mode': 'estimate',
                'timestamp': timezone.now().isoformat(),
                'total_records': total_estimate,
                'rule_breakdown': estimates
            }
            self.stdout.write(json.dumps(result, indent=2))
        else:
            self.stdout.write(f"\nTask Database Cleanup Estimates:")
            self.stdout.write("=" * 60)
            
            if not estimates:
                self.stdout.write("No records to clean up.")
                return
            
            for rule_description, count in estimates.items():
                if count > 0:
                    self.stdout.write(f"{count:,} records: {rule_description}")
            
            self.stdout.write("-" * 60)
            self.stdout.write(f"Total estimated records: {total_estimate:,}")

    def handle_cleanup(self, options):
        """Handle actual cleanup or dry-run mode."""
        dry_run = options['dry_run']
        rules = options.get('rules')
        
        mode_text = "DRY RUN" if dry_run else "CLEANUP"
        self.stdout.write(f"Starting task database {mode_text}...")
        
        if rules:
            self.stdout.write(f"Target rules: {', '.join(rules)}")
        
        # Execute cleanup
        result = cleanup_task_database(dry_run=dry_run, rules=rules)
        
        if options['json']:
            self.stdout.write(json.dumps(result, indent=2))
        else:
            self.format_cleanup_results(result, dry_run)

    def format_cleanup_results(self, result, dry_run):
        """Format and display cleanup results in human-readable format."""
        mode_text = "DRY RUN RESULTS" if dry_run else "CLEANUP RESULTS"
        
        self.stdout.write(f"\n{mode_text}")
        self.stdout.write("=" * 60)
        
        # Summary
        duration = result['duration_seconds']
        total = result['total_deleted']
        action_text = "Would delete" if dry_run else "Deleted"
        
        self.stdout.write(f"Duration: {duration:.2f} seconds")
        self.stdout.write(f"Total records {action_text.lower()}: {total:,}")
        
        # Rule breakdown
        if result['rule_breakdown']:
            self.stdout.write(f"\nRule Breakdown:")
            self.stdout.write("-" * 40)
            
            for rule_desc, rule_result in result['rule_breakdown'].items():
                count = rule_result['deleted']
                if count > 0:
                    self.stdout.write(f"{count:,} records: {rule_desc}")
        
        # Errors
        if result['errors']:
            self.stdout.write(f"\nErrors:")
            self.stdout.write("-" * 30)
            for error in result['errors']:
                self.stdout.write(self.style.ERROR(error))
        
        # Success message
        if result['success']:
            if dry_run:
                self.stdout.write(self.style.SUCCESS(f"\nDry run completed successfully!"))
            else:
                self.stdout.write(self.style.SUCCESS(f"\nCleanup completed successfully!"))
        else:
            self.stdout.write(self.style.ERROR(f"\nCleanup completed with errors."))
        
        # Show available rules
        if not result['rule_breakdown']:
            self.stdout.write("\nNo records to clean up. Available cleanup rules:")
            cleaner = get_task_cleaner()
            for rule in cleaner.rules:
                self.stdout.write(f"  - {rule.description}")
                self.stdout.write(f"    Retention: {rule.retention_days} days")
                if rule.conditions:
                    self.stdout.write(f"    Conditions: {rule.conditions}")
