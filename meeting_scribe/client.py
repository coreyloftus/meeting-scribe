"""Thin HTTP client for the scribed daemon, used by the CLI (and tests).

Discovers the daemon via ~/.local/state/meeting-scribe/daemon.json and
authenticates with the token file next to it. All methods raise DaemonError on
transport problems; callers that want graceful fallback should check is_up().
"""
from __future__ import annotations

try:
    import requests
except ImportError:  # pure-CLI install without deps: --local paths still work
    requests = None

from .daemon import state


class DaemonError(Exception):
    pass


class DaemonClient:
    def __init__(self, timeout: float = 10.0):
        self.base = state.base_url()
        self.token = state.read_token()
        self.timeout = timeout

    def is_up(self) -> bool:
        if requests is None or not self.base or not self.token:
            return False
        try:
            return self.request("GET", "/v1/status", timeout=1.5) is not None
        except DaemonError:
            return False

    def request(self, method: str, path: str, json_body: dict | None = None,
                timeout: float | None = None, **params) -> dict:
        if requests is None:
            raise DaemonError("the `requests` package is required to talk to the daemon")
        if not self.base:
            raise DaemonError("daemon not discovered (no daemon.json)")
        try:
            r = requests.request(
                method, self.base + path,
                headers={"Authorization": f"Bearer {self.token or ''}"},
                json=json_body, params=params or None,
                timeout=timeout or self.timeout)
        except requests.RequestException as e:
            raise DaemonError(f"daemon unreachable: {e}") from e
        if r.status_code >= 300:
            try:
                detail = r.json().get("detail", r.text)
            except ValueError:
                detail = r.text
            raise DaemonError(f"{method} {path} -> {r.status_code}: {detail}")
        return r.json()

    # convenience wrappers -----------------------------------------------------
    def status(self) -> dict:
        return self.request("GET", "/v1/status")

    def start(self) -> dict:
        return self.request("POST", "/v1/start")

    def stop(self) -> dict:
        return self.request("POST", "/v1/stop")

    def meetings(self, limit: int = 50, q: str | None = None) -> list[dict]:
        params = {"limit": limit}
        if q:
            params["q"] = q
        return self.request("GET", "/v1/meetings", **params)["meetings"]

    def reprocess(self, meeting_id: str) -> dict:
        return self.request("POST", f"/v1/meetings/{meeting_id}/reprocess")

    def push(self, meeting_id: str, target: str, options: dict | None = None) -> dict:
        return self.request("POST", f"/v1/meetings/{meeting_id}/push",
                            json_body={"target": target, "options": options})

    def doctor(self) -> dict:
        return self.request("GET", "/v1/doctor")
