"""
Write-time secret scanning and redaction for Pulse task/comment content (PLS-39).

Detects high-confidence credential patterns and replaces them with a
[REDACTED:<type>] placeholder BEFORE the content is persisted to the DB.
This prevents secrets from being stored in plaintext, broadcast to readers,
or retained in edit/version history.

Coverage (ordered by specificity):
  - GitHub fine-grained PAT       github_pat_<86-base62>
  - GitHub classic PAT            ghp_<36-base62>
  - GitHub OAuth/server token     gho_ / ghs_ / ghr_ / ghu_
  - AWS access key ID             AKIA[A-Z0-9]{16}
  - AWS secret access key         40 base64 chars following known context
  - Private-key PEM block         -----BEGIN ... PRIVATE KEY-----
  - Fernet token                  gAAAAA... (base64url, 52+ chars, starts with gAAAAA)
  - Generic bearer / JWT          Authorization header / Bearer <token>
  - DB connection string          postgres:// / mysql:// / mongodb:// with credentials
  - Generic high-entropy token    >40 char hex strings after =, :, or whitespace

False-positive guardrails:
  - All patterns require anchoring (begin-of-value characters, prefixes, or headers)
    so prose text with random similar characters does not trigger.
  - Hex pattern excludes common lowercase words / SHA refs < 40 chars.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _Pattern:
    name: str
    regex: re.Pattern[str]
    description: str


_PATTERNS: list[_Pattern] = [
    _Pattern(
        name="github_fine_grained_pat",
        regex=re.compile(r'\bgithub_pat_[A-Za-z0-9]{22}_[A-Za-z0-9]{59}\b'),
        description="GitHub fine-grained personal access token",
    ),
    _Pattern(
        name="github_classic_pat",
        regex=re.compile(r'\bghp_[A-Za-z0-9]{36}\b'),
        description="GitHub classic personal access token",
    ),
    _Pattern(
        name="github_app_token",
        regex=re.compile(r'\b(gho|ghs|ghr|ghu)_[A-Za-z0-9]{36}\b'),
        description="GitHub OAuth / server-to-server / installation / user token",
    ),
    _Pattern(
        name="aws_access_key_id",
        regex=re.compile(r'\bAKIA[A-Z2-7]{16}\b'),
        description="AWS access key ID",
    ),
    _Pattern(
        name="aws_secret_access_key",
        regex=re.compile(
            r'(?i)(?:aws.?secret.?access.?key|aws.?secret|secret.?key)'
            r'[^\n]{0,30}?[=:"\s][^\n"]{0,8}([A-Za-z0-9+/]{40})\b'
        ),
        description="AWS secret access key",
    ),
    _Pattern(
        name="pem_private_key",
        regex=re.compile(
            r'-----BEGIN\s+(?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----'
            r'[\s\S]*?'
            r'-----END\s+(?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----',
            re.IGNORECASE,
        ),
        description="PEM-encoded private key block",
    ),
    _Pattern(
        name="fernet_token",
        regex=re.compile(r'\bgAAAAA[A-Za-z0-9\-_]{45,}={0,2}\b'),
        description="Fernet encrypted token",
    ),
    _Pattern(
        name="bearer_jwt",
        regex=re.compile(
            r'(?i)(?:bearer\s+|authorization:\s*bearer\s+)'
            r'([A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]+)',
        ),
        description="Bearer / JWT token in Authorization context",
    ),
    _Pattern(
        name="db_connection_string",
        regex=re.compile(
            r'(?i)(?:postgres|postgresql|mysql|mongodb(?:\+srv)?|redis|amqp)'
            r'://[^:@\s]+:[^@\s]+@[^\s"\'<>]+'
        ),
        description="Database connection string with embedded credentials",
    ),
    _Pattern(
        name="generic_hex_secret",
        regex=re.compile(
            r'(?<![A-Za-z0-9])(?:token|secret|key|password|passwd|api_key|apikey|auth)'
            r"[\s=:\"']{1,4}([0-9a-fA-F]{40,})\b",
            re.IGNORECASE,
        ),
        description="Generic high-entropy hex secret",
    ),
]

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

REDACTION_PLACEHOLDER = "[REDACTED:{name}]"


@dataclass(frozen=True)
class SecretMatch:
    pattern_name: str
    matched_text: str
    start: int
    end: int


def scan_for_secrets(text: str) -> list[SecretMatch]:
    """Return all secret matches found in *text*, ordered by position."""
    if not text:
        return []
    matches: list[SecretMatch] = []
    for pattern in _PATTERNS:
        for m in pattern.regex.finditer(text):
            matches.append(SecretMatch(
                pattern_name=pattern.name,
                matched_text=m.group(0),
                start=m.start(),
                end=m.end(),
            ))
    matches.sort(key=lambda m: m.start)
    return matches


def has_secrets(text: str) -> bool:
    """Return True if *text* contains any detectable secret pattern."""
    if not text:
        return False
    return any(p.regex.search(text) for p in _PATTERNS)


def redact_secrets(text: str) -> tuple[str, list[str]]:
    """Replace detectable secrets in *text* with redaction placeholders.

    Returns:
        (redacted_text, list_of_redacted_pattern_names)

    The redacted list is non-empty iff at least one substitution was made.
    Pattern names are de-duplicated but ordered by first occurrence.
    """
    if not text:
        return text, []

    result = text
    found_names: list[str] = []
    seen_names: set[str] = set()

    # Process patterns in reverse specificity order so that overlapping ranges
    # are replaced by the most specific pattern (run-after means the longer
    # match has already been replaced and subsequent passes hit the placeholder).
    for pattern in _PATTERNS:
        def _replace(m: re.Match, _name: str = pattern.name) -> str:
            if _name not in seen_names:
                found_names.append(_name)
                seen_names.add(_name)
            return REDACTION_PLACEHOLDER.format(name=_name)

        result = pattern.regex.sub(_replace, result)

    return result, found_names
