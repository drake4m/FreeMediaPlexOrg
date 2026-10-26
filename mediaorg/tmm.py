"""tinyMediaManager HTTP API client (https://www.tinymediamanager.org/docs/http-api).

The API queues commands and runs them asynchronously; a 2xx answer means "accepted",
not "finished".  Separate calls are queued and run one after another, so no delay is needed
between the TV-show and movie calls.  `status()` (GET /api/system) needs tMM >= 5.3.5; on
older versions it returns None and moves are not deferred.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from .config import Tmm, uses_paths


class TmmUnavailable(Exception):
    """tMM is not listening (not running).  Not fatal for file moves."""


class TmmError(Exception):
    pass


def read_api_key(path: str) -> str:
    from .config import ConfigError, check_file_permissions

    try:
        check_file_permissions(path, "the tMM API key", secret=True)
    except ConfigError as exc:
        raise TmmError(str(exc)) from None
    with open(path, encoding="utf-8") as fh:
        key = fh.read().strip()
    if not key:
        raise TmmError(f"{path} is empty")
    return key


def to_tmm_path(host_path: str, path_map) -> str | None:
    host_path = os.path.normpath(host_path)
    for host, inner in path_map:  # sorted longest first by config loader
        if host_path == host or host_path.startswith(host + "/"):
            return inner + host_path[len(host):]
    return None


def render(commands, paths: list[str]) -> list[dict]:
    """Replace the string "{paths}" anywhere in the command templates by the path list."""
    def sub(v):
        if v == "{paths}":
            return list(paths)
        if isinstance(v, dict):
            return {k: sub(x) for k, x in v.items()}
        if isinstance(v, list):
            return [sub(x) for x in v]
        return v
    return [sub(c) for c in commands]


class TmmClient:
    def __init__(self, cfg: Tmm, api_key: str, opener=None):
        self.cfg = cfg
        self.base = cfg.base_url.rstrip("/") + "/api"
        self.api_key = api_key
        self._opener = opener or urllib.request.build_opener()

    def _call(self, method: str, path: str, body=None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"{self.base}/{path}", data=data, method=method)  # noqa: S310 - http(s), config check
        req.add_header("api-key", self.api_key)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with self._opener.open(req, timeout=self.cfg.timeout_seconds) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            exc.close()
            raise TmmError(f"HTTP {exc.code} on {path}: {detail}") from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, ConnectionRefusedError) or "refused" in str(reason).lower():
                raise TmmUnavailable(f"tMM not listening at {self.base}") from exc
            raise TmmError(f"cannot reach tMM at {self.base}: {reason}") from exc
        except (TimeoutError, OSError) as exc:
            raise TmmError(f"tMM request failed: {exc}") from exc
        try:
            return json.loads(raw) if raw.strip() else {}
        except ValueError:
            return {"raw": raw[:300]}

    def status(self) -> dict | None:
        """System status, or None when this tMM version has no /api/system (404)."""
        try:
            res = self._call("GET", "system")
        except TmmError as exc:
            if "HTTP 404" in str(exc):
                return None
            raise
        return res.get("data", res)

    def api_paths(self) -> set[str] | None:
        """Endpoint names from the OpenAPI description (GET /api/docs, tMM >= 5.3.3), e.g. {"movie",
        "tvshow"}; None when this tMM has no /api/docs."""
        try:
            doc = self._call("GET", "docs")
        except TmmError as exc:
            if "HTTP 404" in str(exc):
                return None
            raise
        paths = doc.get("paths") if isinstance(doc, dict) else None
        if not isinstance(paths, dict):
            raise TmmError("unrecognised /api/docs answer (no OpenAPI 'paths')")
        out = set()
        for p in paths:
            parts = [x for x in str(p).split("/") if x]
            if parts and parts[0] == "api":
                parts = parts[1:]
            if parts:
                out.add(parts[0])
        return out

    def send(self, endpoint: str, commands: list[dict]) -> dict:
        return self._call("POST", endpoint, commands)

    def refresh(self, kind: str, host_paths: list[str]) -> dict:
        """The calls after moves; kind is 'movie' or 'tvshow'.

        The default templates update the whole library, then scrape and rename what that update found
        (config.DEFAULT_AFTER_MOVE_*); a template using "{paths}" gets the moved folders translated to
        tMM's container paths.
        """
        tmpl = self.cfg.after_move_movie if kind == "movie" else self.cfg.after_move_tvshow
        endpoint = self.cfg.movie_endpoint if kind == "movie" else self.cfg.tvshow_endpoint
        inner: list[str] = []
        if uses_paths(tmpl):
            inner = sorted({p for p in (to_tmm_path(h, self.cfg.path_map) for h in host_paths) if p})
            if not inner:
                raise TmmError(f"none of {host_paths} is covered by tmm.path_map")
        return self.send(endpoint, render(tmpl, inner))

    def full_sync(self, kind: str) -> dict:
        tmpl = self.cfg.full_sync_movie if kind == "movie" else self.cfg.full_sync_tvshow
        endpoint = self.cfg.movie_endpoint if kind == "movie" else self.cfg.tvshow_endpoint
        return self.send(endpoint, render(tmpl, []))
