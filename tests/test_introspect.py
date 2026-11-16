"""Tests for database adapters, entity/RBAC introspection, and `_id` resolution."""

from __future__ import annotations

import sqlite3
from pathlib import Path
import tempfile
import unittest

from formspec.db import get_adapter
from formspec.db.base import parse_permission_string
from formspec.introspect import introspect_entity


def _seed_db(db_path: Path) -> None:
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
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE
        );
        CREATE TABLE roles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE
        );
        CREATE TABLE permissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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
        CREATE TABLE team (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL
        );
        CREATE TABLE user (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            email TEXT NOT NULL,
            department_id INTEGER NOT NULL,
            team_id INTEGER,
            active BOOLEAN NOT NULL DEFAULT 1
        );

        INSERT INTO entity (slug, display_name, display_attribute) VALUES
            ('user', 'User Account', NULL),
            ('department', 'Department', 'name'),
            ('team', 'Team', 'title');

        INSERT INTO roles (id, name) VALUES
            (1, 'admin'),
            (2, 'self_service'),
            (3, 'viewer');

        INSERT INTO permissions (id, name) VALUES
            (1, 'user:write'),
            (2, 'user:read'),
            (3, 'user:write_username'),
            (4, 'user:read_email');

        INSERT INTO role_permissions (role_id, permission_id) VALUES
            (1, 1), -- admin -> user:write
            (2, 2), -- self_service -> user:read
            (2, 3), -- self_service -> user:write_username
            (3, 2); -- viewer -> user:read
        """
    )
    conn.commit()
    conn.close()


class IntrospectTests(unittest.TestCase):
    def test_parse_permission_string(self) -> None:
        attrs = {"username", "first_name", "department_id"}
        self.assertEqual(parse_permission_string("user:read", "user", attrs), (None, "read"))
        self.assertEqual(parse_permission_string("user:write", "user", attrs), (None, "write"))
        self.assertEqual(
            parse_permission_string("user:write_username", "user", attrs),
            ("username", "write"),
        )
        self.assertEqual(
            parse_permission_string("user:write_first_name", "user", attrs),
            ("first_name", "write"),
        )
        self.assertIsNone(parse_permission_string("user:write_unknown", "user", attrs))
        self.assertIsNone(parse_permission_string("order:write", "user", attrs))

    def test_unknown_adapter_scheme_raises(self) -> None:
        with self.assertRaises(ValueError):
            get_adapter("oracle://localhost/db")

    def test_introspect_singular_entity_and_id_links(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            _seed_db(db_path)

            with get_adapter(f"sqlite:///{db_path}") as adapter:
                schema = introspect_entity(adapter, "user")

            self.assertEqual(schema.entity_slug, "user")
            self.assertEqual(schema.display_name, "User Account")
            self.assertEqual(schema.pk_column, "id")

            cols = {c.name: c for c in schema.columns}
            self.assertEqual(cols["email"].fspec_type, "email")
            self.assertEqual(cols["active"].fspec_type, "boolean")

            self.assertIsNotNone(cols["department_id"].entity_link)
            self.assertEqual(cols["department_id"].entity_link.target_table, "department")
            self.assertEqual(cols["department_id"].entity_link.display_col, "name")

            self.assertIsNotNone(cols["team_id"].entity_link)
            self.assertEqual(cols["team_id"].entity_link.target_table, "team")
            self.assertEqual(cols["team_id"].entity_link.display_col, "title")

            self.assertEqual(schema.rbac.effective_role_perm("admin", "email"), "write")
            self.assertEqual(schema.rbac.effective_role_perm("self_service", "username"), "write")
            self.assertEqual(schema.rbac.effective_role_perm("self_service", "email"), "read")
            self.assertEqual(schema.rbac.effective_role_perm("viewer", "username"), "read")
            self.assertEqual(schema.rbac.write_capable_roles(), ["admin", "self_service"])

    def test_entity_table_with_id_name_label_description(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "canonical.db"
            conn = sqlite3.connect(db_path)
            conn.executescript(
                """
                CREATE TABLE entity (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    label TEXT NOT NULL,
                    description TEXT
                );
                CREATE TABLE department (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL
                );
                INSERT INTO entity (name, label, description) VALUES
                    ('department', 'Org Department', 'Organizational departments');
                """
            )
            conn.commit()
            conn.close()

            with get_adapter(f"sqlite:///{db_path}") as adapter:
                schema = introspect_entity(adapter, "department")

            self.assertEqual(schema.entity_slug, "department")
            self.assertEqual(schema.display_name, "Org Department")
            self.assertEqual(schema.pk_column, "id")


if __name__ == "__main__":
    unittest.main()
