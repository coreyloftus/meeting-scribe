"""Write the note to a Notion database via the Notion REST API directly.

No MCP, no Claude-Code dependency — just an integration token and a database ID
in config.json, so it's fully portable. Markdown is converted to Notion blocks
(headings, bullets, to-dos, dividers, paragraphs).
"""
from __future__ import annotations

import re
import time

import requests

from .. import config as config_mod
from ..config import Config
from .base import OutputResult

KEY = "notion"
LABEL = "Notion"

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
MAX_RICH_TEXT = 1900      # Notion hard limit is 2000 chars per rich-text item
MAX_CHILDREN = 100        # Notion limit per create/append request


class NotionError(Exception):
    pass


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _rich_text(text: str) -> list[dict]:
    # Split overly long strings so we never exceed Notion's 2000-char cap.
    chunks = [text[i:i + MAX_RICH_TEXT] for i in range(0, len(text), MAX_RICH_TEXT)] or [""]
    return [{"type": "text", "text": {"content": c}} for c in chunks]


def _block(block_type: str, text: str, extra: dict | None = None) -> dict:
    body = {"rich_text": _rich_text(text)}
    if extra:
        body.update(extra)
    return {"object": "block", "type": block_type, block_type: body}


def markdown_to_blocks(md: str) -> list[dict]:
    blocks: list[dict] = []
    for raw in md.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.strip() == "---":
            blocks.append({"object": "block", "type": "divider", "divider": {}})
        elif line.startswith("### "):
            blocks.append(_block("heading_3", line[4:]))
        elif line.startswith("## "):
            blocks.append(_block("heading_2", line[3:]))
        elif line.startswith("# "):
            blocks.append(_block("heading_1", line[2:]))
        elif re.match(r"^[-*] \[[ xX]\] ", line):
            checked = line[3] in "xX"
            text = line.split("] ", 1)[1]
            blocks.append(_block("to_do", text, {"checked": checked}))
        elif re.match(r"^[-*] ", line):
            blocks.append(_block("bulleted_list_item", line[2:]))
        else:
            blocks.append(_block("paragraph", line))
    return blocks


def _request(method: str, url: str, token: str, payload: dict) -> requests.Response:
    """Notion allows ~3 requests/s: retry 429 and 5xx, honoring Retry-After."""
    for attempt in range(4):
        resp = requests.request(method, url, headers=_headers(token), json=payload, timeout=30)
        if resp.status_code != 429 and resp.status_code < 500 or attempt == 3:
            return resp
        try:
            wait = float(resp.headers.get("Retry-After", ""))
        except ValueError:
            wait = 2 ** attempt
        time.sleep(min(wait, 30))
    return resp


def _page_id(url: str) -> str | None:
    m = re.search(r"([0-9a-f]{32})$", url.replace("-", ""))
    return m.group(1) if m else None


def _archive(token: str, page_id: str) -> None:
    try:
        _request("PATCH", f"{NOTION_API}/pages/{page_id}", token, {"archived": True})
    except requests.RequestException:
        pass  # best-effort: a leftover page is better than a failed push


def is_configured(cfg: Config) -> bool:
    return bool(cfg.notion_token) and bool(cfg.get("outputs", "notion", "database_id", default=""))


def write(cfg: Config, note, options: dict | None = None) -> OutputResult:
    token = cfg.notion_token
    database_id = cfg.get("outputs", "notion", "database_id", default="")
    if not token:
        raise NotionError("No Notion token (set NOTION_TOKEN or outputs.notion.token).")
    if not database_id:
        raise NotionError("No outputs.notion.database_id configured.")

    title_prop = cfg.get("outputs", "notion", "title_property", default="Name")
    date_prop = cfg.get("outputs", "notion", "date_property", default="Date")

    blocks = markdown_to_blocks(note.summary_md)
    if note.user_notes and note.user_notes.strip():
        blocks.append({"object": "block", "type": "divider", "divider": {}})
        blocks.append(_block("heading_2", "My Notes"))
        blocks += markdown_to_blocks(note.user_notes)
    blocks.append({"object": "block", "type": "divider", "divider": {}})
    blocks.append(_block("heading_2", "Full Transcript"))
    blocks += markdown_to_blocks(note.transcript)

    properties = {title_prop: {"title": _rich_text(note.title)}}
    if date_prop:  # a database with no date column files the page without one
        properties[date_prop] = {"date": {"start": note.date}}
    payload = {
        "parent": {"database_id": database_id},
        "properties": properties,
        "children": blocks[:MAX_CHILDREN],
    }

    resp = _request("POST", f"{NOTION_API}/pages", token, payload)
    if resp.status_code == 401:
        from ..integrations import notion_auth
        if not notion_auth.refresh(cfg):
            raise NotionError("Notion access expired — reconnect in Settings.")
        token = config_mod.load(cfg.source).notion_token
        resp = _request("POST", f"{NOTION_API}/pages", token, payload)
    if resp.status_code >= 300:
        raise NotionError(f"create page failed ({resp.status_code}): {resp.text[:300]}")
    page = resp.json()
    page_id = page["id"]

    # Append any remaining blocks beyond the first 100, in batches.
    remaining = blocks[MAX_CHILDREN:]
    for i in range(0, len(remaining), MAX_CHILDREN):
        batch = remaining[i:i + MAX_CHILDREN]
        r = _request("PATCH", f"{NOTION_API}/blocks/{page_id}/children", token, {"children": batch})
        if r.status_code >= 300:
            _archive(token, page_id)  # don't leave a half-written page behind
            raise NotionError(f"append blocks failed ({r.status_code}): {r.text[:200]}")

    # A re-push replaces the meeting's last page rather than adding another copy.
    old_id = _page_id(note.previous_urls.get(KEY, ""))
    if old_id and old_id != page_id.replace("-", ""):
        _archive(token, old_id)

    url = page.get("url", page_id)
    return OutputResult(target=KEY, ok=True, url=url)
