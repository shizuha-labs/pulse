"""PLAT-10642: dispatch-time git write-scope preflight."""
from django.test import SimpleTestCase

from tasks.services.git_write_scope_preflight import (
    GitWritePreflightError,
    preflight_git_write_scope,
    seat_can_deliver,
)


BASE = {"repo": "shizuha-labs/deploy", "surface": "base"}
FORK = {"repo": "shizuha-labs/deploy", "surface": "fork", "fork": "saki/deploy"}

CATALOG = [
    {
        "email": "cos@shizuha.com",
        "reachable": True,
        "base_write": [],
        "fork_write": [],
    },
    {
        "email": "jun@shizuha.com",
        "reachable": True,
        "base_write": ["shizuha-labs/deploy"],
        "fork_write": [],
    },
    {
        "email": "saki@shizuha.com",
        "reachable": True,
        "base_write": ["shizuha-labs/hive"],
        "fork_write": ["saki/deploy"],
    },
]


class GitWriteScopePreflightTests(SimpleTestCase):
    def test_read_scoped_seat_cannot_deliver_base(self):
        self.assertFalse(seat_can_deliver(CATALOG[0], BASE))

    def test_keeps_proposed_seat_when_it_has_base_write(self):
        self.assertEqual(
            preflight_git_write_scope(BASE, CATALOG, "jun@shizuha.com"),
            "jun@shizuha.com",
        )

    def test_routes_away_from_read_scoped_proposed_seat(self):
        self.assertEqual(
            preflight_git_write_scope(BASE, CATALOG, "cos@shizuha.com"),
            "jun@shizuha.com",
        )

    def test_fork_write_does_not_satisfy_base(self):
        self.assertFalse(seat_can_deliver(CATALOG[2], BASE))

    def test_routes_fork_delivery_to_fork_owner(self):
        self.assertEqual(
            preflight_git_write_scope(FORK, CATALOG, "jun@shizuha.com"),
            "saki@shizuha.com",
        )

    def test_unreachable_writer_is_not_a_route(self):
        catalog = [
            {**CATALOG[1], "reachable": False},
            CATALOG[0],
        ]
        with self.assertRaises(GitWritePreflightError) as caught:
            preflight_git_write_scope(BASE, catalog, "cos@shizuha.com")
        self.assertEqual(caught.exception.reason, "no_proven_writer")
        self.assertIn("parked", caught.exception.note)

    def test_empty_catalog_parks(self):
        with self.assertRaises(GitWritePreflightError) as caught:
            preflight_git_write_scope(BASE, [], "cos@shizuha.com")
        self.assertIn("checked: (none)", caught.exception.note)

    def test_missing_fork_parks(self):
        with self.assertRaises(GitWritePreflightError) as caught:
            preflight_git_write_scope(
                {"repo": "shizuha-labs/deploy", "surface": "fork"},
                CATALOG,
                "saki@shizuha.com",
            )
        self.assertEqual(caught.exception.reason, "missing_fork")
