"""Notion OAuth (public integration) through the hosted broker in broker/.

The broker holds the Notion client secret; the daemon never sees it. Flow:
`start()` makes a single-use state "<port>.<nonce>" and returns the broker's
authorize URL. Notion redirects to the broker, the broker redirects to
/v1/integrations/notion/callback on this daemon, and `finish()` swaps the code
for a token via the broker's /api/notion/token.

The token lands in outputs.notion.token — the same key the manual
internal-integration flow uses — so notion.write() needs no special case.
"""
from __future__ import annotations

import secrets
import threading
import time
from urllib.parse import urlencode

import requests

from .. import config as config_mod
from ..config import Config
from ..outputs.notion import NOTION_API, _headers

STATE_TTL_SEC = 600

_pending: dict[str, float] = {}   # nonce -> expiry (monotonic)
_lock = threading.Lock()


class NotionAuthError(Exception):
    pass


def _broker(cfg: Config) -> str:
    url = cfg.notion_broker_url
    if not url:
        raise NotionAuthError(
            "No Notion broker configured. Set notion.broker_url in config.json "
            "(or MEETING_SCRIBE_BROKER_URL), or use an internal integration token.")
    return url


def start(cfg: Config, port: int) -> str:
    """Remember a fresh nonce and return the broker authorize URL to open."""
    broker = _broker(cfg)
    nonce = secrets.token_urlsafe(24)
    now = time.monotonic()
    with _lock:
        for n, exp in list(_pending.items()):
            if exp < now:
                del _pending[n]
        _pending[nonce] = now + STATE_TTL_SEC
    return f"{broker}/api/notion/authorize?" + urlencode({"state": f"{port}.{nonce}"})


def _consume(state: str) -> bool:
    nonce = state.split(".", 1)[1] if "." in state else ""
    with _lock:
        exp = _pending.pop(nonce, None)
    return exp is not None and exp >= time.monotonic()


def _exchange(cfg: Config, body: dict) -> dict:
    try:
        r = requests.post(f"{_broker(cfg)}/api/notion/token", json=body, timeout=30)
    except requests.RequestException as e:
        raise NotionAuthError(f"could not reach the Notion broker: {e}") from e
    try:
        data = r.json()
    except ValueError:
        data = {}
    if r.status_code >= 300 or not data.get("access_token"):
        err = data.get("error_description") or data.get("error") or "no access token returned"
        raise NotionAuthError(f"Notion token exchange failed ({r.status_code}): {err}")
    return data


def _save_tokens(data: dict, **extra) -> None:
    notion = {"token": data["access_token"], **extra}
    if data.get("refresh_token"):
        notion["refresh_token"] = data["refresh_token"]
    config_mod.write_patch({"outputs": {"notion": notion}})


def finish(cfg: Config, code: str, state: str) -> dict:
    """Check the single-use state, trade the code for a token, save it. Returns {workspace_name}."""
    if not _consume(state):
        raise NotionAuthError("This sign-in link is invalid or expired. Start again from Settings.")
    data = _exchange(cfg, {"code": code})
    workspace = data.get("workspace_name") or ""
    _save_tokens(data, workspace_name=workspace, enabled=True)
    return {"workspace_name": workspace}


def refresh(cfg: Config) -> bool:
    """Swap a stored refresh token for a new access token. False if there is none or it fails."""
    token = cfg.get("outputs", "notion", "refresh_token", default="")
    if not token or not cfg.notion_broker_url:
        return False
    try:
        data = _exchange(cfg, {"refresh_token": token})
    except NotionAuthError:
        return False
    _save_tokens(data)
    return True


def disconnect(cfg: Config) -> None:
    config_mod.write_patch({"outputs": {"notion": {
        "token": "", "refresh_token": "", "workspace_name": "", "database_id": "",
        "enabled": False}}})


def _plain(rich: list) -> str:
    return "".join(t.get("plain_text", "") for t in rich or [])


def list_databases(cfg: Config) -> list[dict]:
    """Databases shared with the integration, with their title and first date property."""
    token = cfg.notion_token
    if not token:
        raise NotionAuthError("Notion is not connected.")
    out: list[dict] = []
    body: dict = {"filter": {"property": "object", "value": "database"}, "page_size": 100}
    while True:
        r = requests.post(f"{NOTION_API}/search", headers=_headers(token), json=body, timeout=30)
        if r.status_code >= 300:
            raise NotionAuthError(f"Notion search failed ({r.status_code}): {r.text[:200]}")
        data = r.json()
        for db in data.get("results", []):
            props = db.get("properties") or {}
            out.append({
                "id": db["id"],
                "title": _plain(db.get("title")) or "Untitled",
                "title_property": next((n for n, p in props.items() if p.get("type") == "title"), ""),
                "date_property": next((n for n, p in props.items() if p.get("type") == "date"), ""),
            })
        if not data.get("has_more") or not data.get("next_cursor"):
            return out
        body["start_cursor"] = data["next_cursor"]
