"""公网入口、口令、文件隔离与连接限制：不请求模型或外部工具。"""

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from src.config import PROJECT_ROOT
from src.index import create_mcp_clients
from src.web_app import create_app
from src.web_security import COOKIE_NAME, WebSettings, canonical_origin

ORIGIN = "https://mellow.example"
PASSWORD = "unit-test-only-long-password"


class SettingsTests(unittest.TestCase):
    def test_public_mode_rejects_missing_password_and_insecure_url(self):
        for url, password in [(ORIGIN, ""), ("http://mellow.example", PASSWORD), (ORIGIN + "/page", PASSWORD)]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                WebSettings(public_url=url, access_password=password)
        self.assertFalse(WebSettings().public)
        self.assertTrue(WebSettings(public_url="http://localhost:8765", access_password=PASSWORD).public)
        self.assertEqual(canonical_origin("https://MELLOW.example:443/"), ORIGIN)

    def test_render_url_is_used_without_committing_a_domain(self):
        with patch.dict(os.environ, {"WEB_PUBLIC_URL": "", "RENDER_EXTERNAL_URL": ORIGIN, "WEB_ACCESS_PASSWORD": PASSWORD}):
            self.assertEqual(WebSettings.from_env().public_url, ORIGIN)

    def test_public_file_tools_are_scoped_and_private_fetch_override_is_disabled(self):
        with TemporaryDirectory() as temp, patch.dict(os.environ, {
            "FETCH_ALLOW_PRIVATE_IPS": "true", "MCP_FETCH_COMMAND": "mcp-server-fetch",
            "MCP_FILESYSTEM_COMMAND": "mcp-server-filesystem",
        }):
            root = Path(temp).resolve()
            clients = create_mcp_clients(file_root=root, allow_private_ips=False)
            self.assertEqual(clients[0].args, [])
            self.assertEqual(clients[1].args, [str(root)])
            self.assertEqual(clients[1].cwd, root)
            self.assertNotIn(str(PROJECT_ROOT), clients[1].args)


class PublicDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = WebSettings(public_url=ORIGIN, access_password=PASSWORD, workspace_root=Path(self.temp.name) / "workspaces", max_connections=1)
        self.app = create_app(settings=self.settings)
        self.client = TestClient(self.app, base_url=ORIGIN, follow_redirects=False)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def login(self, password=PASSWORD):
        return self.client.post("/auth/login", data={"password": password}, headers={"origin": ORIGIN})

    def socket(self, *, origin=ORIGIN, cookie=None, url=None):
        headers = {"origin": origin}
        if cookie:
            headers["cookie"] = f"{COOKIE_NAME}={cookie}"
        return self.client.websocket_connect(url or "wss://mellow.example/ws", headers=headers)

    def owner(self):
        return self.app.state.logins.get(self.client.cookies.get(COOKIE_NAME))

    def test_login_gates_home_and_websocket_but_not_health(self):
        self.assertEqual(self.client.get("/").headers["location"], "/auth/login")
        self.assertEqual(self.client.get("/health").json(), {"status": "ok"})
        self.assertEqual(self.client.get("/auth/login").status_code, 200)
        with self.assertRaises(WebSocketDisconnect) as caught, self.socket() as socket:
            socket.receive_json()
        self.assertEqual(caught.exception.code, 4401)
        response = self.login()
        self.assertEqual(response.status_code, 303)
        cookie = response.headers["set-cookie"].lower()
        self.assertIn("httponly", cookie)
        self.assertIn("secure", cookie)
        self.assertIn("samesite=strict", cookie)
        self.assertNotIn(PASSWORD, cookie)
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_password_csrf_and_login_rate_limit(self):
        self.assertEqual(self.client.post("/auth/login", data={"password": PASSWORD}, headers={"origin": "https://other.example"}).status_code, 403)
        for _ in range(5):
            response = self.login("wrong")
            self.assertEqual(response.status_code, 401)
            self.assertNotIn(PASSWORD, response.text)
        self.assertEqual(self.login().status_code, 429)
        for address in self.app.state.logins.failures:
            self.app.state.logins.failures[address] = [time.time() - 61]
        self.assertEqual(self.login().status_code, 303)

    def test_large_login_body_is_bounded(self):
        response = self.client.post("/auth/login", content="password=" + "a" * 5000, headers={"origin": ORIGIN})
        self.assertEqual(response.status_code, 413)

    def test_proxy_terminated_https_origin_is_accepted_without_proxy_header_trust(self):
        self.login()
        token = self.client.cookies.get(COOKIE_NAME)
        with patch("src.web_app.create_retriever") as retriever, self.socket(cookie=token, url="ws://mellow.example/ws") as socket:
            ready = socket.receive_json()
            self.assertEqual(ready["files_url"], "/files")
            self.assertTrue(ready["authenticated"])
            self.assertEqual(socket.receive_json()["state"], "idle")
            retriever.assert_not_called()
        with self.assertRaises(WebSocketDisconnect), self.socket(origin="https://other.example"):
            pass

    def test_connection_limit_and_cleanup(self):
        self.login()
        with self.socket() as first:
            first.receive_json()
            first.receive_json()
            with self.assertRaises(WebSocketDisconnect) as caught, self.socket() as second:
                second.receive_json()
            self.assertEqual(caught.exception.code, 1013)
        with self.socket() as reopened:
            self.assertEqual(reopened.receive_json()["type"], "ready")

    def test_expired_login_cannot_open_http_or_websocket(self):
        self.login()
        self.owner().expires_at = time.time() - 1
        self.assertEqual(self.client.get("/").status_code, 303)
        with self.assertRaises(WebSocketDisconnect), self.socket() as socket:
            socket.receive_json()

    def test_websocket_lease_expires_and_closes(self):
        self.login()
        self.owner().expires_at = time.time() + .25
        with self.socket() as socket:
            socket.receive_json()
            socket.receive_json()
            with self.assertRaises(WebSocketDisconnect) as caught:
                socket.receive_json()
            self.assertEqual(caught.exception.code, 4401)

    def test_download_is_attached_and_owned_by_this_login(self):
        self.login()
        first = self.owner()
        root = self.app.state.logins.workspace(first)
        root.mkdir(parents=True)
        (root / "成果.html").write_text("<script>alert('sample')</script>", encoding="utf-8")
        (root / "A&B.txt").write_text("first owner", encoding="utf-8")
        listing = self.client.get("/files")
        self.assertIn("A&amp;B.txt", listing.text)
        file = self.client.get("/files/成果.html")
        self.assertEqual(file.status_code, 200)
        self.assertTrue(file.headers["content-disposition"].startswith("attachment;"))
        self.assertEqual(file.headers["content-type"], "application/octet-stream")
        self.login()
        self.assertNotEqual(self.owner().workspace_id, first.workspace_id)
        self.assertNotIn("A&amp;B.txt", self.client.get("/files").text)
        self.assertEqual(self.client.get("/files/成果.html").status_code, 404)

    def test_download_traversal_and_symlink_escape_are_rejected(self):
        self.login()
        root = self.app.state.logins.workspace(self.owner())
        root.mkdir(parents=True)
        outside = Path(self.temp.name) / "private.txt"
        outside.write_text("must stay private", encoding="utf-8")
        for path in ["/files/%2e%2e%2f%2e%2e%2fprivate.txt", "/files/%2fprivate.txt"]:
            response = self.client.get(path)
            self.assertEqual(response.status_code, 404)
            self.assertNotIn("must stay private", response.text)
        try:
            (root / "escape.txt").symlink_to(outside)
        except (OSError, NotImplementedError):
            return  # Windows 非管理员账户可能无创建符号链接的权限。
        self.assertEqual(self.client.get("/files/escape.txt").status_code, 404)

    def test_logout_revokes_cookie_and_closes_active_socket(self):
        self.login()
        token = self.client.cookies.get(COOKIE_NAME)
        with self.socket() as socket:
            socket.receive_json()
            socket.receive_json()
            self.assertEqual(self.client.post("/auth/logout", headers={"origin": "https://other.example"}).status_code, 403)
            response = self.client.post("/auth/logout", headers={"origin": ORIGIN})
            self.assertEqual(response.status_code, 303)
            self.assertIsNone(self.app.state.logins.get(token))
            with self.assertRaises(WebSocketDisconnect) as caught:
                socket.receive_json()
            self.assertEqual(caught.exception.code, 4401)
        self.assertEqual(self.client.get("/").status_code, 303)
