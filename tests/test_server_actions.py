"""End-to-end tests for generated Python and Node.js server actions."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from formspec.cli import main as cli_main
from formspec.generators import (
    CLIENT_MODULE_NAME,
    HEADER_MODULE_NAME,
    generate_detail_client_action,
    generate_for_entity,
    generate_list_client_action,
    get_client_module_code,
    get_header_client_module_code,
)


def _seed_full_db(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE entity (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL,
            display_attribute TEXT
        );

        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL UNIQUE
        );
        CREATE TABLE roles (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE
        );
        CREATE TABLE permissions (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE
        );
        CREATE TABLE user_roles (
            user_id INTEGER NOT NULL,
            role_id INTEGER NOT NULL,
            PRIMARY KEY (user_id, role_id)
        );
        CREATE TABLE role_permissions (
            role_id INTEGER NOT NULL,
            permission_id INTEGER NOT NULL,
            PRIMARY KEY (role_id, permission_id)
        );

        CREATE TABLE department (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL
        );
        CREATE TABLE user (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            email TEXT NOT NULL,
            department_id INTEGER NOT NULL,
            active BOOLEAN NOT NULL DEFAULT 1
        );

        INSERT INTO entity (slug, display_name, display_attribute) VALUES
            ('user', 'User', NULL),
            ('department', 'Department', 'name');

        INSERT INTO department (id, name) VALUES
            (1, 'Engineering'),
            (2, 'Sales');

        INSERT INTO users (id, username) VALUES
            (10, 'alice_admin'),
            (20, 'bob_self'),
            (30, 'carol_viewer'),
            (40, 'dave_guest'),
            (50, 'eve_username_only');

        INSERT INTO roles (id, name) VALUES
            (1, 'admin'),
            (2, 'self_service'),
            (3, 'viewer'),
            (4, 'username_only');

        INSERT INTO permissions (id, name) VALUES
            (1, 'user:write'),
            (2, 'user:read'),
            (3, 'user:write_username');

        INSERT INTO user_roles (user_id, role_id) VALUES
            (10, 1),
            (20, 2),
            (30, 3),
            (50, 4);

        INSERT INTO role_permissions (role_id, permission_id) VALUES
            (1, 1), -- admin -> user:write
            (2, 2), -- self_service -> user:read
            (2, 3), -- self_service -> user:write_username
            (3, 2), -- viewer -> user:read
            (4, 3); -- username_only -> user:write_username only (no entity-level read)

        INSERT INTO user (id, username, email, department_id, active) VALUES
            (100, 'jdoe', 'jdoe@example.com', 1, 1);
        """
    )
    conn.commit()
    conn.close()


def _load_python_module(path: Path):
    spec = importlib.util.spec_from_file_location("generated_user_action", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ServerActionsTests(unittest.TestCase):
    def test_python_and_node_server_actions_enforce_permissions_and_populate_options(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            db_path = tmp_dir / "app.db"
            out_dir = tmp_dir / "out"
            _seed_full_db(db_path)

            artifacts = generate_for_entity(
                dsn=f"sqlite:///{db_path}",
                entity_slug="user",
                out_dir=out_dir,
                target="both",
            )
            py_action_path = out_dir / "user_action.py"
            node_action_path = out_dir / "user_action.mjs"
            self.assertTrue(artifacts.fspec_path.exists())
            self.assertTrue(py_action_path.exists())
            self.assertTrue(node_action_path.exists())

            py_mod = _load_python_module(py_action_path)
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            try:
                # 1. Admin loads record + _id options sorted by display name
                loaded = py_mod.load_form(conn, user_id=10, record_id=100)
                self.assertEqual(loaded["permissions"]["entity"], "write")
                self.assertTrue(loaded["permissions"]["can_submit"])
                self.assertEqual(
                    loaded["options"]["department_id"],
                    [{"id": 1, "name": "Engineering"}, {"id": 2, "name": "Sales"}],
                )
                self.assertEqual(loaded["record"]["username"], "jdoe")

                # 2. Admin inserts a new user
                created = py_mod.submit_form(
                    conn,
                    user_id=10,
                    payload={
                        "username": "asmith",
                        "email": "asmith@example.com",
                        "department_id": 2,
                        "active": True,
                    },
                )
                self.assertEqual(created["action"], "created")
                self.assertEqual(created["record"]["username"], "asmith")

                # 3. Invalid FK reference fails with 422
                with self.assertRaises(py_mod.ValidationError) as ctx_fk:
                    py_mod.submit_form(
                        conn,
                        user_id=10,
                        payload={
                            "username": "badfk",
                            "email": "bad@example.com",
                            "department_id": 999,
                        },
                    )
                self.assertEqual(ctx_fk.exception.status_code, 422)

                # 4. Missing required field fails with 422
                with self.assertRaises(py_mod.ValidationError) as ctx_req:
                    py_mod.submit_form(
                        conn,
                        user_id=10,
                        payload={"email": "nouser@example.com", "department_id": 1},
                    )
                self.assertEqual(ctx_req.exception.status_code, 422)

                # 5. self_service can update username (user:write_username)
                updated = py_mod.submit_form(
                    conn,
                    user_id=20,
                    record_id=100,
                    payload={"username": "jdoe_updated"},
                )
                self.assertEqual(updated["action"], "updated")
                self.assertEqual(updated["record"]["username"], "jdoe_updated")

                # 6. self_service CANNOT update email (only has user:read)
                with self.assertRaises(py_mod.PermissionDeniedError) as ctx_attr:
                    py_mod.submit_form(
                        conn,
                        user_id=20,
                        record_id=100,
                        payload={"email": "hack@example.com"},
                    )
                self.assertEqual(ctx_attr.exception.status_code, 403)

                # 7. viewer can load but CANNOT submit
                viewer_load = py_mod.load_form(conn, user_id=30, record_id=100)
                self.assertFalse(viewer_load["permissions"]["can_submit"])
                with self.assertRaises(py_mod.PermissionDeniedError) as ctx_viewer:
                    py_mod.submit_form(
                        conn,
                        user_id=30,
                        record_id=100,
                        payload={"username": "nope"},
                    )
                self.assertEqual(ctx_viewer.exception.status_code, 403)

                # 8. guest (no roles) CANNOT load or submit
                with self.assertRaises(py_mod.PermissionDeniedError):
                    py_mod.load_form(conn, user_id=40, record_id=100)
                with self.assertRaises(py_mod.PermissionDeniedError):
                    py_mod.submit_form(conn, user_id=40, payload={"username": "x"})

                # 9. username_only (only user:write_username) sees only username & no department_id options,
                #    cannot INSERT when required uneditable fields exist, but can UPDATE username
                partial_load = py_mod.load_form(conn, user_id=50, record_id=100)
                self.assertEqual(partial_load["record"], {"username": "jdoe_updated"})
                self.assertEqual(partial_load["options"], {})
                with self.assertRaises(py_mod.PermissionDeniedError):
                    py_mod.submit_form(conn, user_id=50, payload={"username": "new_insert"})
                partial_update = py_mod.submit_form(
                    conn, user_id=50, record_id=100, payload={"username": "jdoe_eve"}
                )
                self.assertEqual(partial_update["record"], {"username": "jdoe_eve"})
            finally:
                conn.close()

            # Verify CLI parity for both Python and Node scripts
            def run_node(args: list[str]) -> tuple[int, dict]:
                proc = subprocess.run(
                    ["node", str(node_action_path), "--db", str(db_path), *args],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                return proc.returncode, json.loads(proc.stdout.strip())

            def run_py(args: list[str]) -> tuple[int, dict]:
                proc = subprocess.run(
                    [sys.executable, str(py_action_path), "--db", str(db_path), *args],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                return proc.returncode, json.loads(proc.stdout.strip())

            for runner in (run_node, run_py):
                code, res = runner(["--user-id", "20", "--action", "load", "--record-id", "100"])
                self.assertEqual(code, 0)
                self.assertEqual(
                    res["options"]["department_id"],
                    [{"id": 1, "name": "Engineering"}, {"id": 2, "name": "Sales"}],
                )
                self.assertEqual(res["permissions"]["attributes"]["username"], "write")
                self.assertEqual(res["permissions"]["attributes"]["email"], "read")

                # Reject disallowed field
                code, err = runner(
                    [
                        "--user-id",
                        "20",
                        "--action",
                        "submit",
                        "--record-id",
                        "100",
                        "--payload",
                        '{"email":"blocked@example.com"}',
                    ]
                )
                self.assertEqual(code, 1)
                self.assertEqual(err["status_code"], 403)

                # Reject unauthorized user on load
                code, err = runner(["--user-id", "40", "--action", "load"])
                self.assertEqual(code, 1)
                self.assertEqual(err["status_code"], 403)

            # Node creates a record cleanly (including boolean conversion)
            code, node_created = run_node(
                [
                    "--user-id",
                    "10",
                    "--action",
                    "submit",
                    "--payload",
                    '{"username":"node_user","email":"node@example.com","department_id":1,"active":true}',
                ]
            )
            self.assertEqual(code, 0)
            self.assertEqual(node_created["action"], "created")
            self.assertEqual(node_created["record"]["username"], "node_user")

            # Both Python and Node support filtered list action + RBAC column redaction
            for runner in (run_node, run_py):
                code, listed = runner(
                    [
                        "--user-id",
                        "30",
                        "--action",
                        "list",
                        "--filters",
                        '{"department_id":2}',
                    ]
                )
                self.assertEqual(code, 0)
                self.assertEqual(len(listed["records"]), 1)
                self.assertEqual(listed["records"][0]["username"], "asmith")

                code, searched = runner(
                    [
                        "--user-id",
                        "50",
                        "--action",
                        "list",
                        "--search",
                        "node_user",
                    ]
                )
                self.assertEqual(code, 0)
                self.assertEqual(len(searched["records"]), 1)
                self.assertEqual(searched["records"][0]["username"], "node_user")
                self.assertNotIn("email", searched["records"][0])

                # User 50 cannot filter by unreadable attribute `email`
                code, err_filter = runner(
                    [
                        "--user-id",
                        "50",
                        "--action",
                        "list",
                        "--filters",
                        '{"email":"node@example.com"}',
                    ]
                )
                self.assertEqual(code, 1)
                self.assertEqual(err_filter["status_code"], 403)

                # Upsert inserts when id is null and updates when id is populated
                code, up_ins = runner(
                    [
                        "--user-id",
                        "10",
                        "--action",
                        "upsert",
                        "--payload",
                        json.dumps(
                            {
                                "id": None,
                                "username": f"up_{runner.__name__}",
                                "email": f"{runner.__name__}@example.com",
                                "department_id": 1,
                            }
                        ),
                    ]
                )
                self.assertEqual(code, 0)
                self.assertEqual(up_ins["action"], "created")
                created_id = up_ins["id"]

                # User 20 (user:write_username only) can upsert-update username by id
                code, up_upd = runner(
                    [
                        "--user-id",
                        "20",
                        "--action",
                        "upsert",
                        "--payload",
                        json.dumps({"id": created_id, "username": f"up2_{runner.__name__}"}),
                    ]
                )
                self.assertEqual(code, 0)
                self.assertEqual(up_upd["action"], "updated")
                self.assertEqual(up_upd["record"]["username"], f"up2_{runner.__name__}")

    def test_cli_generate_all_skips_system_tables(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            db_path = tmp_dir / "cli.db"
            out_dir = tmp_dir / "generated"
            _seed_full_db(db_path)

            exit_code = cli_main(
                [
                    "generate",
                    "--db",
                    f"sqlite:///{db_path}",
                    "--all",
                    "--target",
                    "both",
                    "--out-dir",
                    str(out_dir),
                ]
            )
            self.assertEqual(exit_code, 0)
            self.assertTrue((out_dir / "department.fspec").exists())
            self.assertTrue((out_dir / "user.fspec").exists())
            self.assertTrue((out_dir / "user_action.py").exists())
            self.assertTrue((out_dir / "formspec_server.mjs").exists())
            self.assertTrue((out_dir / "user_action.mjs").exists())
            self.assertIn(
                'from "./formspec_server.mjs"',
                (out_dir / "user_action.mjs").read_text(encoding="utf-8"),
            )
            self.assertFalse((out_dir / "users.fspec").exists())
            self.assertFalse((out_dir / "entity.fspec").exists())

    def test_generated_client_module_and_client_action_validate_and_invoke_server(self) -> None:
        from formspec.db import get_adapter
        from formspec.introspect import introspect_entity

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            db_path = tmp_dir / "client_test.db"
            _seed_full_db(db_path)
            with get_adapter(f"sqlite:///{db_path}") as adapter:
                schema = introspect_entity(adapter, "user")

            mod_path = tmp_dir / f"{CLIENT_MODULE_NAME}.mjs"
            mod_path.write_text(get_client_module_code(), encoding="utf-8")
            header_path = tmp_dir / f"{HEADER_MODULE_NAME}.mjs"
            header_path.write_text(get_header_client_module_code(), encoding="utf-8")

            detail_ca_path = tmp_dir / "user_form_submit.mjs"
            detail_ca_path.write_text(
                generate_detail_client_action("user_form", entity_name="user", schema=schema),
                encoding="utf-8",
            )
            list_ca_path = tmp_dir / "user_list_open.mjs"
            list_ca_path.write_text(
                generate_list_client_action("user_list", target_form_name="user_form", entity_name="user"),
                encoding="utf-8",
            )

            runner_js = tmp_dir / "run_client_test.mjs"
            runner_js.write_text(
                """
import assert from "node:assert/strict";
import { run as runDetail } from "./user_form_submit.mjs";
import { run as runList } from "./user_list_open.mjs";
import { renderAppHeader } from "./formspec_header.mjs";

const hdr = renderAppHeader({
  formName: "user_form",
  formLabel: "User Form",
  role: "admin",
  recordId: 7,
  breadcrumbs: [
    { name: "home", label: "Home", recordId: null },
    { name: "user_list", label: "Users", recordId: null },
    { name: "user_form", label: "User Form", recordId: 7 },
  ],
});
assert.match(hdr, /data-breadcrumb-index="0"/);
assert.match(hdr, />Users</);
assert.match(hdr, /User Form #7/);

let errMsg = null;
try {
  await runDetail({ payload: { username: "", email: "a@b.com", department_id: 1 } });
} catch (err) {
  errMsg = err.message;
}
assert.match(errMsg, /required/i);

let emailErr = null;
try {
  await runDetail({ payload: { username: "alice", email: "not-an-email", department_id: 1 } });
} catch (err) {
  emailErr = err.message;
}
assert.match(emailErr, /email/i);

let called = null;
const res = await runDetail({
  payload: { username: "alice", email: "alice@example.com", department_id: 1 },
  serverAction: async (name, payload) => {
    called = { name, payload };
    return { status: "ok", action: "created", id: 5 };
  },
});
assert.equal(res.id, 5);
assert.equal(called.name, "user_upsert");

let navigated = null;
await runList({
  params: { id: 7 },
  navigate: (target, params) => {
    navigated = { target, params };
    return navigated;
  },
});
assert.deepEqual(navigated, { target: "user_form", params: { id: 7 } });
console.log("OK");
""",
                encoding="utf-8",
            )
            proc = subprocess.run(
                ["node", str(runner_js)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stderr or proc.stdout)
            self.assertEqual(proc.stdout.strip(), "OK")


if __name__ == "__main__":
    unittest.main()
