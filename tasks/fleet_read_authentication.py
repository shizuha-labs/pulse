"""Read-only OAuth identity for Hive's fleet demand snapshots.

This authenticator is installed only on the two snapshot views. A service
identity never becomes a staff user or acquires general Pulse access.
"""

from dataclasses import dataclass

from django.conf import settings
import jwt
from rest_framework.exceptions import PermissionDenied
from rest_framework_simplejwt.exceptions import InvalidToken

from .authentication import ShizuhaJWTAuthentication


CLIENT_ID = 'hive-pulse-fleet-read'
READ_SCOPE = 'pulse:fleet-snapshot.read'


@dataclass(frozen=True)
class HiveFleetReader:
    id: str = f'service:{CLIENT_ID}'
    pk: str = f'service:{CLIENT_ID}'
    username: str = CLIENT_ID
    email: str = ''
    is_authenticated: bool = True
    is_anonymous: bool = False
    is_active: bool = True
    is_service: bool = True
    is_staff: bool = False
    is_superuser: bool = False


class HiveFleetReadAuthentication(ShizuhaJWTAuthentication):
    def authenticate(self, request):
        header = self.get_header(request)
        raw_token = self.get_raw_token(header) if header is not None else None
        if raw_token is None:
            return None
        try:
            # Unverified claims select a verifier only; they grant no access.
            hint = jwt.decode(raw_token, options={'verify_signature': False})
        except jwt.PyJWTError:
            return None  # Existing user authentication reports malformed tokens.
        if not (hint.get('is_service') or str(hint.get('sub', '')).startswith('service:')):
            return None
        if request.method != 'GET':
            raise PermissionDenied('Fleet snapshot service credentials are read-only.')
        if jwt.get_unverified_header(raw_token).get('alg') != 'RS256':
            raise InvalidToken('Fleet snapshot service credentials require RS256.')

        # Share Pulse's signature/expiry verification and fail-closed ID
        # revocation lookup, including the existing jti/client cutoff checks.
        try:
            token = self.get_validated_token(raw_token)
        except (TypeError, ValueError) as exc:
            raise InvalidToken('Fleet snapshot token has malformed claims.') from exc
        issuer = getattr(settings, 'PULSE_FLEET_READ_ISSUER', '')
        if not issuer or token.get('iss') != issuer:
            raise InvalidToken('Fleet snapshot token has an invalid issuer.')
        if token.get('aud') not in ('pulse', ['pulse']):
            raise InvalidToken('Fleet snapshot token requires the pulse audience.')
        if (
            token.get('is_service') is not True
            or token.get('client_id') != CLIENT_ID
            or token.get('sub') != f'service:{CLIENT_ID}'
            or token.get('token_type') != 'access'
            or token.get('scope') != READ_SCOPE
        ):
            raise InvalidToken('Fleet snapshot token has an invalid service grant.')
        if (
            not isinstance(token.get('exp'), int)
            or isinstance(token.get('exp'), bool)
            or not isinstance(token.get('iat'), int)
            or isinstance(token.get('iat'), bool)
            or not isinstance(token.get('jti'), str)
            or not token['jti']
        ):
            raise InvalidToken('Fleet snapshot token requires exp, iat and jti.')
        return HiveFleetReader(), token


def is_hive_fleet_reader(request):
    return request.method == 'GET' and isinstance(request.user, HiveFleetReader)
