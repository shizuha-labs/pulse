"""A package of tasks is refused. One unit of work is a task."""

from django.test import SimpleTestCase

from tasks.services.package_guard import package_of_tasks_reason


class PackageGuardTests(SimpleTestCase):
    def test_sitting_title_is_a_package(self):
        reason = package_of_tasks_reason(
            "Operator sitting: one-package unblock for the fleet incident",
            "Do these in order.",
        )
        self.assertIn("one unit at a time", reason)

    def test_item_range_title_is_a_package(self):
        reason = package_of_tasks_reason(
            "Execute the console sitting: items 40-53 in order",
            "",
        )
        self.assertIn("one unit at a time", reason)

    def test_catalog_of_other_tasks_is_a_package(self):
        description = (
            "### 1. Repair the roster (PLAT-8207)\n"
            "### 2. Read the alert (CTX-923)\n"
            "### 3. Mint the token (PLS-1211)\n"
        )
        reason = package_of_tasks_reason("Cluster unblock", description)
        self.assertIn("one unit at a time", reason)

    def test_steps_of_one_job_are_not_a_package(self):
        description = (
            "### 1. Provision the Id account\n"
            "### 2. Verify identity\n"
            "### 3. Reconcile service grants\n"
        )
        self.assertEqual(
            package_of_tasks_reason("Provision Grok Engineer", description),
            "",
        )

    def test_two_asks_joined_in_the_title_are_a_package(self):
        reason = package_of_tasks_reason(
            "Fire Accept on PLAT-5977 + PLAT-5877",
            "",
        )
        self.assertIn("one unit at a time", reason)

    def test_one_task_that_mentions_another_key_is_not_a_package(self):
        self.assertEqual(
            package_of_tasks_reason(
                "Close PLAT-5798 once the grant is live",
                "See PLAT-9840 for the repair note.",
            ),
            "",
        )
