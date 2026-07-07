"""Google desktop OAuth (loopback flow) + a tiny Drive upload helper.

One-time connect: `scribe google connect` (or Settings → Connect Google in the
app) opens a browser consent page; the refresh token is cached at
config `google.token_path` (chmod 600) and refreshed automatically after that.

Needs a GCP project with the Drive API (and Docs API for gdocs viewing) enabled
and an OAuth client of type "Desktop app" — put its id/secret in config
`google.client_id` / `google.client_secret` (env GOOGLE_CLIENT_ID/SECRET win).

Scopes: `drive.file` only sees files this app created — least privilege that
still lets us upload notes. `userinfo.email` is just to show which account is
connected in Settings.
"""
from __future__ import annotations

import json
import os

from ..config import Config

SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
]
DOC_MIME = "application/vnd.google-apps.document"


class GoogleAuthError(Exception):
    pass


def _imports():
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request, AuthorizedSession
        from google_auth_oauthlib.flow import InstalledAppFlow
        return Credentials, Request, AuthorizedSession, InstalledAppFlow
    except ImportError as e:
        raise GoogleAuthError(
            "Google libraries not installed. Run: pip install -e '.[google]'") from e


def _client_config(cfg: Config) -> dict:
    if not (cfg.google_client_id and cfg.google_client_secret):
        raise GoogleAuthError(
            "No Google OAuth client configured. Create a 'Desktop app' OAuth client "
            "in your GCP project (Drive API enabled) and set google.client_id / "
            "google.client_secret in config.json.")
    return {"installed": {
        "client_id": cfg.google_client_id,
        "client_secret": cfg.google_client_secret,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"],
    }}


def _save_token(cfg: Config, creds) -> None:
    path = cfg.google_token_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(creds.to_json())
    os.chmod(path, 0o600)


def load_credentials(cfg: Config):
    """Cached credentials, refreshed if stale. None if never connected."""
    Credentials, Request, _, _ = _imports()
    path = cfg.google_token_path
    if not path.is_file():
        return None
    try:
        creds = Credentials.from_authorized_user_file(str(path), SCOPES)
    except (ValueError, json.JSONDecodeError):
        return None
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_token(cfg, creds)
        except Exception as e:
            raise GoogleAuthError(
                f"Google token refresh failed ({e}). Reconnect with: scribe google connect")
    return creds


def connect_interactive(cfg: Config) -> dict:
    """Run the browser consent flow; cache the token. Returns {email}."""
    _, _, AuthorizedSession, InstalledAppFlow = _imports()
    flow = InstalledAppFlow.from_client_config(_client_config(cfg), SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True,
                                  authorization_prompt_message="")
    _save_token(cfg, creds)
    email = None
    try:
        r = AuthorizedSession(creds).get("https://www.googleapis.com/oauth2/v2/userinfo", timeout=15)
        email = r.json().get("email")
    except Exception:
        pass
    return {"email": email}


def session(cfg: Config):
    """AuthorizedSession for API calls; raises if not connected."""
    _, _, AuthorizedSession, _ = _imports()
    creds = load_credentials(cfg)
    if creds is None:
        raise GoogleAuthError("Google not connected. Run: scribe google connect")
    return AuthorizedSession(creds)


def is_connected(cfg: Config) -> bool:
    try:
        return cfg.google_token_path.is_file()
    except Exception:
        return False


def upload_file(cfg: Config, name: str, content: str, source_mime: str,
                target_mime: str | None = None, folder_id: str | None = None) -> tuple[str, str]:
    """Multipart upload to Drive. With target_mime=DOC_MIME, Drive converts the
    content (e.g. markdown) into a native Google Doc. Returns (file_id, url)."""
    metadata: dict = {"name": name}
    if target_mime:
        metadata["mimeType"] = target_mime
    if folder_id:
        metadata["parents"] = [folder_id]

    boundary = "scribe-multipart-boundary"
    body = (
        f"--{boundary}\r\n"
        "Content-Type: application/json; charset=UTF-8\r\n\r\n"
        f"{json.dumps(metadata)}\r\n"
        f"--{boundary}\r\n"
        f"Content-Type: {source_mime}; charset=UTF-8\r\n\r\n"
        f"{content}\r\n"
        f"--{boundary}--"
    ).encode("utf-8")

    s = session(cfg)
    r = s.post(
        "https://www.googleapis.com/upload/drive/v3/files"
        "?uploadType=multipart&fields=id,webViewLink",
        headers={"Content-Type": f"multipart/related; boundary={boundary}"},
        data=body, timeout=120)
    if r.status_code >= 300:
        raise GoogleAuthError(f"Drive upload failed ({r.status_code}): {r.text[:300]}")
    data = r.json()
    return data["id"], data.get("webViewLink") or f"https://drive.google.com/file/d/{data['id']}"
