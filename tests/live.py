"""Run the real ASGI app under Uvicorn in a background thread, plus a cookie-keeping HTTP client."""
from __future__ import annotations

import http.cookiejar
import json
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import uvicorn

from novaquest.web.app import create_app


class LiveServer:
    """Uvicorn on a free local port, sharing the test database pool."""

    def __init__(self, settings, db) -> None:
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        sock.close()
        app = create_app(settings, db)
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning", lifespan="on"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        for _ in range(100):
            if self.server.started:
                break
            time.sleep(0.05)
        self.url = f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(5)


class Headers(dict):
    """Case-insensitive response headers."""

    def __init__(self, items) -> None:
        super().__init__({k.lower(): v for k, v in items})

    def __getitem__(self, key: str) -> str:
        return super().__getitem__(key.lower())

    def get(self, key: str, default=None):
        return super().get(key.lower(), default)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


class Client:
    """A browser-like client: keeps cookies, reads the CSRF token from pages, follows redirects by default."""

    def __init__(self, base: str, follow: bool = True) -> None:
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        handlers = [urllib.request.HTTPCookieProcessor(self.jar), urllib.request.ProxyHandler({})]
        if not follow:
            handlers.append(NoRedirect())
        self.opener = urllib.request.build_opener(*handlers)
        self.csrf = ""

    def _do(self, req: urllib.request.Request) -> tuple[int, str, dict, str]:
        try:
            with self.opener.open(req, timeout=30) as r:
                body = r.read()
                status, headers, url = r.status, Headers(r.headers.items()), r.geturl()
        except urllib.error.HTTPError as e:
            body, status, headers, url = e.read(), e.code, Headers(e.headers.items()), req.full_url
        ctype = headers.get("Content-Type", "")
        text = body.decode("utf-8", "replace") if ("text" in ctype or "json" in ctype or "javascript" in ctype) else ""
        m = re.search(r'name="csrf" content="([^"]+)"', text)
        if m:
            self.csrf = m.group(1)
        self.last_bytes = body
        return status, text, headers, url.replace(self.base, "")

    def get(self, path: str, headers: dict | None = None):
        return self._do(urllib.request.Request(self.base + path, headers=headers or {}))

    def post(self, path: str, data: dict | None = None, csrf: bool = True):
        data = dict(data or {})
        if csrf:
            data.setdefault("csrf", self.csrf)
        return self._do(urllib.request.Request(self.base + path, data=urllib.parse.urlencode(data, doseq=True).encode()))

    def post_json(self, path: str, payload: dict, csrf: bool = True):
        headers = {"Content-Type": "application/json"}
        if csrf:
            headers["X-CSRF-Token"] = self.csrf
        status, text, h, url = self._do(urllib.request.Request(self.base + path, data=json.dumps(payload).encode(), headers=headers))
        return status, (json.loads(text) if text.startswith("{") else {}), h

    def post_multipart(self, path: str, fields: dict, files: dict):
        boundary = "----nqtest" + str(time.time_ns())
        parts = []
        for k, v in {**fields, "csrf": self.csrf}.items():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
        for k, (name, data, ctype) in (files.items() if isinstance(files, dict) else files):
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; filename="{name}"\r\nContent-Type: {ctype}\r\n\r\n'.encode()
                         + data + b"\r\n")
        body = b"".join(parts) + f"--{boundary}--\r\n".encode()
        return self._do(urllib.request.Request(self.base + path, data=body,
                                               headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}))

    def login(self, email: str, password: str):
        self.get("/login")
        return self.post("/login", {"email": email, "password": password})

    def login_student(self, code: str, klass: str, roll: str, pin: str):
        self.get("/login/student")
        return self.post("/login/student", {"school": code, "klass": klass, "roll": roll, "pin": pin})
