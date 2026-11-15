"""PLS-536: tags MCP contract — string-or-array in, canonical string out.

The tool schema advertises `tags` as string OR array, but the API field is a
comma-separated TextField; raw arrays previously reached the serializer and
400'd with "Not a valid string". The approved contract (Architecture ruling on
PLS-536): accept string or array at the MCP boundary, normalize to ONE
canonical comma-separated representation before serializer validation,
identical for create and update, with field-specific errors for invalid types.

These tests pin BOTH halves so the contract cannot silently drift again
(the PLS-3 lesson):
  1. the payload actually POSTed to the API is always the canonical string,
     for every advertised input shape;
  2. the advertised signature types themselves are asserted — if someone
     widens/narrows the schema without normalization coverage, the drift
     test fails, not production callers.
"""
import os
import typing
import unittest
from unittest import mock

os.environ.setdefault("SHIZUHA_PULSE_URL", "http://localhost:8002/pulse/api")

import pulse_server  # noqa: E402


class NormalizeTagsTests(unittest.TestCase):
    def test_none_passes_through(self):
        self.assertIsNone(pulse_server._normalize_tags(None))

    def test_comma_string_canonicalized(self):
        self.assertEqual(
            pulse_server._normalize_tags(" a , b ,, c "), "a,b,c"
        )

    def test_single_string(self):
        self.assertEqual(pulse_server._normalize_tags("bug"), "bug")

    def test_array_canonicalized(self):
        self.assertEqual(
            pulse_server._normalize_tags(["frontend", " auth "]), "frontend,auth"
        )

    def test_json_array_string(self):
        self.assertEqual(
            pulse_server._normalize_tags('["a", "b"]'), "a,b"
        )

    def test_duplicates_removed_case_insensitive_first_wins(self):
        self.assertEqual(
            pulse_server._normalize_tags(["Auth", "auth", "AUTH", "ui"]), "Auth,ui"
        )

    def test_empty_string_and_empty_array_clear(self):
        self.assertEqual(pulse_server._normalize_tags(""), "")
        self.assertEqual(pulse_server._normalize_tags("   "), "")
        self.assertEqual(pulse_server._normalize_tags([]), "")

    def test_whitespace_only_elements_dropped(self):
        self.assertEqual(pulse_server._normalize_tags(["  ", "x", ""]), "x")

    def test_invalid_type_field_specific_error(self):
        with self.assertRaises(ValueError) as ctx:
            pulse_server._normalize_tags(42)
        self.assertIn("tags:", str(ctx.exception))

    def test_invalid_element_field_specific_error(self):
        with self.assertRaises(ValueError) as ctx:
            pulse_server._normalize_tags(["ok", 7])
        self.assertIn("tags:", str(ctx.exception))
        self.assertIn("int", str(ctx.exception))


class _PayloadCaptureMixin:
    """Drive the real tool functions with api_request mocked; capture payloads."""

    def _create(self, **kwargs):
        posted = {}

        def fake_api(method, path, data=None, **kw):
            if method == "POST" and path.rstrip("/").endswith("items"):
                posted.update(data or {})
                return {"item_key": "PLS-1", "id": 1}
            return {}

        with mock.patch.object(pulse_server, "api_request", side_effect=fake_api), \
             mock.patch.object(pulse_server, "_find_existing_task_candidates", return_value=[]), \
             mock.patch.object(pulse_server, "_resolve_default_project_id", return_value=1, create=True):
            result = pulse_server.create_task(
                title="PLS-536 contract probe", description="x",
                project_id=1, workflow="simple", allow_duplicate=True, **kwargs
            )
        return result, posted

    def _update(self, **kwargs):
        patched = {}

        def fake_api(method, path, data=None, **kw):
            if method in ("PATCH", "PUT"):
                patched.update(data or {})
                return {"item_key": "PLS-1"}
            return {"id": 1, "item_key": "PLS-1"}

        with mock.patch.object(pulse_server, "api_request", side_effect=fake_api), \
             mock.patch.object(pulse_server, "_resolve_item_pk", return_value=1):
            result = pulse_server.update_task("PLS-1", **kwargs)
        return result, patched


class CreateTaskTagsContractTests(_PayloadCaptureMixin, unittest.TestCase):
    def test_array_input_posts_canonical_string(self):
        _, posted = self._create(tags=["retro", " mcp "])
        self.assertEqual(posted.get("tags"), "retro,mcp")

    def test_comma_string_posts_canonical_string(self):
        _, posted = self._create(tags="retro, mcp")
        self.assertEqual(posted.get("tags"), "retro,mcp")

    def test_invalid_type_returns_field_error_and_does_not_post(self):
        result, posted = self._create(tags=123)
        self.assertIn("Error: tags:", str(result))
        self.assertNotIn("tags", posted)

    def test_no_tags_omits_field(self):
        _, posted = self._create()
        self.assertNotIn("tags", posted)


class UpdateTaskTagsContractTests(_PayloadCaptureMixin, unittest.TestCase):
    def test_array_input_patches_canonical_string(self):
        _, patched = self._update(tags=["a", "b", "a"])
        self.assertEqual(patched.get("tags"), "a,b")

    def test_empty_array_clears(self):
        _, patched = self._update(tags=[])
        self.assertEqual(patched.get("tags"), "")

    def test_invalid_element_returns_field_error(self):
        result, patched = self._update(tags=["ok", None])
        self.assertIn("Error: tags:", str(result))
        self.assertNotIn("tags", patched)


class SchemaDriftTests(unittest.TestCase):
    """The generated tool schema is derived from these signatures. Pin the
    advertised types to the set the normalization boundary actually covers —
    a future widening (or narrowing back to plain `str`, the original PLS-3
    regression) fails HERE instead of on live callers."""

    ADVERTISED = typing.Optional[typing.Union[str, typing.List[str]]]

    def _assert_tags_contract(self, func):
        hints = typing.get_type_hints(func)
        self.assertEqual(
            hints.get("tags"), self.ADVERTISED,
            f"{func.__name__} advertises a tags type the normalization "
            "boundary does not cover — update _normalize_tags AND these "
            "contract tests together.",
        )

    def test_create_task_signature(self):
        self._assert_tags_contract(pulse_server.create_task)

    def test_update_task_signature(self):
        self._assert_tags_contract(pulse_server.update_task)

    def test_every_advertised_shape_normalizes_to_str(self):
        for sample in ("a,b", ["a", "b"], '["a"]', "", []):
            out = pulse_server._normalize_tags(sample)
            self.assertIsInstance(out, str, f"shape {sample!r} must canonicalize to str")


if __name__ == "__main__":
    unittest.main()
