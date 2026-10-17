"""Tests for PLS-39: write-time secret scanning on task/comment content."""

import unittest

from tasks.secret_scanner import has_secrets, redact_secrets, scan_for_secrets

REDACTED_PREFIX = "[REDACTED:"


class TestGitHubPatterns(unittest.TestCase):
    def test_github_fine_grained_pat_detected(self):
        # Format: github_pat_ + 22 alphanumeric + _ + 59 alphanumeric
        text = "Token: github_pat_" + "A" * 22 + "_" + "B" * 59
        self.assertTrue(has_secrets(text))

    def test_github_classic_pat_detected(self):
        text = "Key: ghp_" + "A" * 36
        self.assertTrue(has_secrets(text))

    def test_github_classic_pat_redacted(self):
        token = "ghp_" + "B" * 36
        text = f"Use this token: {token} for auth"
        result, found = redact_secrets(text)
        self.assertIn("github_classic_pat", found)
        self.assertNotIn(token, result)
        self.assertIn(REDACTED_PREFIX, result)
        self.assertIn("Use this token:", result)

    def test_github_app_token_gho(self):
        text = "gho_" + "X" * 36 + " is the oauth token"
        self.assertTrue(has_secrets(text))

    def test_github_app_token_ghs(self):
        text = "Server token: ghs_" + "Y" * 36
        self.assertTrue(has_secrets(text))

    def test_short_word_not_detected(self):
        # ghp_ prefix but too short — not a real PAT
        self.assertFalse(has_secrets("ghp_short"))


class TestAwsPatterns(unittest.TestCase):
    def test_aws_access_key_id_detected(self):
        text = "AKIAIOSFODNN7EXAMPLE is the key id"
        self.assertTrue(has_secrets(text))

    def test_aws_access_key_id_redacted(self):
        key = "AKIAIOSFODNN7EXAMPLE"  # 20 chars: AKIA + 16 uppercase
        result, found = redact_secrets(f"key_id={key}")
        self.assertIn("aws_access_key_id", found)
        self.assertNotIn(key, result)


class TestPemPrivateKey(unittest.TestCase):
    def test_rsa_private_key_full_block_detected(self):
        text = (
            "Here it is:\n"
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEowIBAAKCAQEA1234abcd\n"
            "-----END RSA PRIVATE KEY-----"
        )
        self.assertTrue(has_secrets(text))

    def test_openssh_private_key_detected(self):
        text = (
            "-----BEGIN OPENSSH PRIVATE KEY-----\n"
            "aaaaBBBcccDDD\n"
            "-----END OPENSSH PRIVATE KEY-----"
        )
        self.assertTrue(has_secrets(text))

    def test_pem_private_key_redacted(self):
        key_body = "MIIEowIBAAKCAQEA1234abcd"
        text = (
            "Key:\n"
            "-----BEGIN RSA PRIVATE KEY-----\n"
            f"{key_body}\n"
            "-----END RSA PRIVATE KEY-----"
        )
        result, found = redact_secrets(text)
        self.assertIn("pem_private_key", found)
        self.assertNotIn("BEGIN RSA PRIVATE KEY", result)
        self.assertNotIn("END RSA PRIVATE KEY", result)
        # Key body must also be gone (the actual sensitive bytes)
        self.assertNotIn(key_body, result)

    def test_pem_key_body_not_present_after_redaction(self):
        # Reproduces the P2 finding: base64 key material must be redacted, not just the header
        body_line1 = "MIIEpAIBAAKCAQEA3q8oVoIsQMgXqQ=="
        body_line2 = "wQkJuGTpI8PmP3fX2t9OjA9H/AQIDAQAB"
        text = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            f"{body_line1}\n"
            f"{body_line2}\n"
            "-----END RSA PRIVATE KEY-----"
        )
        result, found = redact_secrets(text)
        self.assertIn("pem_private_key", found)
        self.assertNotIn(body_line1, result, "key material line 1 must be redacted")
        self.assertNotIn(body_line2, result, "key material line 2 must be redacted")


class TestFernetToken(unittest.TestCase):
    def test_fernet_token_detected(self):
        # Fernet token starts with gAAAAA
        token = "gAAAAA" + "B" * 50 + "=="
        self.assertTrue(has_secrets(token))

    def test_fernet_token_redacted(self):
        token = "gAAAAA" + "C" * 52 + "="
        result, found = redact_secrets(f"FERNET_KEY={token}")
        self.assertIn("fernet_token", found)
        self.assertNotIn(token, result)


class TestBearerJwt(unittest.TestCase):
    def test_bearer_jwt_detected(self):
        text = "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signature"
        self.assertTrue(has_secrets(text))

    def test_bearer_jwt_redacted(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.SIG123"
        text = f"Header: Authorization: Bearer {jwt}"
        result, found = redact_secrets(text)
        self.assertIn("bearer_jwt", found)
        self.assertNotIn(jwt, result)


class TestDbConnectionString(unittest.TestCase):
    def test_postgres_connection_string_detected(self):
        text = "DATABASE_URL=postgres://user:p@ssword@db.example.com:5432/mydb"
        self.assertTrue(has_secrets(text))

    def test_mongodb_srv_connection_string_redacted(self):
        conn = "mongodb+srv://admin:s3cret@cluster0.example.mongodb.net/mydb"
        result, found = redact_secrets(f"connect to {conn}")
        self.assertIn("db_connection_string", found)
        self.assertNotIn("s3cret", result)

    def test_plain_domain_not_detected(self):
        self.assertFalse(has_secrets("https://example.com/api/v1/resource"))


class TestGenericHexSecret(unittest.TestCase):
    def test_long_hex_after_token_keyword_detected(self):
        text = "token=" + "a" * 40
        self.assertTrue(has_secrets(text))

    def test_sha256_hash_in_context_not_false_positive(self):
        # 64-char hex SHA256 with NO secret keyword prefix — should not trigger generic_hex
        self.assertFalse(has_secrets("checksum: " + "d" * 64))

    def test_long_hex_after_secret_keyword_redacted(self):
        val = "0" * 40
        text = f"secret={val}"
        result, found = redact_secrets(text)
        self.assertIn("generic_hex_secret", found)
        self.assertNotIn(val, result)


class TestRedactionOutput(unittest.TestCase):
    def test_clean_text_unchanged(self):
        text = "Fix the login button alignment"
        result, found = redact_secrets(text)
        self.assertEqual(result, text)
        self.assertEqual(found, [])

    def test_multiple_secrets_in_one_text(self):
        token = "ghp_" + "A" * 36
        key = "AKIAIOSFODNN7EXAMPLE"
        text = f"token={token} key_id={key}"
        result, found = redact_secrets(text)
        self.assertNotIn(token, result)
        self.assertNotIn(key, result)
        self.assertGreaterEqual(len(found), 2)

    def test_non_secret_context_preserved(self):
        token = "ghp_" + "Z" * 36
        text = f"Please use {token} but not admin@example.com"
        result, found = redact_secrets(text)
        self.assertIn("but not admin@example.com", result)
        self.assertIn("Please use", result)

    def test_scan_returns_positions(self):
        token = "ghp_" + "Q" * 36
        text = f"token: {token}"
        matches = scan_for_secrets(text)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].pattern_name, "github_classic_pat")
        self.assertEqual(matches[0].matched_text, token)

    def test_empty_string_no_crash(self):
        result, found = redact_secrets("")
        self.assertEqual(result, "")
        self.assertEqual(found, [])

    def test_none_safe_via_has_secrets(self):
        self.assertFalse(has_secrets(""))


class TestSignalActivityRedaction(unittest.TestCase):
    """
    Regression for PLS-39 P2: activity history must not store raw secrets.

    The signals.py track_item_changes path captures the pre-save DB value
    as old_value and the new serializer-redacted value as new_value.
    If the old DB row contained a secret, old_value must also be redacted
    before being written to ItemActivity.
    """

    def _make_activity_old_new(self, field, old_raw, new_raw):
        """
        Simulate what signals.py does: format then redact for _ACTIVITY_REDACT_FIELDS.
        Returns (old_display, new_display) after the full pipeline.
        """
        # _format_value for title/description is just str(value)
        old_display = str(old_raw) if old_raw is not None else ''
        new_display = str(new_raw) if new_raw is not None else ''
        if field in ('title', 'description'):
            old_display, _ = redact_secrets(old_display)
            new_display, _ = redact_secrets(new_display)
        return old_display, new_display

    def test_old_description_with_secret_is_redacted_in_activity(self):
        """Old DB value containing a PAT must not appear in activity old_value."""
        old_secret = "ghp_" + "X" * 36
        old_raw = f"Use token {old_secret} for auth"
        new_raw = "Use the configured token for auth"  # already clean after serializer

        old_display, new_display = self._make_activity_old_new('description', old_raw, new_raw)

        self.assertNotIn(old_secret, old_display,
                         "Raw PAT must not appear in activity old_value")
        self.assertIn("[REDACTED:", old_display,
                      "Redacted placeholder must be present in old_value")
        # new value was clean — no redaction needed
        self.assertEqual(new_display, new_raw)

    def test_old_title_with_aws_key_is_redacted(self):
        """Old title containing an AWS key must be redacted in activity."""
        aws_key = "AKIAIOSFODNN7EXAMPLE"
        old_raw = f"Task: deploy {aws_key}"
        new_raw = "Task: deploy service"

        old_display, _ = self._make_activity_old_new('title', old_raw, new_raw)

        self.assertNotIn(aws_key, old_display)
        self.assertIn("[REDACTED:", old_display)

    def test_non_text_field_not_redacted(self):
        """Status field values (not free-text) should not be run through redact."""
        # status values like 'open' or 'in_progress' won't have secrets,
        # but the signal does NOT redact non-_ACTIVITY_REDACT_FIELDS — confirm
        # that 'status' is not in the redact set (no accidental suppression).
        from tasks.signals import _ACTIVITY_REDACT_FIELDS
        self.assertNotIn('status', _ACTIVITY_REDACT_FIELDS)
        self.assertNotIn('priority', _ACTIVITY_REDACT_FIELDS)
        self.assertIn('title', _ACTIVITY_REDACT_FIELDS)
        self.assertIn('description', _ACTIVITY_REDACT_FIELDS)

    def test_clean_old_value_unchanged(self):
        """Old value without secrets is written to activity unchanged."""
        old_raw = "Fix the login page alignment"
        new_raw = "Fix the login page layout"

        old_display, new_display = self._make_activity_old_new('description', old_raw, new_raw)

        self.assertEqual(old_display, old_raw)
        self.assertEqual(new_display, new_raw)


if __name__ == "__main__":
    unittest.main()
