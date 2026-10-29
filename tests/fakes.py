"""Local HTTP fakes for the Freebox OS API and the tMM API (documented request/response shapes)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()


class _Server:
    def __init__(self, handler_cls):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        self.httpd.fake = self
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"null")


class FakeFreebox(_Server):
    APP_TOKEN = "app-token-secret"
    CHALLENGE = "challenge-123"

    def __init__(self, tasks=(), files=None, permissions=None, auth_states=("pending", "granted"),
                 download_dir="/Freebox/Downloads", stop_ratio=10, on_stop="stopped"):
        self.download_dir = download_dir
        self.stop_ratio = stop_ratio
        # PUT downloads/{id} {"status": "stopped"}: the task becomes "stopped" ("stopped"), stays "stopping"
        # ("stopping"), the call fails with an API error ("refuse", or only for the ids in refuse_ids), or the
        # answer is cut short ("truncate")
        self.on_stop = on_stop
        self.refuse_ids: set[int] = set()
        self.tasks = list(tasks)
        self.files = files or {}
        self.permissions = permissions if permissions is not None else {"downloader": True, "settings": False}
        self.auth_states = list(auth_states)
        self.requests = []
        super().__init__(FreeboxHandler)


class FreeboxHandler(_Handler):
    def _authed(self):
        return self.headers.get("X-Fbx-App-Auth") == "session-ok"

    def do_GET(self):
        fb = self.server.fake
        fb.requests.append(("GET", self.path))
        if self.path == "/api_version":
            return self._send(200, {"api_version": "15.0", "api_base_url": "/api/", "device_name": "Freebox Server",
                                    "https_available": True})
        if self.path == "/api/v15/login/":
            return self._send(200, {"success": True, "result": {"logged_in": False, "challenge": fb.CHALLENGE}})
        if self.path.startswith("/api/v15/login/authorize/"):
            st = fb.auth_states.pop(0) if len(fb.auth_states) > 1 else fb.auth_states[0]
            return self._send(200, {"success": True, "result": {"status": st, "challenge": fb.CHALLENGE}})
        if not self._authed():
            return self._send(403, {"success": False, "error_code": "auth_required", "msg": "Non autorisé"})
        if self.path == "/api/v15/downloads/config/":
            return self._send(200, {"success": True, "result": {
                "download_dir": b64(fb.download_dir), "watch_dir": b64("/Freebox/Watch"),
                "use_watch_dir": False, "bt": {"stop_ratio": fb.stop_ratio}}})
        if self.path == "/api/v15/downloads/":
            return self._send(200, {"success": True, "result": fb.tasks})
        if self.path.startswith("/api/v15/downloads/") and self.path.endswith("/files"):
            tid = int(self.path.split("/")[4])
            return self._send(200, {"success": True, "result": fb.files.get(tid, [])})
        return self._send(404, {"success": False, "error_code": "invalid_request"})

    def do_POST(self):
        fb = self.server.fake
        body = self._body()
        fb.requests.append(("POST", self.path, body))
        if self.path == "/api/v15/login/authorize/":
            return self._send(200, {"success": True, "result": {"app_token": fb.APP_TOKEN, "track_id": 42}})
        if self.path == "/api/v15/login/session/":
            want = hmac.new(fb.APP_TOKEN.encode(), fb.CHALLENGE.encode(), hashlib.sha1).hexdigest()
            if body.get("password") != want:
                return self._send(403, {"success": False, "error_code": "invalid_token", "msg": "bad",
                                        "result": {"challenge": fb.CHALLENGE}})
            return self._send(200, {"success": True, "result": {"session_token": "session-ok",
                                                                 "permissions": fb.permissions}})
        if self.path == "/api/v15/login/logout/":
            return self._send(200, {"success": True})
        return self._send(404, {"success": False, "error_code": "invalid_request"})

    def do_PUT(self):
        fb = self.server.fake
        body = self._body()
        fb.requests.append(("PUT", self.path, body))
        if not self._authed():
            return self._send(403, {"success": False, "error_code": "auth_required", "msg": "Non autorisé"})
        parts = self.path.strip("/").split("/")  # api, v15, downloads, <id>
        if len(parts) == 4 and parts[:3] == ["api", "v15", "downloads"] and parts[3].isdigit():
            task = next((t for t in fb.tasks if t["id"] == int(parts[3])), None)
            if task is None:
                return self._send(404, {"success": False, "error_code": "task_not_found", "msg": "Tâche inconnue"})
            if body != {"status": "stopped"}:
                return self._send(400, {"success": False, "error_code": "invalid_request"})
            if fb.on_stop == "refuse" or task["id"] in fb.refuse_ids:
                return self._send(403, {"success": False, "error_code": "insufficient_rights", "msg": "Accès refusé"})
            if fb.on_stop == "truncate":
                task["status"] = "stopped"
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "500")
                self.end_headers()
                self.wfile.write(b'{"success": tr')
                self.wfile.flush()
                self.close_connection = True
                return None
            task["status"] = "stopped" if fb.on_stop == "stopped" else "stopping"
            return self._send(200, {"success": True, "result": task})
        return self._send(404, {"success": False, "error_code": "invalid_request"})


class FakeTmm(_Server):
    API_KEY = "test-api-key"

    def __init__(self, busy=False, system=True, docs=True):
        self.busy = busy
        self.system = system
        self.docs = docs
        self.posts = []
        super().__init__(TmmHandler)


class TmmHandler(_Handler):
    # as tMM 5.3.4's TmmHttpServer: a path without a context is 404 before any key check; a context answers
    # 403 {"message": "Invalid API key"} to a missing or wrong key
    def _key_ok(self):
        if self.headers.get("api-key") != self.server.fake.API_KEY:
            self._send(403, {"message": "Invalid API key"})
            return False
        return True

    def do_GET(self):
        fake = self.server.fake
        if not ((self.path == "/api/docs" and fake.docs) or (self.path == "/api/system" and fake.system)):
            return self._send(404, {"message": "not found"})
        if not self._key_ok():
            return
        if self.path == "/api/docs" and fake.docs:
            return self._send(200, {"openapi": "3.0.1", "paths": {
                "/api/movie": {"post": {}}, "/api/tvshow": {"post": {}}, "/api/movieset": {"post": {}},
                "/api/system": {"get": {}, "post": {}}}})
        if self.path == "/api/system" and fake.system:
            return self._send(200, {"message": "OK", "data": {"version": "5.3.5", "state": "RUNNING",
                                                               "busy": fake.busy, "mainTaskRunning": fake.busy,
                                                               "queuedMainTasks": 1 if fake.busy else 0}})
        return self._send(404, {"message": "not found"})

    def do_POST(self):
        if not self._key_ok():
            return
        self.server.fake.posts.append((self.path, self._body()))
        return self._send(200, {"message": "OK"})
