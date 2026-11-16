"""Integration tests for the FormSpec Prototype IDE backend, auth, DDL, and HTTP server."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
import urllib.request

from formspec.ide.server import HttpError, IDEApp, create_server


def _extract_token(set_cookie_header: str) -> str:
    first = set_cookie_header.split(";", 1)[0]
    return first.split("=", 1)[1]


class IdeApiTests(unittest.TestCase):
    def test_bootstrap_superuser_entities_fk_rbac_forms_and_render(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "studio.db"
            app = IDEApp(dsn=f"sqlite:///{db_path}", secret="test-secret")

            # 1. Database initialized with RBAC, entity, forms, and default roles
            status_code, status_body, _ = app.handle_api("GET", "/api/auth/status")
            self.assertEqual(status_code, 200)
            self.assertTrue(status_body["initialized"])
            self.assertTrue(status_body["is_sqlite"])
            self.assertFalse(status_body["has_superuser"])
            self.assertFalse(status_body["authenticated"])

            # Unauthenticated workspace endpoints are blocked
            with self.assertRaises(HttpError) as ctx_unauth:
                app.handle_api("GET", "/api/entities")
            self.assertEqual(ctx_unauth.exception.status, 401)

            # 2. First-time superuser creation
            code, setup_body, cookie = app.handle_api(
                "POST",
                "/api/auth/setup",
                {"username": "superuser", "password": "strong-password-123"},
            )
            self.assertEqual(code, 201)
            self.assertTrue(setup_body["authenticated"])
            self.assertIsNotNone(cookie)
            assert cookie is not None
            token = _extract_token(cookie)

            # Cannot re-run setup once superuser exists
            with self.assertRaises(HttpError) as ctx_dup:
                app.handle_api("POST", "/api/auth/setup", {"password": "other"})
            self.assertEqual(ctx_dup.exception.status, 409)

            # Verify default roles seeded (admin, guest, anon)
            _, rbac_body, _ = app.handle_api("GET", "/api/rbac", session_token=token)
            self.assertEqual(rbac_body["roles"], ["admin", "anon", "guest"])

            # Verify logout + login flow
            _, _, _ = app.handle_api("POST", "/api/auth/logout", session_token=token)
            with self.assertRaises(HttpError):
                app.handle_api(
                    "POST",
                    "/api/auth/login",
                    {"username": "superuser", "password": "wrong"},
                )
            code, login_body, cookie2 = app.handle_api(
                "POST",
                "/api/auth/login",
                {"username": "superuser", "password": "strong-password-123"},
            )
            self.assertEqual(code, 200)
            self.assertTrue(login_body["authenticated"])
            assert cookie2 is not None
            token = _extract_token(cookie2)

            # Verify seeded shared server_module, client_module, and `home` entry form
            _, sm_init, _ = app.handle_api("GET", "/api/server-modules", session_token=token)
            self.assertEqual([m["name"] for m in sm_init["server_modules"]], ["formspec_server"])
            _, cm_init, _ = app.handle_api("GET", "/api/client-modules", session_token=token)
            self.assertEqual(
                [m["name"] for m in cm_init["client_modules"]],
                ["formspec_client", "formspec_header"],
            )
            _, forms_init, _ = app.handle_api("GET", "/api/forms", session_token=token)
            self.assertEqual([f["name"] for f in forms_init["forms"]], ["home"])

            # 3. Create domain entities (`department`, `employee`) — server_action created immediately!
            code, ent_res, _ = app.handle_api(
                "POST",
                "/api/entities",
                {
                    "name": "department",
                    "label": "Department",
                    "description": "Company departments",
                    "attributes": [{"name": "name", "fspec_type": "text", "nullable": False}],
                },
                session_token=token,
            )
            self.assertEqual(code, 201)
            self.assertEqual(ent_res["server_action"]["name"], "department_upsert")
            self.assertEqual(ent_res["server_action"]["target_runtime"], "node")
            self.assertIn('from "./formspec_server.mjs"', ent_res["server_action"]["code"])

            # Auto-created department:read & department:write permissions granted to admin
            _, rbac_after, _ = app.handle_api("GET", "/api/rbac", session_token=token)
            self.assertIn("department:read", rbac_after["permissions"])
            self.assertIn("department:write", rbac_after["permissions"])
            self.assertIn("department:read", rbac_after["grants"]["admin"])

            # Seed sample department rows for live preview options
            conn = sqlite3.connect(db_path)
            conn.execute("INSERT INTO department (name) VALUES ('Engineering'), ('Product')")
            conn.commit()
            conn.close()

            _, emp_ent_res, _ = app.handle_api(
                "POST",
                "/api/entities",
                {
                    "name": "employee",
                    "label": "Employee",
                    "attributes": [{"name": "full_name", "fspec_type": "text", "nullable": False}],
                },
                session_token=token,
            )
            self.assertEqual(emp_ent_res["server_action"]["name"], "employee_upsert")

            # Before creating any form, both entity server_actions already exist in DB
            _, sa_before_form, _ = app.handle_api("GET", "/api/server-actions", session_token=token)
            self.assertEqual(
                [a["name"] for a in sa_before_form["server_actions"]],
                ["department_upsert", "employee_upsert"],
            )

            # Add scalar attribute & <target>_id foreign key — syncs employee_upsert ENTITY_CONFIG
            app.handle_api(
                "POST",
                "/api/entities/employee/attributes",
                {"name": "email", "fspec_type": "email", "nullable": True, "create_permissions": True},
                session_token=token,
            )
            code, fk_res, _ = app.handle_api(
                "POST",
                "/api/entities/employee/foreign-keys",
                {"target_entity": "department", "nullable": True},
                session_token=token,
            )
            self.assertEqual(code, 201)
            emp_ent = next(e for e in fk_res["entities"] if e["name"] == "employee")
            col_names = [c["name"] for c in emp_ent["columns"]]
            self.assertEqual(col_names, ["id", "full_name", "email", "department_id"])
            self.assertIn('"email"', fk_res["server_action"]["code"])
            self.assertIn('"department_id"', fk_res["server_action"]["code"])

            # Grant employee:read to guest via lightweight RBAC API
            app.handle_api(
                "POST",
                "/api/rbac/assign",
                {"role": "guest", "permission": "employee:read", "granted": True},
                session_token=token,
            )

            # 4. Generate detail form from entity -> links Submit to auto-created client_action initiating employee_upsert
            code, form_res, _ = app.handle_api(
                "POST",
                "/api/forms/from-entity",
                {"entity": "employee"},
                session_token=token,
            )
            self.assertEqual(code, 201)
            form_id = form_res["form"]["id"]
            fspec_src = form_res["form"]["fspec_source"]
            self.assertIn("[ID](id:hidden)", fspec_src)
            self.assertIn("submit:submit -> client_action:employee_form_submit", fspec_src)
            self.assertIn("department_id -> department.key(id).display(name)", fspec_src)
            self.assertIn("guest:read", fspec_src)
            self.assertEqual(form_res["server_action"]["name"], "employee_upsert")
            self.assertEqual(form_res["server_action"]["target_runtime"], "node")
            self.assertEqual(form_res["client_action"]["name"], "employee_form_submit")
            self.assertIn('from "./formspec_client.mjs"', form_res["client_action"]["code"])
            self.assertIn('"employee_upsert"', form_res["client_action"]["code"])
            self.assertIn('"full_name"', form_res["client_action"]["code"])

            # Update form in `forms`
            _, updated_form, _ = app.handle_api(
                "PUT",
                f"/api/forms/{form_id}",
                {"theme": "dark"},
                session_token=token,
            )
            self.assertEqual(updated_form["form"]["theme"], "dark")

            # Export .fspec + Python + Node module + Node actions
            _, export_res, _ = app.handle_api(
                "POST",
                f"/api/forms/{form_id}/export",
                {"target": "both"},
                session_token=token,
            )
            exported_names = [f["filename"] for f in export_res["files"]]
            self.assertEqual(
                exported_names,
                [
                    "employee_form.fspec",
                    "employee_action.py",
                    "formspec_server.mjs",
                    "employee_action.mjs",
                ],
            )

            # 5. Live render with DB-populated _id options and role preview
            _, render_admin, _ = app.handle_api(
                "POST",
                "/api/render",
                {"fspec_source": fspec_src, "formatter": "html5", "role": "admin"},
                session_token=token,
            )
            self.assertIn("<option value=\"1\">Engineering</option>", render_admin["content"])
            self.assertIn("<option value=\"2\">Product</option>", render_admin["content"])
            self.assertIn('type="submit"', render_admin["content"])

            _, render_guest, _ = app.handle_api(
                "POST",
                "/api/render",
                {"fspec_source": fspec_src, "formatter": "html5", "role": "guest"},
                session_token=token,
            )
            self.assertIn("disabled", render_guest["content"])
            self.assertNotIn('type="submit"', render_guest["content"])

            # 6. Changing RBAC permissions later automatically updates existing forms
            _, assign_res, _ = app.handle_api(
                "POST",
                "/api/rbac/assign",
                {"role": "anon", "permission": "employee:write_email", "granted": True},
                session_token=token,
            )
            self.assertEqual(len(assign_res["synced_forms"]), 1)
            synced_src = assign_res["synced_forms"][0]["fspec_source"]
            self.assertIn("[Email](email:email) @(anon:write)", synced_src)
            self.assertIn("anon:write", synced_src)

            # Verify persisted in `forms` and rendered for `anon`
            _, forms_after, _ = app.handle_api("GET", "/api/forms", session_token=token)
            persisted = next(f for f in forms_after["forms"] if f["id"] == form_id)
            self.assertEqual(persisted["fspec_source"], synced_src)

            _, render_anon, _ = app.handle_api(
                "POST",
                "/api/render",
                {"fspec_source": persisted["fspec_source"], "formatter": "html5", "role": "anon"},
                session_token=token,
            )
            self.assertIn('name="email"', render_anon["content"])
            self.assertNotIn('name="full_name"', render_anon["content"])
            self.assertIn('type="submit"', render_anon["content"])

            # 7. Generate List form linked to Detail form, filter rows, open record, and update via preview submit
            conn = sqlite3.connect(db_path)
            conn.execute(
                "INSERT INTO employee (id, full_name, email, department_id) VALUES "
                "(1, 'Alice Smith', 'alice@ex.com', 1), "
                "(2, 'Bob Jones', 'bob@ex.com', 2)"
            )
            conn.commit()
            conn.close()

            code, list_res, _ = app.handle_api(
                "POST",
                "/api/forms/from-entity",
                {"entity": "employee", "form_type": "list"},
                session_token=token,
            )
            self.assertEqual(code, 201)
            self.assertEqual(list_res["form"]["form_type"], "list")
            self.assertEqual(list_res["client_action"]["name"], "employee_list_open")
            self.assertIn('from "./formspec_client.mjs"', list_res["client_action"]["code"])
            list_src = list_res["form"]["fspec_source"]
            self.assertIn("[ID](id:hidden)", list_src)
            self.assertIn("[Department ID](department_id:hidden)", list_src)
            self.assertIn("full_name:text -> employee_form(id = $row.id)", list_src)

            # Filtered list render by department_id=2 shows only Bob Jones with name-link to employee_form
            _, render_list, _ = app.handle_api(
                "POST",
                "/api/render",
                {
                    "fspec_source": list_src,
                    "formatter": "html5",
                    "role": "guest",
                    "filters": {"department_id": "2"},
                },
                session_token=token,
            )
            self.assertIn("Bob Jones", render_list["content"])
            self.assertNotIn("Alice Smith", render_list["content"])
            self.assertIn("Product", render_list["content"])
            self.assertIn('class="fs-cell-link"', render_list["content"])
            self.assertIn('data-link-target="employee_form"', render_list["content"])
            self.assertIn('data-record-id="2"', render_list["content"])
            self.assertIn('<td class="fs-col-hidden" hidden><input type="hidden" name="department_id" value="2" /></td>', render_list["content"])

            # Opening detail form with injected params populates hidden id and full_name
            _, render_detail_rec, _ = app.handle_api(
                "POST",
                "/api/render",
                {
                    "fspec_source": persisted["fspec_source"],
                    "formatter": "html5",
                    "role": "admin",
                    "params": {"id": 2},
                    "entity_name": "employee",
                },
                session_token=token,
            )
            self.assertIn('value="Bob Jones"', render_detail_rec["content"])
            self.assertIn('<input type="hidden" id="f_id" name="id" value="2" />', render_detail_rec["content"])

            # guest (read-only) cannot update via Node.js server_action preview submit (403)
            with self.assertRaises(HttpError) as ctx_guest_sub:
                app.handle_api(
                    "POST",
                    "/api/preview/submit",
                    {
                        "entity": "employee",
                        "server_action": "employee_upsert",
                        "record_id": 2,
                        "payload": {"id": 2, "full_name": "Bob Updated"},
                        "role": "guest",
                    },
                    session_token=token,
                )
            self.assertEqual(ctx_guest_sub.exception.status, 403)

            # superuser inserts new record via Node.js server_action when id is null
            code, ins_res, _ = app.handle_api(
                "POST",
                "/api/preview/submit",
                {
                    "entity": "employee",
                    "server_action": "employee_upsert",
                    "payload": {"id": None, "full_name": "Charlie New", "email": "charlie@ex.com", "department_id": 1},
                    "role": "superuser",
                },
                session_token=token,
            )
            self.assertEqual(code, 200)
            self.assertEqual(ins_res["action"], "created")

            # admin updates record 2 via Node.js server_action when id is populated
            code, upd_res, _ = app.handle_api(
                "POST",
                "/api/preview/submit",
                {
                    "entity": "employee",
                    "server_action": "employee_upsert",
                    "record_id": 2,
                    "payload": {"id": 2, "full_name": "Bob Updated"},
                    "role": "admin",
                },
                session_token=token,
            )
            self.assertEqual(code, 200)
            self.assertEqual(upd_res["action"], "updated")

            _, render_list_after, _ = app.handle_api(
                "POST",
                "/api/render",
                {
                    "fspec_source": list_src,
                    "formatter": "html5",
                    "role": "admin",
                    "search": "Bob Updated",
                },
                session_token=token,
            )
            self.assertIn("Bob Updated", render_list_after["content"])

            # Blank form creation also auto-creates a client_action
            code, blank_res, _ = app.handle_api(
                "POST",
                "/api/forms",
                {
                    "name": "feedback_form",
                    "label": "Feedback",
                    "form_type": "detail",
                    "fspec_source": '::: form feedback_form "Feedback" @(admin:write)\n\t[Note](note:text)\n',
                },
                session_token=token,
            )
            self.assertEqual(code, 201)
            self.assertEqual(blank_res["client_action"]["name"], "feedback_form_submit")

            # Client action & module CRUD
            code, ca_res, _ = app.handle_api(
                "POST",
                "/api/client-actions",
                {
                    "name": "open_employee_detail",
                    "label": "Open Employee",
                    "target_form_name": "employee_form",
                    "params_spec": '{"id": "$selected.id"}',
                },
                session_token=token,
            )
            self.assertEqual(code, 201)
            self.assertEqual(ca_res["client_action"]["target_form_name"], "employee_form")
            _, ca_list, _ = app.handle_api("GET", "/api/client-actions", session_token=token)
            self.assertEqual(
                {c["name"] for c in ca_list["client_actions"]},
                {
                    "home_open",
                    "employee_form_submit",
                    "employee_list_open",
                    "feedback_form_submit",
                    "open_employee_detail",
                },
            )

            # Form Map (Mermaid graph) includes nodes and edges across forms
            _, map_res, _ = app.handle_api("GET", "/api/forms/map", session_token=token)
            self.assertTrue(any(n["name"] == "home" and n["is_home"] for n in map_res["nodes"]))
            self.assertTrue(
                any(
                    e["from"] == "employee_list" and e["to"] == "employee_form"
                    for e in map_res["edges"]
                )
            )
            self.assertIn("flowchart LR", map_res["mermaid"])
            self.assertIn('home(["Home (home)"])', map_res["mermaid"])
            self.assertIn("employee_list -->", map_res["mermaid"])

            code, sm_res, _ = app.handle_api(
                "POST",
                "/api/server-modules",
                {"name": "audit_helper", "label": "Audit Helper", "code": "export const V = 1;\n"},
                session_token=token,
            )
            self.assertEqual(code, 201)
            self.assertEqual(sm_res["server_module"]["name"], "audit_helper")

            code, cm_res, _ = app.handle_api(
                "POST",
                "/api/client-modules",
                {"name": "ui_helper", "label": "UI Helper", "code": "export const U = 1;\n"},
                session_token=token,
            )
            self.assertEqual(code, 201)
            self.assertEqual(cm_res["client_module"]["name"], "ui_helper")

            # 8. SQL panel lists all tables (including users, roles, permissions, modules & actions) & relations
            _, sql_tables_res, _ = app.handle_api("GET", "/api/sql/tables", session_token=token)
            tables_by_name = {t["name"]: t for t in sql_tables_res["tables"]}
            for expected_tbl in (
                "users",
                "roles",
                "permissions",
                "user_roles",
                "role_permissions",
                "entity",
                "forms",
                "server_module",
                "server_action",
                "client_module",
                "client_action",
                "department",
                "employee",
            ):
                self.assertIn(expected_tbl, tables_by_name)

            rp_cols = {c["name"]: c for c in tables_by_name["role_permissions"]["columns"]}
            self.assertEqual(rp_cols["role_id"]["fk_table"], "roles")
            self.assertEqual(rp_cols["permission_id"]["fk_table"], "permissions")

            emp_cols = {c["name"]: c for c in tables_by_name["employee"]["columns"]}
            self.assertEqual(emp_cols["department_id"]["fk_table"], "department")

            # Run SQL query across RBAC tables
            code, sql_res, _ = app.handle_api(
                "POST",
                "/api/sql/query",
                {
                    "sql": (
                        "SELECT r.name AS role, p.name AS permission "
                        "FROM role_permissions rp "
                        "JOIN roles r ON r.id = rp.role_id "
                        "JOIN permissions p ON p.id = rp.permission_id "
                        "WHERE r.name = 'guest'"
                    )
                },
                session_token=token,
            )
            self.assertEqual(code, 200)
            self.assertEqual(sql_res["columns"], ["role", "permission"])
            self.assertEqual(sql_res["rows"], [["guest", "employee:read"]])

            # Invalid SQL returns 400 error
            with self.assertRaises(HttpError) as ctx_bad_sql:
                app.handle_api(
                    "POST",
                    "/api/sql/query",
                    {"sql": "SELECT * FROM nonexistent_table"},
                    session_token=token,
                )
            self.assertEqual(ctx_bad_sql.exception.status, 400)

            # 9. Non-superuser cannot wipe DB (403); superuser can wipe & re-initialize SQLite
            from formspec.ide.auth import hash_password

            with app.open_adapter() as adp:
                with adp.conn:  # type: ignore[attr-defined]
                    cur = adp.conn.execute(  # type: ignore[attr-defined]
                        "INSERT INTO users (username, password_hash, is_superuser) VALUES (?, ?, 0)",
                        ("regular_user", hash_password("pw1234")),
                    )
                    reg_uid = int(cur.lastrowid)
            reg_token = app.sessions.create_token(reg_uid)
            with self.assertRaises(HttpError) as ctx_non_su:
                app.handle_api("POST", "/api/admin/reset-db", session_token=reg_token)
            self.assertEqual(ctx_non_su.exception.status, 403)

            code, reset_res, clear_cookie = app.handle_api(
                "POST", "/api/admin/reset-db", session_token=token
            )
            self.assertEqual(code, 200)
            self.assertTrue(reset_res["reset"])
            self.assertFalse(reset_res["has_superuser"])
            self.assertIn("Max-Age=0", clear_cookie or "")

            _, st_after, _ = app.handle_api("GET", "/api/auth/status")
            self.assertTrue(st_after["initialized"])
            self.assertFalse(st_after["has_superuser"])

            # Re-setup superuser on fresh re-initialized DB: old entities/forms are gone, default modules & roles present
            code, _, cookie3 = app.handle_api(
                "POST",
                "/api/auth/setup",
                {"username": "superuser", "password": "new-password-123"},
            )
            self.assertEqual(code, 201)
            assert cookie3 is not None
            token2 = _extract_token(cookie3)
            _, ents_after, _ = app.handle_api("GET", "/api/entities", session_token=token2)
            self.assertEqual(ents_after["entities"], [])
            _, sm_after, _ = app.handle_api("GET", "/api/server-modules", session_token=token2)
            self.assertEqual([m["name"] for m in sm_after["server_modules"]], ["formspec_server"])
            _, cm_after, _ = app.handle_api("GET", "/api/client-modules", session_token=token2)
            self.assertEqual(
                [m["name"] for m in cm_after["client_modules"]],
                ["formspec_client", "formspec_header"],
            )
            _, forms_reset, _ = app.handle_api("GET", "/api/forms", session_token=token2)
            self.assertEqual([f["name"] for f in forms_reset["forms"]], ["home"])
            _, rbac_reset, _ = app.handle_api("GET", "/api/rbac", session_token=token2)
            self.assertEqual(rbac_reset["roles"], ["admin", "anon", "guest"])

    def test_http_server_serves_spa_and_separate_css_themes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "http_test.db"
            app = IDEApp(dsn=f"sqlite:///{db_path}", secret="http-secret")
            server = create_server(app, host="127.0.0.1", port=0)
            port = server.server_address[1]
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/") as resp:
                    self.assertEqual(resp.status, 200)
                    html = resp.read().decode("utf-8")
                    self.assertIn("FormSpec Studio", html)
                    self.assertIn('id="git-branch-select"', html)
                    self.assertIn('id="btn-git-publish"', html)
                    self.assertIn('id="btn-git-push"', html)
                    self.assertIn('id="btn-open-package"', html)
                    self.assertIn('id="btn-open-map"', html)
                    self.assertLess(html.index("btn-open-sql"), html.index("btn-open-rbac"))
                    self.assertEqual(html.count('<details open class="panel-section">'), 6)
                    self.assertIn('id="server-modules-list"', html)
                    self.assertIn('id="server-actions-list"', html)
                    self.assertIn('id="client-modules-list"', html)
                    self.assertIn('id="client-actions-list"', html)
                    self.assertLess(html.index("client-actions-list"), html.index("server-modules-list"))
                    self.assertLess(html.index("server-modules-list"), html.index("client-modules-list"))
                    self.assertIn('id="sidebar-hover-card"', html)
                    self.assertIn('id="code-editor" class="code-editor"', html)
                    self.assertIn('id="admin-menu"', html)
                    self.assertIn('id="btn-wipe-db"', html)
                    self.assertNotIn("fspec-editor", html)

                with urllib.request.urlopen(f"http://127.0.0.1:{port}/ide.css") as resp:
                    self.assertEqual(resp.status, 200)
                    ide_css = resp.read().decode("utf-8")
                    self.assertIn(".code-editor", ide_css)
                    self.assertIn("border: 1px solid var(--border)", ide_css)
                    self.assertNotIn("fspec-editor", ide_css)

                with urllib.request.urlopen(f"http://127.0.0.1:{port}/themes/default.css") as resp:
                    self.assertEqual(resp.status, 200)
                    css = resp.read().decode("utf-8")
                    self.assertIn(".fs-form", css)
                    self.assertIn(".fs-app-header", css)
                    self.assertIn(".fs-breadcrumbs", css)

                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/auth/status") as resp:
                    status = json.loads(resp.read().decode("utf-8"))
                    self.assertTrue(status["initialized"])
                    self.assertFalse(status["has_superuser"])
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
