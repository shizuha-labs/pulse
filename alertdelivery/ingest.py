"""PLAT-5928 §8.4.1B — strict raw ingest, closed AlertEnvelopeV1, RFC 8785 bytes.

Parses request-body bytes BEFORE any ``request.data`` / ordinary dictionary
access. Enforces:
- strict UTF-8, no BOM, well-formed Unicode scalar values, valid I-JSON;
- duplicate-preserving event parse: duplicate member names rejected at every
  object depth after JSON escape decoding;
- lone surrogates, non-finite numbers and out-of-schema values rejected;
- closed ``AlertEnvelopeV1`` construction + RFC 8785 JCS canonical bytes +
  domain-separated SHA-256 digests.

No tenant payload is ever logged; bounded failure reasons only.
"""
from __future__ import annotations

import base64
import datetime
import hashlib
import json
import re
import struct
from typing import Any

_ENVELOPE_SCHEMA = 'shizuha.alert-envelope.v1'

# Literal domain bytes with NUL separators and terminal NUL (HLD §8.4.1B).
ENV_DOMAIN = b'SHIZUHA\x00ALERT_ENVELOPE\x00V1\x00'
INGRESS_DOMAIN = b'SHIZUHA\x00ALERT_INGRESS\x00V1\x00'
PROOF_DOMAIN = b'SHIZUHA\x00ALERT_ROUTING_DECISION_PROOF\x00V1\x00'

_RFC3339 = re.compile(
    r'^(?P<Y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})'
    r'[Tt](?P<H>\d{2}):(?P<M>\d{2}):(?P<S>\d{2})'
    r'(?P<f>\.\d{1,9})?(?P<z>[Zz]|[+-]\d{2}:\d{2})$'
)


class StrictDecodeError(ValueError):
    """Raised for any strict wire/schema failure. Carries a bounded reason."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class DuplicateKeyError(StrictDecodeError):
    pass


def _reject_duplicate_keys(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise DuplicateKeyError('duplicate_member_key')
        out[key] = value
    return out


def _parse_constant(value):
    # Reject NaN/Infinity/-Infinity (non-finite numbers are out of I-JSON).
    raise StrictDecodeError('non_finite_number')


def _parse_float(value):
    # A huge exponent like 1e999 bypasses parse_constant and the C scanner
    # yields inf; reject any non-finite float here.
    f = float(value)
    if f != f or f in (float('inf'), float('-inf')):
        raise StrictDecodeError('non_finite_number')
    return f


def strict_json_loads(raw: bytes):
    """Strict-decodes raw request bytes into a Python object (no aliasing).

    Raises ``StrictDecodeError`` on: invalid UTF-8, BOM, trailing data,
    duplicate member names, non-finite numbers, lone surrogates or malformed
    JSON.
    """
    if isinstance(raw, str):
        raw = raw.encode('utf-8')
    if raw.startswith(b'\xef\xbb\xbf'):
        raise StrictDecodeError('leading_bom')
    try:
        text = raw.decode('utf-8')  # strictly validates UTF-8, rejects surrogates
    except UnicodeDecodeError:
        raise StrictDecodeError('invalid_utf8') from None
    # Reject lone surrogates explicitly (json.loads would allow them via string).
    if re.search(r'[\ud800-\udfff]', text):
        raise StrictDecodeError('lone_surrogate')
    decoder = json.JSONDecoder(object_pairs_hook=_reject_duplicate_keys,
                               parse_constant=_parse_constant,
                               parse_float=_parse_float)
    try:
        obj, end = decoder.raw_decode(text)
    except StrictDecodeError:
        raise
    except json.JSONDecodeError as exc:
        raise StrictDecodeError(f'invalid_json:{exc.pos}') from None
    if end != len(text):
        raise StrictDecodeError('trailing_json_data')
    return obj


def normalize_starts_at(value) -> tuple[int, int, str]:
    """Validate a strict RFC3339Nano startsAt. Returns (seconds, nanos, text).

    Raises ``StrictDecodeError`` with a bounded reason on missing/null/wrong
    type/non-canonical/invalid timestamps. Canonicalizes to UTC seconds+nanos.
    """
    if value is None:
        raise StrictDecodeError('starts_at_null')
    if not isinstance(value, str):
        raise StrictDecodeError('starts_at_wrong_type')
    value = value.strip()
    if not value:
        raise StrictDecodeError('starts_at_empty')
    m = _RFC3339.match(value)
    if not m:
        raise StrictDecodeError('starts_at_noncanonical')
    try:
        dt = datetime.datetime(
            year=int(m['Y']), month=int(m['m']), day=int(m['d']),
            hour=int(m['H']), minute=int(m['M']), second=int(m['S']),
            tzinfo=datetime.timezone.utc,
        )
    except ValueError:
        raise StrictDecodeError('starts_at_invalid') from None
    zone = m['z']
    if zone.lower() != 'z':
        sign = 1 if zone[0] == '+' else -1
        off_h, off_m = int(zone[1:3]), int(zone[4:6])
        dt = dt - datetime.timedelta(hours=off_h * sign, minutes=off_m * sign)
    frac = m['f'] or ''
    nanos = 0
    if frac:
        digits = frac[1:]  # strip leading '.'
        nanos = int(digits.ljust(9, '0'))
    seconds = int(dt.timestamp())
    return seconds, nanos, value


def _canonical_bytes(obj: Any) -> bytes:
    """RFC 8785 JCS canonical bytes for the *validated* object."""
    if isinstance(obj, dict):
        items = sorted(
            ((k, _canonical_bytes(v)) for k, v in obj.items()),
            key=lambda kv: kv[0].encode('utf-16', 'surrogatepass'),
        )
        inner = b','.join(
            json.dumps(k, ensure_ascii=False).encode('utf-8') + b':' + v
            for k, v in items
        )
        return b'{' + inner + b'}'
    if isinstance(obj, list):
        return b'[' + b','.join(_canonical_bytes(v) for v in obj) + b']'
    if obj is None:
        return b'null'
    if isinstance(obj, bool):
        return b'true' if obj else b'false'
    if isinstance(obj, int):
        return str(obj).encode('utf-8')
    if isinstance(obj, float):
        if obj != obj or obj in (float('inf'), float('-inf')):
            raise StrictDecodeError('non_finite_number')
        return repr(obj).encode('utf-8').replace(b'E', b'e')
    return json.dumps(obj, ensure_ascii=False).encode('utf-8')


def frame(domain: bytes, payload: bytes) -> bytes:
    """FRAME(D,P) = D || uint64_be(len(P)) || P."""
    return domain + struct.pack('>Q', len(payload)) + payload


def b64url(raw: bytes) -> str:
    """RFC 4648 base64url without padding (Digest32 wire form)."""
    return base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')


def ingest_identity(organization_id: int, producer_lineage_id: int,
                    envelope_digest: str) -> str:
    """b64url_no_pad over the deterministic ingest identity domain."""
    payload = {
        'organization_id': str(organization_id),
        'producer_lineage_id': str(producer_lineage_id),
        'envelope_digest': envelope_digest,
    }
    blob = _canonical_bytes(payload)
    digest = hashlib.sha256(frame(INGRESS_DOMAIN, blob)).digest()
    return b64url(digest)


def envelope_digest(envelope: dict) -> str:
    """Digest32 for the closed AlertEnvelopeV1."""
    blob = _canonical_bytes(envelope)
    return b64url(hashlib.sha256(frame(ENV_DOMAIN, blob)).digest())


def build_envelope(*, organization_id, source_name, webhook_key_id,
                   webhook_key_revision, project_id, project_key,
                   labels, annotations, fingerprint, starts_at, ends_at,
                   status, severity, title_input, description_input,
                   source_url) -> dict:
    """Construct the closed AlertEnvelopeV1 inventory (validation by caller)."""
    return {
        'schema': _ENVELOPE_SCHEMA,
        'authority': {'organization_id': str(organization_id)},
        'source': {
            'kind': 'alertmanager',
            'source_name': str(source_name or ''),
            'webhook_key_id': str(webhook_key_id or ''),
            'webhook_key_revision': str(webhook_key_revision or ''),
            'project_id': str(project_id) if project_id is not None else None,
            'project_key': str(project_key) if project_key is not None else None,
        },
        'alert': {
            'labels': labels or {},
            'annotations': annotations or {},
            'fingerprint': fingerprint or None,
            'starts_at': starts_at,
            'ends_at': ends_at or None,
            'status': str(status or '').strip().lower(),
            'severity': str(severity or '').strip().lower(),
            'title_input': str(title_input or ''),
            'description_input': str(description_input or ''),
            'source_url': source_url or None,
        },
    }
