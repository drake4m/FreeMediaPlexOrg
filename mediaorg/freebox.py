"""Minimal Freebox OS API client: app authorisation, session, read download tasks.

Reference: https://dev.freebox.fr/sdk/os/ (login, download).  Only read calls are
made during normal runs; the single write is the one-time authorisation request.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass


class FreeboxError(Exception):
    pass


class FreeboxApiError(FreeboxError):
    """The API answered with success = false; `code` is its error_code (e.g. "task_not_found")."""

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


@dataclass
class Task:
    id: int
    name: str
    status: str
    rx_pct: int  # 0..10000
    size: int
    download_dir: str  # decoded Freebox-side path
    type: str
    stop_ratio: int | None
    error: str | None
    created_ts: int | None = None


@dataclass
class TaskFile:
    filepath: str  # decoded Freebox-side path
    size: int
    rx: int
    status: str
    priority: str


def _b64(s: str | None) -> str:
    if not s:
        return ""
    try:
        return base64.b64decode(s).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return ""


class FreeboxClient:
    def __init__(self, base_url: str, ca_file: str | None, timeout: int = 10, opener=None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session_token: str | None = None
        self.permissions: dict = {}
        self._api_prefix: str | None = None
        if opener is not None:
            self._opener = opener
        elif self.base_url.startswith("https://"):
            ctx = ssl.create_default_context(cafile=ca_file) if ca_file else ssl.create_default_context()
            # Freebox certificates do not satisfy Python >= 3.13 strict X.509 checks
            # (same relaxation as the freebox-api reference client); chain and hostname
            # are still verified.
            ctx.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
            self._opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))
        else:
            self._opener = urllib.request.build_opener()

    # -- transport --------------------------------------------------------------------
    def _request(self, method: str, url: str, body: dict | None = None, auth: bool = True) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)  # noqa: S310 - https only (config check)
        req.add_header("Content-Type", "application/json")
        if auth and self.session_token:
            req.add_header("X-Fbx-App-Auth", self.session_token)
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode("utf-8"))
            except Exception:
                raise FreeboxError(f"HTTP {exc.code} from {url}") from exc
            finally:
                exc.close()
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise FreeboxError(f"cannot reach Freebox API at {url}: {exc}") from exc
        if not isinstance(payload, dict):
            raise FreeboxError(f"unexpected response from {url}")
        return payload

    def api_version(self) -> dict:
        return self._request("GET", f"{self.base_url}/api_version", auth=False)

    def _api(self, path: str) -> str:
        if self._api_prefix is None:
            info = self.api_version()
            major = str(info.get("api_version", "4")).split(".")[0]
            base = info.get("api_base_url", "/api/")
            self._api_prefix = f"{self.base_url}{base.rstrip('/')}/v{major}/"
        return self._api_prefix + path

    def _ok(self, payload: dict, what: str):
        if not payload.get("success"):
            code = payload.get("error_code")
            raise FreeboxApiError(f"{what} failed: {code} {payload.get('msg', '')}".strip(), code)
        return payload.get("result")

    # -- authorisation (one-time) --------------------------------------------------------
    def request_authorization(self, app_id: str, app_name: str, app_version: str, device_name: str):
        res = self._ok(self._request("POST", self._api("login/authorize/"), {
            "app_id": app_id, "app_name": app_name, "app_version": app_version, "device_name": device_name,
        }, auth=False), "authorization request")
        return res["app_token"], res["track_id"]

    def authorization_status(self, track_id: int) -> str:
        res = self._ok(self._request("GET", self._api(f"login/authorize/{track_id}"), auth=False),
                       "authorization status")
        return res.get("status", "unknown")

    # -- session ----------------------------------------------------------------------------
    def login(self, app_id: str, app_token: str) -> dict:
        res = self._ok(self._request("GET", self._api("login/"), auth=False), "challenge")
        challenge = res["challenge"]
        password = hmac.new(app_token.encode(), challenge.encode(), hashlib.sha1).hexdigest()
        res = self._ok(self._request("POST", self._api("login/session/"),
                                     {"app_id": app_id, "password": password}, auth=False), "login")
        self.session_token = res["session_token"]
        self.permissions = res.get("permissions", {}) or {}
        if not self.permissions.get("downloader"):
            raise FreeboxError("app is authorised but lacks the 'downloader' permission "
                               "(Freebox OS: Paramètres de la Freebox > Gestion des accès > Applications)")
        return self.permissions

    def logout(self) -> None:
        if self.session_token:
            try:
                self._request("POST", self._api("login/logout/"), {})
            except FreeboxError:
                pass
            self.session_token = None

    # -- downloads (read-only) ----------------------------------------------------------------
    def tasks(self) -> list[Task]:
        res = self._ok(self._request("GET", self._api("downloads/")), "list downloads") or []
        out = []
        for t in res:
            out.append(Task(
                id=int(t["id"]), name=t.get("name", ""), status=t.get("status", "unknown"),
                rx_pct=int(t.get("rx_pct", 0)), size=int(t.get("size", 0)),
                download_dir=_b64(t.get("download_dir")), type=t.get("type", ""),
                stop_ratio=t.get("stop_ratio"), error=t.get("error"),
                created_ts=t.get("created_ts") if isinstance(t.get("created_ts"), int) else None,
            ))
        return out

    def download_config(self) -> dict:
        """Download-manager settings: default download folder, watch folder, BitTorrent stop ratio."""
        res = self._ok(self._request("GET", self._api("downloads/config/")), "download config") or {}
        bt = res.get("bt") or {}
        return {
            "download_dir": _b64(res.get("download_dir")),
            "watch_dir": _b64(res.get("watch_dir")),
            "use_watch_dir": bool(res.get("use_watch_dir")),
            "stop_ratio": bt.get("stop_ratio"),  # int scaled by 100; 0 = seed until stopped by hand
        }

    def task_files(self, task_id: int) -> list[TaskFile]:
        """Files of a task; [] when the task no longer exists (removed since the task list was read)."""
        try:
            res = self._ok(self._request("GET", self._api(f"downloads/{task_id}/files")), "list task files") or []
        except FreeboxApiError as exc:
            if exc.code == "task_not_found":
                return []
            raise
        out = []
        for f in res:
            out.append(TaskFile(
                filepath=_b64(f.get("filepath")), size=int(f.get("size", 0)), rx=int(f.get("rx", 0)),
                status=f.get("status", ""), priority=f.get("priority", "normal"),
            ))
        return out


def same_remote_dir(a: str, b: str) -> bool:
    import unicodedata
    n = lambda x: unicodedata.normalize("NFC", x).rstrip("/")  # noqa: E731
    return n(a) == n(b)


def unexpected_permissions(perms: dict) -> list[str]:
    return sorted(k for k, v in perms.items() if v and k != "downloader")


def load_token(path: str) -> dict:
    from .config import ConfigError, check_file_permissions

    try:
        check_file_permissions(path, "the Freebox app token", secret=True)
    except ConfigError as exc:
        raise FreeboxError(str(exc)) from None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError as exc:
        raise FreeboxError(f"no Freebox app token at {path}; run `sudo mediaorg freebox authorize` first") from exc
    if not data.get("app_token") or not data.get("app_id"):
        raise FreeboxError(f"{path} is not a valid token file")
    return data


def save_token(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh)


def authorize_interactive(client: FreeboxClient, token_path: str, app_id: str, app_name: str,
                          app_version: str, device_name: str, out=print, sleep=time.sleep,
                          timeout_s: int = 180) -> str:
    if os.path.exists(token_path):
        raise FreeboxError(f"{token_path} already exists; delete it first to re-authorise")
    app_token, track_id = client.request_authorization(app_id, app_name, app_version, device_name)
    out("Authorisation requested. On the Freebox Server front display, confirm access for "
        f"'{app_name}' with the right arrow ('Oui').")
    deadline = time.monotonic() + timeout_s
    status = "pending"
    while time.monotonic() < deadline:
        status = client.authorization_status(track_id)
        if status != "pending":
            break
        sleep(2)
    if status != "granted":
        raise FreeboxError(f"authorisation not granted (status: {status})")
    save_token(token_path, {"app_id": app_id, "app_token": app_token, "track_id": track_id,
                            "app_name": app_name, "device_name": device_name})
    return status
