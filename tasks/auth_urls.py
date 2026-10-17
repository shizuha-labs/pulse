"""
Authentication URL configuration.

Provides JWT-based authentication endpoints:
- /login/ - Username/password login, returns JWT tokens
- /logout/ - Invalidates refresh token
- /refresh/ - Refresh access token
- /register/ - User registration
- /user/ - Current user info
- /password/change/ - Change password
- /social/google/ - Google OAuth login
- /social/github/ - GitHub OAuth login
"""
from django.urls import path, include
from . import auth_views

urlpatterns = [
    # JWT Authentication
    path('login/', auth_views.LoginView.as_view(), name='auth-login'),
    path('logout/', auth_views.LogoutView.as_view(), name='auth-logout'),
    # PLS-632: federated refresh — validates ID-issued refresh tokens without a
    # local auth.User lookup (stock TokenRefreshView 401s/500s on federated users).
    path('refresh/', auth_views.FederatedTokenRefreshView.as_view(), name='auth-refresh'),
    path('register/', auth_views.RegisterView.as_view(), name='auth-register'),

    # User management
    path('user/', auth_views.UserView.as_view(), name='auth-user'),
    path('password/change/', auth_views.PasswordChangeView.as_view(), name='auth-password-change'),

    # OAuth endpoints
    path('social/google/', auth_views.GoogleLoginView.as_view(), name='auth-google'),
    path('social/github/', auth_views.GitHubLoginView.as_view(), name='auth-github'),
    path('social/callback/', auth_views.SocialCallbackView.as_view(), name='auth-social-callback'),

    # Check OAuth configuration status
    path('oauth/status/', auth_views.OAuthStatusView.as_view(), name='auth-oauth-status'),
]
