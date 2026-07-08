"""OAuth 2.0 for the YouTube upload scope, with a cached refresh token."""

from __future__ import annotations

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from . import config


class AuthError(RuntimeError):
    """Raised when credentials cannot be obtained."""


def _load_cached() -> Credentials | None:
    if not config.TOKEN_PATH.exists():
        return None
    try:
        return Credentials.from_authorized_user_file(
            str(config.TOKEN_PATH), [config.YOUTUBE_UPLOAD_SCOPE]
        )
    except (ValueError, KeyError):
        return None


def _save(creds: Credentials) -> None:
    config.ensure_config_dir()
    config.TOKEN_PATH.write_text(creds.to_json())
    try:
        config.TOKEN_PATH.chmod(0o600)
    except OSError:
        pass


def get_credentials() -> Credentials:
    """Return valid credentials, running the browser consent flow if needed.

    First run opens a browser for one-time consent and caches the refresh token.
    Subsequent runs load and silently refresh it.
    """
    creds = _load_cached()

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        _save(creds)
        return creds

    if not config.CLIENT_SECRET_PATH.exists():
        raise AuthError(
            "OAuth client secret not found at "
            f"{config.CLIENT_SECRET_PATH}.\n"
            "Create an OAuth Desktop client in Google Cloud, download it, and "
            "save it there (or set GOPRO_STITCH_CLIENT_SECRET). See README."
        )

    flow = InstalledAppFlow.from_client_secrets_file(
        str(config.CLIENT_SECRET_PATH), [config.YOUTUBE_UPLOAD_SCOPE]
    )
    creds = flow.run_local_server(port=0)
    _save(creds)
    return creds


def bearer_header(creds: Credentials) -> dict[str, str]:
    """Return an Authorization header, refreshing the token if it has expired."""
    if not creds.valid and creds.refresh_token:
        creds.refresh(Request())
        _save(creds)
    return {"Authorization": f"Bearer {creds.token}"}
