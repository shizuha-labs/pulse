"""
Management command to setup default organizations for existing users.

This command:
1. Creates a personal organization for each existing user who doesn't have one
2. Adds the user as owner of their personal organization
"""
from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from django.utils import timezone
from tasks.models import Organization, OrganizationMembership


class Command(BaseCommand):
    help = 'Setup default personal organizations for existing users'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be done without making changes',
        )
        parser.add_argument(
            '--user-id',
            type=int,
            help='Only setup organization for a specific user ID',
        )

    def handle(self, *args, **options):
        User = get_user_model()
        dry_run = options['dry_run']
        user_id = options.get('user_id')

        if dry_run:
            self.stdout.write(self.style.WARNING('DRY RUN - No changes will be made'))

        # Get users to process
        users = User.objects.all()
        if user_id:
            users = users.filter(id=user_id)

        created_count = 0
        skipped_count = 0

        for user in users:
            # Check if user already has a personal organization
            personal_slug = f"personal-{user.id}"
            existing_org = Organization.objects.filter(slug=personal_slug).first()

            if existing_org:
                self.stdout.write(
                    f"  Skipping user {user.id} ({user.username}) - "
                    f"already has personal org: {existing_org.slug}"
                )
                skipped_count += 1
                continue

            # Also check if user owns any organization
            membership = OrganizationMembership.objects.filter(
                user_id=user.id,
                role='owner',
                is_active=True
            ).first()

            if membership:
                self.stdout.write(
                    f"  Skipping user {user.id} ({user.username}) - "
                    f"already owns org: {membership.organization.slug}"
                )
                skipped_count += 1
                continue

            # Create personal organization
            org_name = f"{user.username}'s Workspace"
            if hasattr(user, 'first_name') and user.first_name:
                org_name = f"{user.first_name}'s Workspace"

            if dry_run:
                self.stdout.write(
                    f"  Would create org '{org_name}' (slug: {personal_slug}) "
                    f"for user {user.id} ({user.username})"
                )
            else:
                org = Organization.objects.create(
                    name=org_name,
                    slug=personal_slug,
                    description='Personal workspace',
                    owner_id=user.id,
                    plan='free',
                    is_active=True
                )

                # Add user as owner
                OrganizationMembership.objects.create(
                    organization=org,
                    user_id=user.id,
                    user_email=user.email,
                    role='owner',
                    is_pending=False,
                    accepted_at=timezone.now(),
                    is_active=True
                )

                self.stdout.write(
                    self.style.SUCCESS(
                        f"  Created org '{org.name}' (slug: {org.slug}) "
                        f"for user {user.id} ({user.username})"
                    )
                )

            created_count += 1

        # Summary
        self.stdout.write('')
        if dry_run:
            self.stdout.write(self.style.WARNING(
                f"DRY RUN COMPLETE: Would create {created_count} organizations, "
                f"skipped {skipped_count} users"
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                f"COMPLETE: Created {created_count} organizations, "
                f"skipped {skipped_count} users"
            ))
