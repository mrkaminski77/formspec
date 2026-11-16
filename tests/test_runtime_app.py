"""Tests for standalone packaged AppRuntime, empty-DB auto-init, and pluggable auth."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from formspec.generators.db_init import generate_db_init_sql
from formspec.ide.server import IDEApp
from formspec.runtime import AppRuntime, resolve_runtime_dsn


def _extract_token(set_cookie_header: str) -> str:
    return set_cookie_header.split(";", 1)[0].split("=", 1)[1]


class RuntimeAppTests(unittest.TestCase):
    def test_empty_db_auto_initializes_on_launch_and_skips_when_non_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            studio_db = tmp_dir / "studio.db"
            studio = IDEApp(dsn=f"sqlite:///{studio_db}", secret="studio-sec")
            try:
                _, _, cookie = studio.handle_api(
                    "POST",
                    "/api/auth/setup",
                    {"username": "superuser", "password": "strong-password-123"},
                )
                tok = _extract_token(cookie or "")
                studio.handle_api(
                    "POST",
                    "/api/entities",
                    {
                        "name": "task",
                        "label": "Task",
                        "attributes": [{"name": "title", "fspec_type": "text", "nullable": False}],
                    },
                    session_token=tok,
                )
                # Grant guest write/read on task for test login
                studio.handle_api(
                    "POST",
                    "/api/rbac/assign",
                    {"role": "guest", "permission": "task:write", "granted": True},
                    session_token=tok,
                )
                studio.handle_api(
                    "POST",
                    "/api/forms/from-entity",
                    {"entity": "task", "form_type": "detail"},
                    session_token=tok,
                )

                app_data = tmp_dir / "app_data"
                app_data.mkdir()
                with studio.open_adapter() as adp:
                    (app_data / "init_db.sqlite.sql").write_text(
                        generate_db_init_sql(adp, "sqlite"), encoding="utf-8"
                    )
            finally:
                studio.git.cleanup()
                studio.packager.cleanup()

            # Launch packaged AppRuntime on a brand-new empty SQLite database
            runtime_db = tmp_dir / "runtime_app.db"
            self.assertFalse(runtime_db.exists())

            old_admin_pw = os.environ.get("FORMSPEC_ADMIN_PASSWORD")
            os.environ["FORMSPEC_ADMIN_PASSWORD"] = "admin-pw-123"
            try:
                rt1 = AppRuntime(
                    dsn=f"sqlite:///{runtime_db}",
                    app_data_dir=app_data,
                    secret="rt-secret",
                )
                self.assertTrue(rt1.db_initialized_on_startup)

                # Seed a sample row in `task` and add a local `guest` user
                conn = sqlite3.connect(runtime_db)
                conn.execute("INSERT INTO task (id, title) VALUES (1, 'Keep Me Across Restart')")
                conn.commit()
                conn.close()

                # Re-launching against non-empty DB must skip init script & preserve existing rows
                rt2 = AppRuntime(
                    dsn=f"sqlite:///{runtime_db}",
                    app_data_dir=app_data,
                    secret="rt-secret",
                )
                self.assertFalse(rt2.db_initialized_on_startup)

                conn = sqlite3.connect(runtime_db)
                row = conn.execute("SELECT title FROM task WHERE id = 1").fetchone()
                conn.close()
                self.assertEqual(row, ("Keep Me Across Restart",))

                # Unauthenticated anon visitor boots cleanly onto `home` entry form
                _, boot_anon, _ = rt2.handle_api("GET", "/api/app/bootstrap")
                self.assertEqual(boot_anon["user"]["effective_role"], "anon")
                self.assertEqual(boot_anon["entry_form"], "home")

                _, home_render, _ = rt2.handle_api(
                    "POST", "/api/app/render", {"form_name": "home"}
                )
                self.assertIn("Home", home_render["content"])

                # Local DB auth login succeeds and submits stored Node.js upsert action
                code, _, login_cookie = rt2.handle_api(
                    "POST",
                    "/auth/login",
                    {"username": "admin", "password": "admin-pw-123"},
                )
                self.assertEqual(code, 200)
                app_tok = _extract_token(login_cookie or "")

                code, sub_res, _ = rt2.handle_api(
                    "POST",
                    "/api/app/submit",
                    {
                        "entity": "task",
                        "server_action": "task_upsert",
                        "payload": {"id": None, "title": "Created From Packaged App"},
                    },
                    session_token=app_tok,
                )
                self.assertEqual(code, 200)
                self.assertEqual(sub_res["action"], "created")
                self.assertEqual(sub_res["record"]["title"], "Created From Packaged App")

                # Azure EasyAuth provider resolves identity & roles from X-MS-CLIENT-PRINCIPAL
                rt_easy = AppRuntime(
                    dsn=f"sqlite:///{runtime_db}",
                    app_data_dir=app_data,
                    auth_provider="easyauth",
                    secret="rt-secret",
                )
                principal = {
                    "userDetails": "alice@contoso.com",
                    "userRoles": ["authenticated", "guest"],
                }
                encoded = base64.b64encode(json.dumps(principal).encode("utf-8")).decode("ascii")
                _, boot_ea, _ = rt_easy.handle_api(
                    "GET",
                    "/api/app/bootstrap",
                    headers={"X-MS-CLIENT-PRINCIPAL": encoded},
                )
                self.assertEqual(boot_ea["user"]["username"], "alice@contoso.com")
                self.assertEqual(boot_ea["user"]["effective_role"], "guest")
                self.assertFalse(boot_ea["supports_login"])
            finally:
                if old_admin_pw is None:
                    os.environ.pop("FORMSPEC_ADMIN_PASSWORD", None)
                else:
                    os.environ["FORMSPEC_ADMIN_PASSWORD"] = old_admin_pw

    def test_resolve_runtime_dsn_uses_sqlite_unless_connection_string_set(self) -> None:
        old = os.environ.pop("DATABASE_CONNECTION_STRING", None)
        old_fs = os.environ.pop("FORMSPEC_DATABASE_URL", None)
        old_db = os.environ.pop("DATABASE_URL", None)
        try:
            self.assertEqual(resolve_runtime_dsn(), "sqlite:///./data/app.db")
            os.environ["DATABASE_CONNECTION_STRING"] = "postgresql://user:pw@db/prod"
            self.assertEqual(resolve_runtime_dsn(), "postgresql://user:pw@db/prod")
        finally:
            if old is not None:
                os.environ["DATABASE_CONNECTION_STRING"] = old
            else:
                os.environ.pop("DATABASE_CONNECTION_STRING", None)
            if old_fs is not None:
                os.environ["FORMSPEC_DATABASE_URL"] = old_fs
            if old_db is not None:
                os.environ["DATABASE_URL"] = old_db


if __name__ == "__main__":
    unittest.main()
