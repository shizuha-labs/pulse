"""
Authentication views for JWT and OAuth authentication.
"""
from django.contrib.auth import get_user_model, authenticate
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.conf import settings
from rest_framework import status, permissions
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.serializers import TokenRefreshSerializer
from rest_framework_simplejwt.settings import api_settings
from rest_framework_simplejwt.views import TokenRefreshView as SimpleJWTTokenRefreshView
from allauth.socialaccount.providers.google.views import GoogleOAuth2Adapter
from allauth.socialaccount.providers.github.views import GitHubOAuth2Adapter
from allauth.socialaccount.providers.oauth2.client import OAuth2Client
from allauth.socialaccount.models import SocialApp
import logging

logger = logging.getLogger(__name__)
User = get_user_model()


def get_tokens_for_user(user):
    """Generate JWT tokens for a user."""
    refresh = RefreshToken.for_user(user)
    return {
        'refresh': str(refresh),
        'access': str(refresh.access_token),
    }


class LoginView(APIView):
    """
    Login with username/email and password.
    Returns JWT access and refresh tokens.
    """
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        username = request.data.get('username', '').strip()
        password = request.data.get('password', '')

        if not username or not password:
            return Response(
                {'error': 'Username and password are required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Try to authenticate with username
        user = authenticate(request, username=username, password=password)

        # If failed, try with email
        if not user:
            try:
                user_obj = User.objects.get(email__iexact=username)
                user = authenticate(request, username=user_obj.username, password=password)
            except User.DoesNotExist:
                pass

        if not user:
            return Response(
                {'error': 'Invalid credentials'},
                status=status.HTTP_401_UNAUTHORIZED
            )

        if not user.is_active:
            return Response(
                {'error': 'Account is disabled'},
                status=status.HTTP_401_UNAUTHORIZED
            )

        tokens = get_tokens_for_user(user)
        logger.info(f"User {user.username} logged in successfully")

        return Response({
            'user': {
                'id': user.id,
                'username': user.username,
                'email': user.email,
                'first_name': user.first_name,
                'last_name': user.last_name,
                'is_staff': user.is_staff,
            },
            'tokens': tokens,
        })


class LogoutView(APIView):
    """
    Logout by blacklisting the refresh token.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        try:
            refresh_token = request.data.get('refresh')
            if refresh_token:
                token = RefreshToken(refresh_token)
                token.blacklist()
                logger.info(f"User {request.user.username} logged out")
            return Response({'message': 'Successfully logged out'})
        except TokenError as e:
            logger.warning(f"Logout failed: {e}")
            return Response(
                {'error': 'Invalid token'},
                status=status.HTTP_400_BAD_REQUEST
            )


class RegisterView(APIView):
    """
    Register a new user account.
    """
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        username = request.data.get('username', '').strip()
        email = request.data.get('email', '').strip()
        password = request.data.get('password', '')
        password2 = request.data.get('password2', '')
        first_name = request.data.get('first_name', '').strip()
        last_name = request.data.get('last_name', '').strip()

        # Validate required fields
        if not username:
            return Response(
                {'error': 'Username is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if not email:
            return Response(
                {'error': 'Email is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if not password:
            return Response(
                {'error': 'Password is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if password != password2:
            return Response(
                {'error': 'Passwords do not match'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Check if username exists
        if User.objects.filter(username__iexact=username).exists():
            return Response(
                {'error': 'Username already exists'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Check if email exists
        if User.objects.filter(email__iexact=email).exists():
            return Response(
                {'error': 'Email already exists'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Validate password
        try:
            validate_password(password)
        except ValidationError as e:
            return Response(
                {'error': list(e.messages)},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Create user
        try:
            user = User.objects.create_user(
                username=username,
                email=email,
                password=password,
                first_name=first_name,
                last_name=last_name,
            )
            tokens = get_tokens_for_user(user)
            logger.info(f"New user registered: {username}")

            return Response({
                'user': {
                    'id': user.id,
                    'username': user.username,
                    'email': user.email,
                    'first_name': user.first_name,
                    'last_name': user.last_name,
                    'is_staff': user.is_staff,
                },
                'tokens': tokens,
            }, status=status.HTTP_201_CREATED)
        except Exception as e:
            logger.error(f"Registration failed: {e}")
            return Response(
                {'error': 'Registration failed'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class UserView(APIView):
    """
    Get or update the current authenticated user.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        user = request.user
        return Response({
            'id': user.id,
            'username': user.username,
            'email': user.email,
            'first_name': user.first_name,
            'last_name': user.last_name,
            'is_staff': user.is_staff,
            'date_joined': user.date_joined.isoformat(),
        })

    def patch(self, request):
        user = request.user
        first_name = request.data.get('first_name')
        last_name = request.data.get('last_name')
        email = request.data.get('email')

        if first_name is not None:
            user.first_name = first_name.strip()
        if last_name is not None:
            user.last_name = last_name.strip()
        if email is not None:
            email = email.strip()
            if email and User.objects.filter(email__iexact=email).exclude(id=user.id).exists():
                return Response(
                    {'error': 'Email already in use'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            user.email = email

        user.save()
        logger.info(f"User {user.username} updated profile")

        return Response({
            'id': user.id,
            'username': user.username,
            'email': user.email,
            'first_name': user.first_name,
            'last_name': user.last_name,
            'is_staff': user.is_staff,
        })


class PasswordChangeView(APIView):
    """
    Change password for the current user.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        old_password = request.data.get('old_password', '')
        new_password = request.data.get('new_password', '')
        new_password2 = request.data.get('new_password2', '')

        if not old_password or not new_password:
            return Response(
                {'error': 'Old password and new password are required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if new_password != new_password2:
            return Response(
                {'error': 'New passwords do not match'},
                status=status.HTTP_400_BAD_REQUEST
            )

        user = request.user
        if not user.check_password(old_password):
            return Response(
                {'error': 'Current password is incorrect'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            validate_password(new_password, user)
        except ValidationError as e:
            return Response(
                {'error': list(e.messages)},
                status=status.HTTP_400_BAD_REQUEST
            )

        user.set_password(new_password)
        user.save()
        logger.info(f"User {user.username} changed password")

        # Generate new tokens since password changed
        tokens = get_tokens_for_user(user)

        return Response({
            'message': 'Password changed successfully',
            'tokens': tokens,
        })


class GoogleLoginView(APIView):
    """
    Initiate or complete Google OAuth login.
    POST with {'code': '...'} to exchange OAuth code for tokens.
    GET to get the OAuth URL.
    """
    permission_classes = [permissions.AllowAny]

    def get(self, request):
        """Return the Google OAuth URL."""
        try:
            app = SocialApp.objects.get(provider='google')
            callback_url = request.build_absolute_uri('/api/auth/social/callback/')
            auth_url = (
                f"https://accounts.google.com/o/oauth2/v2/auth?"
                f"client_id={app.client_id}&"
                f"redirect_uri={callback_url}&"
                f"response_type=code&"
                f"scope=openid%20email%20profile&"
                f"access_type=online"
            )
            return Response({'url': auth_url})
        except SocialApp.DoesNotExist:
            # Check environment config
            client_id = settings.SOCIALACCOUNT_PROVIDERS.get('google', {}).get('APP', {}).get('client_id', '')
            if client_id:
                callback_url = request.build_absolute_uri('/api/auth/social/callback/')
                auth_url = (
                    f"https://accounts.google.com/o/oauth2/v2/auth?"
                    f"client_id={client_id}&"
                    f"redirect_uri={callback_url}&"
                    f"response_type=code&"
                    f"scope=openid%20email%20profile&"
                    f"access_type=online"
                )
                return Response({'url': auth_url})
            return Response(
                {'error': 'Google OAuth is not configured'},
                status=status.HTTP_501_NOT_IMPLEMENTED
            )

    def post(self, request):
        """Exchange OAuth code for user tokens."""
        code = request.data.get('code')
        if not code:
            return Response(
                {'error': 'OAuth code is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # This would be implemented with the allauth flow
        # For now, return a placeholder
        return Response(
            {'error': 'OAuth code exchange not yet implemented. Use the callback URL.'},
            status=status.HTTP_501_NOT_IMPLEMENTED
        )


class GitHubLoginView(APIView):
    """
    Initiate or complete GitHub OAuth login.
    """
    permission_classes = [permissions.AllowAny]

    def get(self, request):
        """Return the GitHub OAuth URL."""
        try:
            app = SocialApp.objects.get(provider='github')
            callback_url = request.build_absolute_uri('/api/auth/social/callback/')
            auth_url = (
                f"https://github.com/login/oauth/authorize?"
                f"client_id={app.client_id}&"
                f"redirect_uri={callback_url}&"
                f"scope=user:email"
            )
            return Response({'url': auth_url})
        except SocialApp.DoesNotExist:
            # Check environment config
            client_id = settings.SOCIALACCOUNT_PROVIDERS.get('github', {}).get('APP', {}).get('client_id', '')
            if client_id:
                callback_url = request.build_absolute_uri('/api/auth/social/callback/')
                auth_url = (
                    f"https://github.com/login/oauth/authorize?"
                    f"client_id={client_id}&"
                    f"redirect_uri={callback_url}&"
                    f"scope=user:email"
                )
                return Response({'url': auth_url})
            return Response(
                {'error': 'GitHub OAuth is not configured'},
                status=status.HTTP_501_NOT_IMPLEMENTED
            )


class SocialCallbackView(APIView):
    """
    Handle OAuth callback from social providers.
    This is called by the frontend after the OAuth redirect.
    """
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        """
        Exchange OAuth code for user tokens.
        Request body: { provider: 'google'|'github', code: '...' }
        """
        provider = request.data.get('provider', '').lower()
        code = request.data.get('code')

        if not provider or not code:
            return Response(
                {'error': 'Provider and code are required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if provider not in ['google', 'github']:
            return Response(
                {'error': 'Invalid provider. Must be google or github'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # For now, return instructions - full OAuth implementation requires
        # additional setup with allauth adapters
        return Response({
            'message': 'OAuth callback received',
            'provider': provider,
            'note': 'Full OAuth integration requires SocialApp configuration in Django admin',
        })


class OAuthStatusView(APIView):
    """
    Check which OAuth providers are configured.
    """
    permission_classes = [permissions.AllowAny]

    def get(self, request):
        providers = {}

        # Check Google
        try:
            SocialApp.objects.get(provider='google')
            providers['google'] = True
        except SocialApp.DoesNotExist:
            # Check environment config
            client_id = settings.SOCIALACCOUNT_PROVIDERS.get('google', {}).get('APP', {}).get('client_id', '')
            providers['google'] = bool(client_id)

        # Check GitHub
        try:
            SocialApp.objects.get(provider='github')
            providers['github'] = True
        except SocialApp.DoesNotExist:
            # Check environment config
            client_id = settings.SOCIALACCOUNT_PROVIDERS.get('github', {}).get('APP', {}).get('client_id', '')
            providers['github'] = bool(client_id)

        return Response({
            'providers': providers,
            'username_password': True,  # Always available
        })


class FederatedTokenRefreshSerializer(TokenRefreshSerializer):
    """Refresh serializer that does not require a local user row (PLS-632).

    Pulse authenticates federated Shizuha-ID users as FederatedUser objects
    built from token claims (tasks.authentication.ShizuhaJWTAuthentication) —
    ID users are not guaranteed to exist in Pulse's local auth.User table, and
    shizuha-id user_ids collide with local auto-increment PKs. SimpleJWT's stock
    TokenRefreshSerializer performs ``get_user_model().objects.get(id=user_id)``
    on refresh, which 401s (inactive colliding local user) or 500s (no local
    row) for a perfectly valid ID-issued refresh token — the access-expiry
    mid-session regression (PLS-632). The refresh token carries the full
    identity, and the blacklist mixin already resolves missing users to
    ``user=None`` for OutstandingToken/BlacklistedToken, so the unconditional
    local lookup is unnecessary and harmful. The conditional form (review
    finding on this PR): deny only when a LOCAL row exists AND is inactive
    (the offboarding control); federated users (no row) and active local
    users refresh. Signature/type/expiry/blacklist validation and rotation
    stay intact.
    """

    def validate(self, attrs):
        refresh = self.token_class(attrs["refresh"])
        # PLS-632 review: the offboarding control is restored CONDITIONALLY.
        # The stock lookup (get(id=user_id)) 401/500s for federated users with
        # no local row — the PLS-632 bug. But dropping the lookup entirely also
        # dropped the is_active offboarding control: a deactivated local user
        # holding a live refresh token could silently refresh until token
        # expiry. The conditional form denies only when a LOCAL row exists AND
        # is inactive; federated users (no local row) and active local users
        # refresh normally. Signature/type/expiry/blacklist validation and
        # rotation stay intact.
        user_id = refresh.payload.get(api_settings.USER_ID_CLAIM)
        local_row = (
            User.objects.filter(**{api_settings.USER_ID_FIELD: user_id}).first()
        )
        if local_row is not None and not local_row.is_active:
            from rest_framework.exceptions import AuthenticationFailed
            raise AuthenticationFailed('User is inactive', code='user_inactive')
        data = {"access": str(refresh.access_token)}
        if api_settings.ROTATE_REFRESH_TOKENS:
            if api_settings.BLACKLIST_AFTER_ROTATION:
                try:
                    # Attempt to blacklist the given refresh token
                    refresh.blacklist()
                except AttributeError:
                    # If blacklist app not installed, `blacklist` method will
                    # not be present
                    pass

            refresh.set_jti()
            refresh.set_exp()
            refresh.set_iat()
            refresh.outstand()

            data["refresh"] = str(refresh)

        return data


class FederatedTokenRefreshView(SimpleJWTTokenRefreshView):
    """TokenRefreshView wired to the federated serializer (PLS-632)."""

    serializer_class = FederatedTokenRefreshSerializer
