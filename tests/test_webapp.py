from __future__ import annotations

import re
from pathlib import Path
import tempfile
import unittest

from master_list_creation.webapp import create_app
from test_store import anime, snapshot


class WebAppTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        seed = snapshot(root / "seed.json", "source-1", {1: anime(1, "Alpha")})
        self.app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "test-session-secret",
                "ADMIN_EMAIL": "ege@rousoftware.com",
                "ADMIN_PASSWORD": "random-test-password",
                "APP_TOKEN": "embedded-test-token",
                "DATA_DIR": str(root / "data"),
                "SEED_LIST_PATH": str(seed),
                "SESSION_COOKIE_SECURE": False,
            }
        )
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _login(self) -> None:
        page = self.client.get("/login")
        token = re.search(rb'name="csrf_token" value="([^"]+)"', page.data).group(1)
        response = self.client.post(
            "/login",
            data={
                "csrf_token": token.decode(),
                "email": "ege@rousoftware.com",
                "password": "random-test-password",
            },
        )
        self.assertEqual(response.status_code, 302)

    def test_admin_login_browser_and_update_queue(self) -> None:
        self.assertEqual(self.client.get("/").status_code, 302)
        self._login()
        browser = self.client.get("/anime?q=Alpha")
        self.assertEqual(browser.status_code, 200)
        self.assertIn(b"Alpha", browser.data)
        with self.client.session_transaction() as browser_session:
            csrf = browser_session["csrf_token"]
        response = self.client.post("/admin/updates", data={"csrf_token": csrf})
        self.assertEqual(response.status_code, 302)
        store = self.app.extensions["version_store"]
        self.assertEqual(store.recent_jobs()[0]["status"], "pending")

    def test_public_api_requires_token_and_serves_contract(self) -> None:
        self.assertEqual(self.client.get("/api/v1/anime-list/version").status_code, 401)
        headers = {"Authorization": "Bearer embedded-test-token"}
        metadata = self.client.get("/api/v1/anime-list/version", headers=headers)
        self.assertEqual(metadata.status_code, 200)
        self.assertEqual(metadata.json["version"], 1)
        update = self.client.get(
            "/api/v1/anime-list/updates?fromVersion=1", headers=headers
        )
        self.assertEqual(update.status_code, 200)
        self.assertEqual(update.json["schemaVersion"], 1)
        self.assertEqual(update.json["upserts"], [])
        full = self.client.get("/api/v1/anime-list", headers=headers)
        self.assertEqual(full.status_code, 200)
        self.assertEqual(full.headers["X-Anime-List-Version"], "1")
        full.close()
        update.close()

    def test_security_headers_and_csrf_rejection(self) -> None:
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self._login()
        self.assertEqual(self.client.post("/admin/updates").status_code, 400)


if __name__ == "__main__":
    unittest.main()
