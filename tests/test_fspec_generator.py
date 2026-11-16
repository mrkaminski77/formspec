"""Tests for `.fspec` file generation."""

from __future__ import annotations

import sqlite3
from pathlib import Path
import tempfile
import unittest

from formspec.db import get_adapter
from formspec.generators.fspec import generate_fspec, sync_fspec_permissions
from formspec.introspect import introspect_entity


def _create_sample_db(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE entity (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL
        );
        CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT);
        CREATE TABLE roles (id INTEGER PRIMARY KEY, name TEXT UNIQUE);
        CREATE TABLE permissions (id INTEGER PRIMARY KEY, name TEXT UNIQUE);
        CREATE TABLE user_roles (user_id INTEGER, role_id INTEGER);
        CREATE TABLE role_permissions (role_id INTEGER, permission_id INTEGER);

        CREATE TABLE department (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL
        );
        CREATE TABLE user (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            email TEXT NOT NULL,
            department_id INTEGER NOT NULL,
            role_note TEXT DEFAULT 'standard',
            active BOOLEAN NOT NULL DEFAULT 1
        );

        INSERT INTO entity (slug, display_name) VALUES ('user', 'User');
        INSERT INTO roles (id, name) VALUES (1, 'admin'), (2, 'self_service'), (3, 'viewer');
        INSERT INTO permissions (id, name) VALUES
            (1, 'user:write'),
            (2, 'user:read'),
            (3, 'user:write_username');
        INSERT INTO role_permissions (role_id, permission_id) VALUES
            (1, 1),
            (2, 2),
            (2, 3),
            (3, 2);
        """
    )
    conn.commit()
    conn.close()


class FspecGeneratorTests(unittest.TestCase):
    def test_generates_tab_nested_fspec_with_id_links_and_granular_perms(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "fspec.db"
            _create_sample_db(db_path)

            with get_adapter(f"sqlite:///{db_path}") as adapter:
                schema = introspect_entity(adapter, "user")

            fspec_text = generate_fspec(schema)
            expected = (
                '::: form user "User" @(admin:write, self_service:read, viewer:read, *:none)\n'
                '\t::: section "Details"\n'
                "\t\t[ID](id:hidden)\n"
                "\t\t[*Username](username:text) @(self_service:write)\n"
                "\t\t[*Email](email:email)\n"
                "\t\t[*Department]{department_id -> department.key(id).display(name).order(name asc)}\n"
                '\t\t[Role Note](role_note:text = "standard")\n'
                "\t\t[x] Active (active)\n"
                "\n"
                "\t[[Submit]](submit:submit -> client_action:user_form_submit) @(admin:write, self_service:write, *:none)\n"
            )
            self.assertEqual(fspec_text, expected)

    def test_two_col_layout_uses_pipe_and_ratios(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "fspec2.db"
            _create_sample_db(db_path)

            with get_adapter(f"sqlite:///{db_path}") as adapter:
                schema = introspect_entity(adapter, "user")

            fspec_text = generate_fspec(schema, layout="two-col")
            self.assertIn(
                "\t\t[*Username](username:text) @(self_service:write) | [*Email](email:email) | [1:1]",
                fspec_text,
            )

    def test_sync_fspec_permissions_preserves_layout_and_modifiers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "sync.db"
            _create_sample_db(db_path)

            existing_fspec = (
                '::: form user "Custom User Form" @(admin:write, *:none)\n'
                '\t::: section "Profile"\n'
                "\t\t[*Handle](username:text) @(self_service:write) | [*Email](email:email) when($active == true) | [2:1]\n"
                "\n"
                "\t[[Save User]](submit) @(admin:write, *:none)\n"
            )

            # Update DB permissions: grant viewer user:write_email, revoke self_service user:write_username
            conn = sqlite3.connect(db_path)
            conn.execute("INSERT INTO permissions (id, name) VALUES (4, 'user:write_email')")
            conn.execute("DELETE FROM role_permissions WHERE role_id = 2 AND permission_id = 3")
            conn.execute("INSERT INTO role_permissions (role_id, permission_id) VALUES (3, 4)")
            conn.commit()
            conn.close()

            with get_adapter(f"sqlite:///{db_path}") as adapter:
                schema = introspect_entity(adapter, "user")

            synced = sync_fspec_permissions(existing_fspec, schema)
            expected = (
                '::: form user "Custom User Form" @(admin:write, self_service:read, viewer:read, *:none)\n'
                '\t::: section "Profile"\n'
                "\t\t[*Handle](username:text) | [*Email](email:email) when($active == true) @(viewer:write) | [2:1]\n"
                "\n"
                "\t[[Save User]](submit) @(admin:write, viewer:write, *:none)\n"
            )
            self.assertEqual(synced, expected)

    def test_generates_and_syncs_list_form(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "list_fspec.db"
            _create_sample_db(db_path)

            with get_adapter(f"sqlite:///{db_path}") as adapter:
                schema = introspect_entity(adapter, "user")

            list_fspec = generate_fspec(schema, form_type="list", detail_form_name="user_detail")
            self.assertIn('::: form user_list(type: list) "User List"', list_fspec)
            self.assertIn('[Search](q:text) placeholder("Filter...")', list_fspec)
            self.assertIn('[[+ New]](create -> user_detail(mode: "modal")) @(admin:write, self_service:write, *:none)', list_fspec)
            self.assertIn('\t::: list user(username ~= $q, department_id == $department_id).order(id asc)', list_fspec)
            self.assertIn('\t\t[ID](id:hidden)', list_fspec)
            self.assertIn('\t\t[Username](username:text -> user_detail(id = $row.id)) @(self_service:write)', list_fspec)
            self.assertIn('\t\t[Department ID](department_id:hidden)', list_fspec)
            self.assertIn('\t\t[Department]{department_id -> department.key(id).display(name).order(name asc)}', list_fspec)

            # Revoke self_service write_username -> [[+ New]] loses self_service
            conn = sqlite3.connect(db_path)
            conn.execute("DELETE FROM role_permissions WHERE role_id = 2 AND permission_id = 3")
            conn.commit()
            conn.close()

            with get_adapter(f"sqlite:///{db_path}") as adapter:
                schema2 = introspect_entity(adapter, "user")
            synced = sync_fspec_permissions(list_fspec, schema2)
            self.assertIn('[[+ New]](create -> user_detail(mode: "modal")) @(admin:write, *:none)', synced)


if __name__ == "__main__":
    unittest.main()
