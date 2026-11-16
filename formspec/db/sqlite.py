"""SQLite database adapter for schema, entity metadata, and RBAC introspection."""

from __future__ import annotations

import sqlite3
from typing import Any, Iterable

from formspec.db.base import (
    ACCESS_ORDER,
    AccessLevel,
    AttributeSpec,
    ClientActionRecord,
    ClientModuleRecord,
    ColumnSchema,
    EntityMetadata,
    FormRecord,
    RbacSnapshot,
    RbacTableConfig,
    RolePermissionMatrix,
    ServerActionRecord,
    ServerModuleRecord,
    UserRecord,
    WorkspaceAdapter,
    max_access,
    parse_permission_string,
    validate_identifier,
)

_SYSTEM_TABLES = frozenset(
    {
        "entity",
        "forms",
        "server_module",
        "client_module",
        "server_action",
        "client_action",
        "users",
        "roles",
        "permissions",
        "user_roles",
        "role_permissions",
        "user",
        "role",
        "permission",
        "user_role",
        "role_permission",
    }
)

_FSPEC_TO_SQL_TYPE: dict[str, str] = {
    "text": "TEXT",
    "textarea": "TEXT",
    "email": "TEXT",
    "url": "TEXT",
    "tel": "TEXT",
    "password": "TEXT",
    "color": "TEXT",
    "int": "INTEGER",
    "integer": "INTEGER",
    "decimal": "REAL",
    "number": "REAL",
    "float": "REAL",
    "currency": "REAL",
    "boolean": "BOOLEAN",
    "bool": "BOOLEAN",
    "date": "DATE",
    "datetime": "DATETIME",
    "time": "TIME",
    "file": "BLOB",
}


def _quote_ident(name: str) -> str:
    """Quote a SQLite identifier safely."""
    return '"' + name.replace('"', '""') + '"'


def _parse_sqlite_path(dsn: str) -> str:
    """Extract SQLite file path or `:memory:` from a DSN or path."""
    if dsn.startswith("sqlite:///"):
        return dsn[len("sqlite:///") :]
    if dsn.startswith("sqlite://"):
        return dsn[len("sqlite://") :]
    return dsn


def _map_sql_type(sql_type: str, col_name: str) -> str:
    """Map a SQLite column type and name to a FormSpec field type."""
    upper = (sql_type or "").strip().upper()
    if "BOOL" in upper:
        return "boolean"
    if "INT" in upper:
        return "int"
    if any(tok in upper for tok in ("REAL", "FLOA", "DOUB", "NUM", "DEC")):
        return "decimal"
    if "DATETIME" in upper or "TIMESTAMP" in upper:
        return "datetime"
    if "DATE" in upper:
        return "date"
    if "TIME" in upper:
        return "time"
    if "BLOB" in upper:
        return "file"
    if col_name == "email" or col_name.endswith("_email"):
        return "email"
    return "text"


def _pick_first_present(candidates: Iterable[str], available: set[str]) -> str | None:
    """Return the first candidate string present in `available`."""
    for candidate in candidates:
        if candidate in available:
            return candidate
    return None


class SQLiteAdapter(WorkspaceAdapter):
    """WorkspaceAdapter implementation backed by Python's stdlib `sqlite3`."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self.db_path = _parse_sqlite_path(dsn)
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")

    def close(self) -> None:
        self.conn.close()

    def list_tables(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        ).fetchall()
        return [str(row["name"]) for row in rows]

    def _table_columns(self, table_name: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            f"PRAGMA table_info({_quote_ident(table_name)})"
        ).fetchall()

    def introspect_table(self, table_name: str) -> list[ColumnSchema]:
        tables = set(self.list_tables())
        if table_name not in tables:
            raise ValueError(f"Table '{table_name}' does not exist in SQLite database.")

        fk_rows = self.conn.execute(
            f"PRAGMA foreign_key_list({_quote_ident(table_name)})"
        ).fetchall()
        fk_by_col: dict[str, tuple[str, str]] = {
            str(row["from"]): (str(row["table"]), str(row["to"] or "id"))
            for row in fk_rows
        }

        columns: list[ColumnSchema] = []
        for row in self._table_columns(table_name):
            col_name = str(row["name"])
            sql_type = str(row["type"] or "TEXT")
            fk_target = fk_by_col.get(col_name)
            columns.append(
                ColumnSchema(
                    name=col_name,
                    sql_type=sql_type,
                    fspec_type=_map_sql_type(sql_type, col_name),
                    nullable=not bool(row["notnull"]),
                    default=None if row["dflt_value"] is None else str(row["dflt_value"]),
                    is_pk=bool(row["pk"]),
                    fk_table=fk_target[0] if fk_target else None,
                    fk_column=fk_target[1] if fk_target else None,
                )
            )
        return columns

    def read_entity_metadata(self, entity_slug: str) -> EntityMetadata | None:
        tables = set(self.list_tables())
        if "entity" not in tables:
            return None

        col_names = {str(row["name"]) for row in self._table_columns("entity")}
        match_col = _pick_first_present(("slug", "name"), col_names)
        if match_col is None:
            return None

        row = self.conn.execute(
            f"SELECT * FROM {_quote_ident('entity')} "
            f"WHERE {_quote_ident(match_col)} = ? LIMIT 1",
            (entity_slug,),
        ).fetchone()
        if row is None:
            return None

        display_col = _pick_first_present(
            ("display_name", "label", "title", "name"),
            col_names,
        )
        attr_col = _pick_first_present(
            (
                "display_attribute",
                "display_field",
                "label_attribute",
                "name_attribute",
                "display_column",
            ),
            col_names,
        )
        display_name = (
            str(row[display_col])
            if display_col and row[display_col] is not None
            else None
        )
        display_attr = (
            str(row[attr_col])
            if attr_col and row[attr_col] is not None
            else None
        )
        return EntityMetadata(
            slug=entity_slug,
            display_name=display_name,
            display_attribute=display_attr,
        )

    def _detect_rbac_config(self) -> RbacTableConfig | None:
        tables = set(self.list_tables())
        users_t = _pick_first_present(("users", "user"), tables)
        roles_t = _pick_first_present(("roles", "role"), tables)
        perms_t = _pick_first_present(("permissions", "permission"), tables)
        ur_t = _pick_first_present(("user_roles", "user_role"), tables)
        rp_t = _pick_first_present(("role_permissions", "role_permission"), tables)
        if not (roles_t and perms_t and rp_t):
            return None

        role_cols = {str(r["name"]) for r in self._table_columns(roles_t)}
        perm_cols = {str(r["name"]) for r in self._table_columns(perms_t)}
        role_name_col = _pick_first_present(("slug", "name"), role_cols)
        perm_name_col = _pick_first_present(("slug", "name"), perm_cols)
        if not (role_name_col and perm_name_col):
            return None

        return RbacTableConfig(
            users_table=users_t or "users",
            roles_table=roles_t,
            permissions_table=perms_t,
            user_roles_table=ur_t or "user_roles",
            role_permissions_table=rp_t,
            role_name_col=role_name_col,
            perm_name_col=perm_name_col,
        )

    def read_rbac_permissions(
        self,
        entity_slug: str,
        attribute_names: list[str],
    ) -> RolePermissionMatrix:
        config = self._detect_rbac_config()
        if config is None:
            return RolePermissionMatrix()

        role_rows = self.conn.execute(
            f"SELECT {_quote_ident(config.role_name_col)} AS role_name "
            f"FROM {_quote_ident(config.roles_table)} "
            f"ORDER BY {_quote_ident(config.role_name_col)}"
        ).fetchall()
        roles = [str(r["role_name"]) for r in role_rows]

        rp_cols = {
            str(r["name"]) for r in self._table_columns(config.role_permissions_table)
        }
        access_col = _pick_first_present(("access", "level"), rp_cols)
        select_access = (
            f", rp.{_quote_ident(access_col)} AS rp_access" if access_col else ""
        )

        query = (
            f"SELECT r.{_quote_ident(config.role_name_col)} AS role_name, "
            f"p.{_quote_ident(config.perm_name_col)} AS perm_name"
            f"{select_access} "
            f"FROM {_quote_ident(config.role_permissions_table)} rp "
            f"JOIN {_quote_ident(config.roles_table)} r ON r.id = rp.role_id "
            f"JOIN {_quote_ident(config.permissions_table)} p ON p.id = rp.permission_id"
        )
        rows = self.conn.execute(query).fetchall()

        attr_set = set(attribute_names)
        entity_perms: dict[str, AccessLevel] = {}
        attribute_perms: dict[str, dict[str, AccessLevel]] = {}

        for row in rows:
            role_name = str(row["role_name"])
            perm_name = str(row["perm_name"])
            parsed = parse_permission_string(perm_name, entity_slug, attr_set)
            if parsed is None and access_col and perm_name == entity_slug:
                raw_access = str(row["rp_access"] or "").lower()
                if raw_access in ACCESS_ORDER:
                    parsed = (None, raw_access)  # type: ignore[assignment]
            if parsed is None:
                continue

            attr_name, access_level = parsed
            if attr_name is None:
                prev = entity_perms.get(role_name, "none")
                entity_perms[role_name] = max_access(prev, access_level)
            else:
                attr_map = attribute_perms.setdefault(attr_name, {})
                prev = attr_map.get(role_name, "none")
                attr_map[role_name] = max_access(prev, access_level)

        return RolePermissionMatrix(
            roles=roles,
            entity_perms=entity_perms,
            attribute_perms=attribute_perms,
            rbac_config=config,
        )

    # ------------------------------------------------------------------
    # Workspace / IDE DDL, Auth, RBAC, and Forms methods
    # ------------------------------------------------------------------

    _REQUIRED_CORE_TABLES = frozenset(
        {
            "users",
            "roles",
            "permissions",
            "user_roles",
            "role_permissions",
            "entity",
            "forms",
            "server_module",
            "client_module",
            "server_action",
            "client_action",
        }
    )

    def is_initialized(self) -> bool:
        tables = set(self.list_tables())
        if not self._REQUIRED_CORE_TABLES.issubset(tables):
            return False
        user_cols = {str(r["name"]) for r in self._table_columns("users")}
        return {"password_hash", "is_superuser"}.issubset(user_cols)

    def has_superuser(self) -> bool:
        if "users" not in set(self.list_tables()):
            return False
        cols = {str(r["name"]) for r in self._table_columns("users")}
        if "is_superuser" in cols:
            row = self.conn.execute(
                "SELECT 1 FROM users WHERE is_superuser = 1 LIMIT 1"
            ).fetchone()
            return row is not None
        row = self.conn.execute("SELECT 1 FROM users LIMIT 1").fetchone()
        return row is not None

    def initialize_schema(self) -> None:
        with self.conn:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL DEFAULT '',
                    is_superuser BOOLEAN NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS roles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE
                );
                CREATE TABLE IF NOT EXISTS permissions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE
                );
                CREATE TABLE IF NOT EXISTS user_roles (
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    role_id INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
                    PRIMARY KEY (user_id, role_id)
                );
                CREATE TABLE IF NOT EXISTS role_permissions (
                    role_id INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
                    permission_id INTEGER NOT NULL REFERENCES permissions(id) ON DELETE CASCADE,
                    PRIMARY KEY (role_id, permission_id)
                );
                CREATE TABLE IF NOT EXISTS entity (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    label TEXT NOT NULL,
                    description TEXT
                );
                CREATE TABLE IF NOT EXISTS forms (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    label TEXT NOT NULL,
                    description TEXT,
                    entity_id INTEGER REFERENCES entity(id) ON DELETE SET NULL,
                    form_type TEXT NOT NULL DEFAULT 'detail',
                    fspec_source TEXT NOT NULL,
                    formatter TEXT NOT NULL DEFAULT 'html5',
                    theme TEXT NOT NULL DEFAULT 'default',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS server_module (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    label TEXT NOT NULL,
                    description TEXT,
                    target_runtime TEXT NOT NULL DEFAULT 'node',
                    code TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS client_module (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    label TEXT NOT NULL,
                    description TEXT,
                    code TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS server_action (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    label TEXT NOT NULL,
                    description TEXT,
                    entity_id INTEGER REFERENCES entity(id) ON DELETE SET NULL,
                    action_type TEXT NOT NULL DEFAULT 'upsert',
                    target_runtime TEXT NOT NULL DEFAULT 'node',
                    code TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS client_action (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    label TEXT NOT NULL,
                    description TEXT,
                    target_form_id INTEGER REFERENCES forms(id) ON DELETE SET NULL,
                    action_type TEXT NOT NULL DEFAULT 'link',
                    params_spec TEXT,
                    code TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                INSERT OR IGNORE INTO roles (name) VALUES ('admin'), ('guest'), ('anon');
                """
            )
            user_cols = {str(r["name"]) for r in self._table_columns("users")}
            if "password_hash" not in user_cols:
                self.conn.execute(
                    "ALTER TABLE users ADD COLUMN password_hash TEXT NOT NULL DEFAULT ''"
                )
            if "is_superuser" not in user_cols:
                self.conn.execute(
                    "ALTER TABLE users ADD COLUMN is_superuser BOOLEAN NOT NULL DEFAULT 0"
                )
            form_cols = {str(r["name"]) for r in self._table_columns("forms")}
            if "form_type" not in form_cols:
                self.conn.execute(
                    "ALTER TABLE forms ADD COLUMN form_type TEXT NOT NULL DEFAULT 'detail'"
                )

    def reset_database(self) -> None:
        """Drop all user and system tables in SQLite and re-initialize core schema."""
        tables = self.list_tables()
        self.conn.execute("PRAGMA foreign_keys = OFF;")
        try:
            with self.conn:
                for tbl in tables:
                    self.conn.execute(f"DROP TABLE IF EXISTS {_quote_ident(tbl)};")
                seq_row = self.conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sqlite_sequence' LIMIT 1"
                ).fetchone()
                if seq_row is not None:
                    self.conn.execute("DELETE FROM sqlite_sequence;")
        finally:
            self.conn.execute("PRAGMA foreign_keys = ON;")
        self.initialize_schema()

    def create_superuser(self, username: str, password_hash: str) -> int:
        clean_user = validate_identifier(username, "username")
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO users (username, password_hash, is_superuser) VALUES (?, ?, 1)",
                (clean_user, password_hash),
            )
            user_id = int(cur.lastrowid)
            self.conn.execute("INSERT OR IGNORE INTO roles (name) VALUES ('admin')")
            role_row = self.conn.execute(
                "SELECT id FROM roles WHERE name = 'admin'"
            ).fetchone()
            if role_row is not None:
                self.conn.execute(
                    "INSERT OR IGNORE INTO user_roles (user_id, role_id) VALUES (?, ?)",
                    (user_id, int(role_row["id"])),
                )
        return user_id

    def _user_roles(self, user_id: int) -> list[str]:
        rows = self.conn.execute(
            "SELECT r.name FROM user_roles ur "
            "JOIN roles r ON r.id = ur.role_id "
            "WHERE ur.user_id = ? ORDER BY r.name",
            (user_id,),
        ).fetchall()
        return [str(r["name"]) for r in rows]

    def get_user_by_username(self, username: str) -> UserRecord | None:
        row = self.conn.execute(
            "SELECT id, username, password_hash, is_superuser FROM users WHERE username = ? LIMIT 1",
            (username,),
        ).fetchone()
        if row is None:
            return None
        uid = int(row["id"])
        return UserRecord(
            id=uid,
            username=str(row["username"]),
            password_hash=str(row["password_hash"]),
            is_superuser=bool(row["is_superuser"]),
            roles=self._user_roles(uid),
        )

    def get_user_by_id(self, user_id: int) -> UserRecord | None:
        row = self.conn.execute(
            "SELECT id, username, password_hash, is_superuser FROM users WHERE id = ? LIMIT 1",
            (user_id,),
        ).fetchone()
        if row is None:
            return None
        uid = int(row["id"])
        return UserRecord(
            id=uid,
            username=str(row["username"]),
            password_hash=str(row["password_hash"]),
            is_superuser=bool(row["is_superuser"]),
            roles=self._user_roles(uid),
        )

    def list_domain_entities(self) -> list[dict[str, Any]]:
        tables = [t for t in self.list_tables() if t not in _SYSTEM_TABLES]
        result: list[dict[str, Any]] = []
        for table in tables:
            meta = self.read_entity_metadata(table)
            cols = self.introspect_table(table)
            fk_by_col: dict[str, str] = {}
            for col in cols:
                if col.fk_table:
                    fk_by_col[col.name] = col.fk_table
                elif col.name.endswith("_id") and col.name[:-3] in tables:
                    fk_by_col[col.name] = col.name[:-3]
            result.append(
                {
                    "name": table,
                    "label": (
                        meta.display_name
                        if meta and meta.display_name
                        else table.replace("_", " ").title()
                    ),
                    "description": self._read_entity_description(table),
                    "columns": [
                        {
                            "name": c.name,
                            "sql_type": c.sql_type,
                            "fspec_type": c.fspec_type,
                            "nullable": c.nullable,
                            "default": c.default,
                            "is_pk": c.is_pk,
                            "fk_table": fk_by_col.get(c.name),
                        }
                        for c in cols
                    ],
                }
            )
        return result

    def _read_entity_description(self, entity_name: str) -> str | None:
        if "entity" not in set(self.list_tables()):
            return None
        cols = {str(r["name"]) for r in self._table_columns("entity")}
        if "description" not in cols:
            return None
        match_col = _pick_first_present(("slug", "name"), cols)
        if not match_col:
            return None
        row = self.conn.execute(
            f"SELECT description FROM entity WHERE {_quote_ident(match_col)} = ? LIMIT 1",
            (entity_name,),
        ).fetchone()
        return None if row is None or row["description"] is None else str(row["description"])

    @staticmethod
    def _format_default_sql(default: str, sql_type: str) -> str:
        raw = default.strip()
        upper = raw.upper()
        if upper in ("CURRENT_TIMESTAMP", "CURRENT_DATE", "CURRENT_TIME"):
            return upper
        if sql_type == "BOOLEAN":
            if upper in ("1", "TRUE", "YES"):
                return "1"
            if upper in ("0", "FALSE", "NO"):
                return "0"
        if sql_type == "INTEGER":
            return str(int(raw))
        if sql_type == "REAL":
            return str(float(raw))
        escaped = raw.replace("'", "''")
        return f"'{escaped}'"

    def _build_column_ddl(
        self,
        spec: AttributeSpec,
        existing_tables: set[str],
        *,
        for_alter: bool,
    ) -> str:
        col_name = validate_identifier(spec.name, "attribute name")
        if col_name == "id":
            raise ValueError("Column 'id' is automatically created as the primary key.")

        if spec.fk_table:
            target = validate_identifier(spec.fk_table, "foreign key target entity")
            if target not in existing_tables:
                raise ValueError(f"Foreign key target entity '{target}' does not exist.")
            sql_type = "INTEGER"
            ref_clause = f" REFERENCES {_quote_ident(target)}(id)"
        else:
            sql_type = _FSPEC_TO_SQL_TYPE.get(spec.fspec_type.lower(), "TEXT")
            ref_clause = ""

        if for_alter and not spec.nullable and spec.default is None:
            raise ValueError(
                "SQLite requires a default value when adding a NOT NULL attribute to an existing entity."
            )

        null_clause = "" if spec.nullable else " NOT NULL"
        default_clause = (
            f" DEFAULT {self._format_default_sql(spec.default, sql_type)}"
            if spec.default is not None and spec.default != ""
            else ""
        )
        return f"{_quote_ident(col_name)} {sql_type}{null_clause}{default_clause}{ref_clause}"

    def create_entity(
        self,
        name: str,
        label: str,
        description: str | None = None,
        attributes: list[AttributeSpec] | None = None,
    ) -> int:
        entity_name = validate_identifier(name, "entity name")
        if entity_name in _SYSTEM_TABLES:
            raise ValueError(f"'{entity_name}' is a reserved system table name.")

        existing = set(self.list_tables())
        if entity_name in existing:
            raise ValueError(f"Entity '{entity_name}' already exists.")

        col_ddls = ["id INTEGER PRIMARY KEY AUTOINCREMENT"]
        for spec in attributes or []:
            col_ddls.append(self._build_column_ddl(spec, existing, for_alter=False))

        create_sql = (
            f"CREATE TABLE {_quote_ident(entity_name)} ({', '.join(col_ddls)})"
        )
        with self.conn:
            self.conn.execute(create_sql)
            cur = self.conn.execute(
                "INSERT INTO entity (name, label, description) VALUES (?, ?, ?)",
                (entity_name, label.strip() or entity_name.title(), description),
            )
            entity_id = int(cur.lastrowid)

        for access in ("read", "write"):
            perm = f"{entity_name}:{access}"
            self.ensure_permission(perm)
            self.set_role_permission("admin", perm, True)

        return entity_id

    def add_attribute(self, entity_name: str, spec: AttributeSpec) -> None:
        clean_entity = validate_identifier(entity_name, "entity name")
        existing = set(self.list_tables())
        if clean_entity not in existing or clean_entity in _SYSTEM_TABLES:
            raise ValueError(f"Domain entity '{clean_entity}' does not exist.")

        existing_cols = {c.name for c in self.introspect_table(clean_entity)}
        if spec.name in existing_cols:
            raise ValueError(f"Attribute '{spec.name}' already exists on '{clean_entity}'.")

        col_ddl = self._build_column_ddl(spec, existing, for_alter=True)
        with self.conn:
            self.conn.execute(
                f"ALTER TABLE {_quote_ident(clean_entity)} ADD COLUMN {col_ddl}"
            )

    def list_rbac_snapshot(self) -> RbacSnapshot:
        role_rows = self.conn.execute("SELECT name FROM roles ORDER BY name").fetchall()
        perm_rows = self.conn.execute(
            "SELECT name FROM permissions ORDER BY name"
        ).fetchall()
        grant_rows = self.conn.execute(
            "SELECT r.name AS role_name, p.name AS perm_name "
            "FROM role_permissions rp "
            "JOIN roles r ON r.id = rp.role_id "
            "JOIN permissions p ON p.id = rp.permission_id "
            "ORDER BY r.name, p.name"
        ).fetchall()

        roles = [str(r["name"]) for r in role_rows]
        grants: dict[str, list[str]] = {role: [] for role in roles}
        for row in grant_rows:
            grants.setdefault(str(row["role_name"]), []).append(str(row["perm_name"]))

        return RbacSnapshot(
            roles=roles,
            permissions=[str(p["name"]) for p in perm_rows],
            grants=grants,
        )

    def create_role(self, role_name: str) -> None:
        clean = validate_identifier(role_name, "role name")
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO roles (name) VALUES (?)", (clean,))

    def ensure_permission(self, perm_name: str) -> None:
        clean = perm_name.strip()
        if not clean:
            raise ValueError("Permission name cannot be empty.")
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO permissions (name) VALUES (?)", (clean,)
            )

    def set_role_permission(
        self, role_name: str, perm_name: str, granted: bool
    ) -> None:
        self.ensure_permission(perm_name)
        role_row = self.conn.execute(
            "SELECT id FROM roles WHERE name = ?", (role_name,)
        ).fetchone()
        if role_row is None:
            raise ValueError(f"Role '{role_name}' does not exist.")
        perm_row = self.conn.execute(
            "SELECT id FROM permissions WHERE name = ?", (perm_name,)
        ).fetchone()
        assert perm_row is not None
        rid = int(role_row["id"])
        pid = int(perm_row["id"])
        with self.conn:
            if granted:
                self.conn.execute(
                    "INSERT OR IGNORE INTO role_permissions (role_id, permission_id) VALUES (?, ?)",
                    (rid, pid),
                )
            else:
                self.conn.execute(
                    "DELETE FROM role_permissions WHERE role_id = ? AND permission_id = ?",
                    (rid, pid),
                )

    @staticmethod
    def _row_to_form_record(row: sqlite3.Row) -> FormRecord:
        return FormRecord(
            id=int(row["id"]),
            name=str(row["name"]),
            label=str(row["label"]),
            description=None if row["description"] is None else str(row["description"]),
            entity_id=None if row["entity_id"] is None else int(row["entity_id"]),
            entity_name=None if row["entity_name"] is None else str(row["entity_name"]),
            fspec_source=str(row["fspec_source"]),
            formatter=str(row["formatter"]),
            theme=str(row["theme"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            form_type=str(row["form_type"] or "detail"),
        )

    def list_forms(self) -> list[FormRecord]:
        rows = self.conn.execute(
            "SELECT f.*, e.name AS entity_name "
            "FROM forms f LEFT JOIN entity e ON e.id = f.entity_id "
            "ORDER BY f.name ASC"
        ).fetchall()
        return [self._row_to_form_record(r) for r in rows]

    def get_form(self, form_id: int) -> FormRecord | None:
        row = self.conn.execute(
            "SELECT f.*, e.name AS entity_name "
            "FROM forms f LEFT JOIN entity e ON e.id = f.entity_id "
            "WHERE f.id = ? LIMIT 1",
            (form_id,),
        ).fetchone()
        return None if row is None else self._row_to_form_record(row)

    def get_form_by_name(self, name: str) -> FormRecord | None:
        row = self.conn.execute(
            "SELECT f.*, e.name AS entity_name "
            "FROM forms f LEFT JOIN entity e ON e.id = f.entity_id "
            "WHERE f.name = ? LIMIT 1",
            (name,),
        ).fetchone()
        return None if row is None else self._row_to_form_record(row)

    def save_form(
        self,
        name: str,
        label: str,
        fspec_source: str,
        *,
        entity_name: str | None = None,
        description: str | None = None,
        formatter: str = "html5",
        theme: str = "default",
        form_type: str = "detail",
    ) -> FormRecord:
        clean_name = validate_identifier(name, "form name")
        existing = self.get_form_by_name(clean_name)
        if existing is None:
            return self.create_form(
                name=clean_name,
                label=label,
                fspec_source=fspec_source,
                entity_name=entity_name,
                description=description,
                formatter=formatter,
                theme=theme,
                form_type=form_type,
            )
        entity_id = existing.entity_id
        if entity_name is not None:
            ent_row = self.conn.execute(
                "SELECT id FROM entity WHERE name = ? LIMIT 1", (entity_name,)
            ).fetchone()
            entity_id = int(ent_row["id"]) if ent_row is not None else None
        clean_type = form_type if form_type in ("detail", "list") else existing.form_type
        with self.conn:
            self.conn.execute(
                "UPDATE forms SET label = ?, description = ?, entity_id = ?, form_type = ?, "
                "fspec_source = ?, formatter = ?, theme = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (
                    label.strip() or existing.label,
                    description if description is not None else existing.description,
                    entity_id,
                    clean_type,
                    fspec_source,
                    formatter,
                    theme,
                    existing.id,
                ),
            )
        rec = self.get_form(existing.id)
        assert rec is not None
        return rec

    def create_form(
        self,
        name: str,
        label: str,
        fspec_source: str,
        entity_name: str | None = None,
        description: str | None = None,
        formatter: str = "html5",
        theme: str = "default",
        form_type: str = "detail",
    ) -> FormRecord:
        clean_name = validate_identifier(name, "form name")
        clean_type = form_type if form_type in ("detail", "list") else "detail"
        entity_id: int | None = None
        if entity_name:
            ent_row = self.conn.execute(
                "SELECT id FROM entity WHERE name = ? LIMIT 1", (entity_name,)
            ).fetchone()
            if ent_row is not None:
                entity_id = int(ent_row["id"])

        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO forms (name, label, description, entity_id, form_type, fspec_source, formatter, theme) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    clean_name,
                    label.strip() or clean_name.title(),
                    description,
                    entity_id,
                    clean_type,
                    fspec_source,
                    formatter,
                    theme,
                ),
            )
            form_id = int(cur.lastrowid)
        record = self.get_form(form_id)
        assert record is not None
        return record

    def update_form(
        self,
        form_id: int,
        *,
        label: str | None = None,
        description: str | None = None,
        fspec_source: str | None = None,
        formatter: str | None = None,
        theme: str | None = None,
        form_type: str | None = None,
    ) -> FormRecord:
        existing = self.get_form(form_id)
        if existing is None:
            raise ValueError(f"Form '{form_id}' does not exist.")

        updates: list[str] = ["updated_at = CURRENT_TIMESTAMP"]
        params: list[Any] = []
        for col, val in (
            ("label", label),
            ("description", description),
            ("fspec_source", fspec_source),
            ("formatter", formatter),
            ("theme", theme),
            ("form_type", form_type),
        ):
            if val is not None:
                updates.append(f"{col} = ?")
                params.append(val)
        params.append(form_id)

        with self.conn:
            self.conn.execute(
                f"UPDATE forms SET {', '.join(updates)} WHERE id = ?",
                params,
            )
        updated = self.get_form(form_id)
        assert updated is not None
        return updated

    def delete_form(self, form_id: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM forms WHERE id = ?", (form_id,))

    def query_entity_options(
        self,
        table_name: str,
        key_col: str = "id",
        display_col: str = "name",
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        clean_table = validate_identifier(table_name, "table name")
        tables = set(self.list_tables())
        if clean_table not in tables:
            return []
        cols = {c.name for c in self.introspect_table(clean_table)}
        k_col = key_col if key_col in cols else ("id" if "id" in cols else next(iter(cols), None))
        d_col = display_col if display_col in cols else ("name" if "name" in cols else k_col)
        if not k_col or not d_col:
            return []
        rows = self.conn.execute(
            f"SELECT {_quote_ident(k_col)} AS id, {_quote_ident(d_col)} AS name "
            f"FROM {_quote_ident(clean_table)} "
            f"ORDER BY {_quote_ident(d_col)} ASC LIMIT ?",
            (limit,),
        ).fetchall()
        return [{"id": r["id"], "name": r["name"]} for r in rows]

    def query_entity_records(
        self,
        table_name: str,
        filters: dict[str, Any] | None = None,
        search: str | None = None,
        order_col: str = "id",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clean_table = validate_identifier(table_name, "table name")
        if clean_table not in set(self.list_tables()):
            return []
        cols_meta = self.introspect_table(clean_table)
        col_names = {c.name for c in cols_meta}
        where_clauses: list[str] = []
        params: list[Any] = []

        for key, val in (filters or {}).items():
            if key not in col_names or val in (None, ""):
                continue
            where_clauses.append(f"{_quote_ident(key)} = ?")
            params.append(val)

        if search and search.strip():
            term = f"%{search.strip()}%"
            text_cols = [
                c.name
                for c in cols_meta
                if c.fspec_type in ("text", "textarea", "email", "url", "tel")
            ]
            if text_cols:
                like_parts = [f"CAST({_quote_ident(c)} AS TEXT) LIKE ?" for c in text_cols]
                where_clauses.append("(" + " OR ".join(like_parts) + ")")
                params.extend([term] * len(text_cols))

        where_sql = f" WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        safe_order = order_col if order_col in col_names else (
            "id" if "id" in col_names else cols_meta[0].name
        )
        rows = self.conn.execute(
            f"SELECT * FROM {_quote_ident(clean_table)}{where_sql} "
            f"ORDER BY {_quote_ident(safe_order)} ASC LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_entity_record(
        self,
        table_name: str,
        record_id: Any,
        pk_col: str = "id",
    ) -> dict[str, Any] | None:
        clean_table = validate_identifier(table_name, "table name")
        if clean_table not in set(self.list_tables()):
            return None
        col_names = {c.name for c in self.introspect_table(clean_table)}
        safe_pk = pk_col if pk_col in col_names else ("id" if "id" in col_names else None)
        if not safe_pk:
            return None
        row = self.conn.execute(
            f"SELECT * FROM {_quote_ident(clean_table)} WHERE {_quote_ident(safe_pk)} = ? LIMIT 1",
            (record_id,),
        ).fetchone()
        return None if row is None else dict(row)

    def list_all_tables_schema(self) -> list[dict[str, Any]]:
        tables = self.list_tables()
        result: list[dict[str, Any]] = []
        for table in tables:
            cols = self.introspect_table(table)
            count_row = self.conn.execute(
                f"SELECT COUNT(*) AS cnt FROM {_quote_ident(table)}"
            ).fetchone()
            row_count = int(count_row["cnt"]) if count_row is not None else 0
            result.append(
                {
                    "name": table,
                    "is_system": table in _SYSTEM_TABLES,
                    "row_count": row_count,
                    "columns": [
                        {
                            "name": c.name,
                            "sql_type": c.sql_type,
                            "nullable": c.nullable,
                            "is_pk": c.is_pk,
                            "default": c.default,
                            "fk_table": c.fk_table,
                            "fk_column": c.fk_column,
                        }
                        for c in cols
                    ],
                }
            )
        return result

    def execute_sql(self, sql: str, limit: int = 500) -> dict[str, Any]:
        query = sql.strip()
        if not query:
            raise ValueError("SQL query cannot be empty.")
        try:
            with self.conn:
                cur = self.conn.execute(query)
                if cur.description is not None:
                    columns = [str(desc[0]) for desc in cur.description]
                    fetched = cur.fetchmany(limit + 1)
                    truncated = len(fetched) > limit
                    rows = [list(r) for r in fetched[:limit]]
                    return {
                        "columns": columns,
                        "rows": rows,
                        "row_count": len(rows),
                        "truncated": truncated,
                        "message": None,
                    }
                return {
                    "columns": [],
                    "rows": [],
                    "row_count": int(cur.rowcount if cur.rowcount is not None else 0),
                    "truncated": False,
                    "message": f"Executed successfully ({cur.rowcount} row(s) affected).",
                }
        except sqlite3.Error as exc:
            raise ValueError(str(exc)) from exc

    # ------------------------------------------------------------------
    # Server Actions & Client Actions CRUD
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_server_action(row: sqlite3.Row) -> ServerActionRecord:
        return ServerActionRecord(
            id=int(row["id"]),
            name=str(row["name"]),
            label=str(row["label"]),
            description=None if row["description"] is None else str(row["description"]),
            entity_id=None if row["entity_id"] is None else int(row["entity_id"]),
            entity_name=None if row["entity_name"] is None else str(row["entity_name"]),
            action_type=str(row["action_type"]),
            target_runtime=str(row["target_runtime"]),
            code=str(row["code"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def list_server_actions(self) -> list[ServerActionRecord]:
        rows = self.conn.execute(
            "SELECT sa.*, e.name AS entity_name "
            "FROM server_action sa LEFT JOIN entity e ON e.id = sa.entity_id "
            "ORDER BY sa.name ASC"
        ).fetchall()
        return [self._row_to_server_action(r) for r in rows]

    def get_server_action(self, action_id: int) -> ServerActionRecord | None:
        row = self.conn.execute(
            "SELECT sa.*, e.name AS entity_name "
            "FROM server_action sa LEFT JOIN entity e ON e.id = sa.entity_id "
            "WHERE sa.id = ? LIMIT 1",
            (action_id,),
        ).fetchone()
        return None if row is None else self._row_to_server_action(row)

    def get_server_action_by_name(self, name: str) -> ServerActionRecord | None:
        row = self.conn.execute(
            "SELECT sa.*, e.name AS entity_name "
            "FROM server_action sa LEFT JOIN entity e ON e.id = sa.entity_id "
            "WHERE sa.name = ? LIMIT 1",
            (name,),
        ).fetchone()
        return None if row is None else self._row_to_server_action(row)

    def save_server_action(
        self,
        name: str,
        label: str,
        code: str,
        *,
        entity_name: str | None = None,
        description: str | None = None,
        action_type: str = "upsert",
        target_runtime: str = "node",
    ) -> ServerActionRecord:
        clean_name = validate_identifier(name, "server action name")
        entity_id: int | None = None
        if entity_name:
            ent_row = self.conn.execute(
                "SELECT id FROM entity WHERE name = ? LIMIT 1", (entity_name,)
            ).fetchone()
            if ent_row is not None:
                entity_id = int(ent_row["id"])

        existing = self.get_server_action_by_name(clean_name)
        with self.conn:
            if existing is None:
                cur = self.conn.execute(
                    "INSERT INTO server_action "
                    "(name, label, description, entity_id, action_type, target_runtime, code) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        clean_name,
                        label.strip() or clean_name.replace("_", " ").title(),
                        description,
                        entity_id,
                        action_type,
                        target_runtime,
                        code,
                    ),
                )
                action_id = int(cur.lastrowid)
            else:
                self.conn.execute(
                    "UPDATE server_action SET label = ?, description = ?, entity_id = ?, "
                    "action_type = ?, target_runtime = ?, code = ?, updated_at = CURRENT_TIMESTAMP "
                    "WHERE id = ?",
                    (
                        label.strip() or existing.label,
                        description if description is not None else existing.description,
                        entity_id if entity_id is not None else existing.entity_id,
                        action_type,
                        target_runtime,
                        code,
                        existing.id,
                    ),
                )
                action_id = existing.id
        rec = self.get_server_action(action_id)
        assert rec is not None
        return rec

    def update_server_action(
        self,
        action_id: int,
        *,
        label: str | None = None,
        description: str | None = None,
        code: str | None = None,
        target_runtime: str | None = None,
    ) -> ServerActionRecord:
        existing = self.get_server_action(action_id)
        if existing is None:
            raise ValueError(f"Server action '{action_id}' does not exist.")
        updates: list[str] = ["updated_at = CURRENT_TIMESTAMP"]
        params: list[Any] = []
        for col, val in (
            ("label", label),
            ("description", description),
            ("code", code),
            ("target_runtime", target_runtime),
        ):
            if val is not None:
                updates.append(f"{col} = ?")
                params.append(val)
        params.append(action_id)
        with self.conn:
            self.conn.execute(
                f"UPDATE server_action SET {', '.join(updates)} WHERE id = ?",
                params,
            )
        rec = self.get_server_action(action_id)
        assert rec is not None
        return rec

    def delete_server_action(self, action_id: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM server_action WHERE id = ?", (action_id,))

    @staticmethod
    def _row_to_client_action(row: sqlite3.Row) -> ClientActionRecord:
        return ClientActionRecord(
            id=int(row["id"]),
            name=str(row["name"]),
            label=str(row["label"]),
            description=None if row["description"] is None else str(row["description"]),
            target_form_id=None if row["target_form_id"] is None else int(row["target_form_id"]),
            target_form_name=None if row["target_form_name"] is None else str(row["target_form_name"]),
            action_type=str(row["action_type"]),
            params_spec=None if row["params_spec"] is None else str(row["params_spec"]),
            code=str(row["code"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def list_client_actions(self) -> list[ClientActionRecord]:
        rows = self.conn.execute(
            "SELECT ca.*, f.name AS target_form_name "
            "FROM client_action ca LEFT JOIN forms f ON f.id = ca.target_form_id "
            "ORDER BY ca.name ASC"
        ).fetchall()
        return [self._row_to_client_action(r) for r in rows]

    def get_client_action(self, action_id: int) -> ClientActionRecord | None:
        row = self.conn.execute(
            "SELECT ca.*, f.name AS target_form_name "
            "FROM client_action ca LEFT JOIN forms f ON f.id = ca.target_form_id "
            "WHERE ca.id = ? LIMIT 1",
            (action_id,),
        ).fetchone()
        return None if row is None else self._row_to_client_action(row)

    def create_client_action(
        self,
        name: str,
        label: str,
        *,
        target_form_name: str | None = None,
        description: str | None = None,
        action_type: str = "link",
        params_spec: str | None = None,
        code: str = "",
    ) -> ClientActionRecord:
        clean_name = validate_identifier(name, "client action name")
        target_form_id: int | None = None
        if target_form_name:
            frow = self.conn.execute(
                "SELECT id FROM forms WHERE name = ? LIMIT 1", (target_form_name,)
            ).fetchone()
            if frow is not None:
                target_form_id = int(frow["id"])
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO client_action "
                "(name, label, description, target_form_id, action_type, params_spec, code) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    clean_name,
                    label.strip() or clean_name.replace("_", " ").title(),
                    description,
                    target_form_id,
                    action_type,
                    params_spec,
                    code,
                ),
            )
            action_id = int(cur.lastrowid)
        rec = self.get_client_action(action_id)
        assert rec is not None
        return rec

    def update_client_action(
        self,
        action_id: int,
        *,
        label: str | None = None,
        description: str | None = None,
        target_form_name: str | None = None,
        params_spec: str | None = None,
        code: str | None = None,
    ) -> ClientActionRecord:
        existing = self.get_client_action(action_id)
        if existing is None:
            raise ValueError(f"Client action '{action_id}' does not exist.")
        updates: list[str] = ["updated_at = CURRENT_TIMESTAMP"]
        params: list[Any] = []
        if target_form_name is not None:
            frow = self.conn.execute(
                "SELECT id FROM forms WHERE name = ? LIMIT 1", (target_form_name,)
            ).fetchone()
            updates.append("target_form_id = ?")
            params.append(int(frow["id"]) if frow is not None else None)
        for col, val in (
            ("label", label),
            ("description", description),
            ("params_spec", params_spec),
            ("code", code),
        ):
            if val is not None:
                updates.append(f"{col} = ?")
                params.append(val)
        params.append(action_id)
        with self.conn:
            self.conn.execute(
                f"UPDATE client_action SET {', '.join(updates)} WHERE id = ?",
                params,
            )
        rec = self.get_client_action(action_id)
        assert rec is not None
        return rec

    def delete_client_action(self, action_id: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM client_action WHERE id = ?", (action_id,))

    def get_client_action_by_name(self, name: str) -> ClientActionRecord | None:
        row = self.conn.execute(
            "SELECT ca.*, f.name AS target_form_name "
            "FROM client_action ca LEFT JOIN forms f ON f.id = ca.target_form_id "
            "WHERE ca.name = ? LIMIT 1",
            (name,),
        ).fetchone()
        return None if row is None else self._row_to_client_action(row)

    def save_client_action(
        self,
        name: str,
        label: str,
        *,
        target_form_name: str | None = None,
        description: str | None = None,
        action_type: str = "link",
        params_spec: str | None = None,
        code: str = "",
    ) -> ClientActionRecord:
        clean_name = validate_identifier(name, "client action name")
        existing = self.get_client_action_by_name(clean_name)
        if existing is None:
            return self.create_client_action(
                name=clean_name,
                label=label,
                target_form_name=target_form_name,
                description=description,
                action_type=action_type,
                params_spec=params_spec,
                code=code,
            )
        target_form_id = existing.target_form_id
        if target_form_name is not None:
            frow = self.conn.execute(
                "SELECT id FROM forms WHERE name = ? LIMIT 1", (target_form_name,)
            ).fetchone()
            target_form_id = int(frow["id"]) if frow is not None else None
        with self.conn:
            self.conn.execute(
                "UPDATE client_action SET label = ?, description = ?, target_form_id = ?, "
                "action_type = ?, params_spec = ?, code = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (
                    label.strip() or existing.label,
                    description if description is not None else existing.description,
                    target_form_id,
                    action_type,
                    params_spec if params_spec is not None else existing.params_spec,
                    code,
                    existing.id,
                ),
            )
        rec = self.get_client_action(existing.id)
        assert rec is not None
        return rec

    # ------------------------------------------------------------------
    # Server Modules & Client Modules CRUD
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_server_module(row: sqlite3.Row) -> ServerModuleRecord:
        return ServerModuleRecord(
            id=int(row["id"]),
            name=str(row["name"]),
            label=str(row["label"]),
            description=None if row["description"] is None else str(row["description"]),
            target_runtime=str(row["target_runtime"]),
            code=str(row["code"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def list_server_modules(self) -> list[ServerModuleRecord]:
        rows = self.conn.execute(
            "SELECT * FROM server_module ORDER BY name ASC"
        ).fetchall()
        return [self._row_to_server_module(r) for r in rows]

    def get_server_module(self, module_id: int) -> ServerModuleRecord | None:
        row = self.conn.execute(
            "SELECT * FROM server_module WHERE id = ? LIMIT 1", (module_id,)
        ).fetchone()
        return None if row is None else self._row_to_server_module(row)

    def get_server_module_by_name(self, name: str) -> ServerModuleRecord | None:
        row = self.conn.execute(
            "SELECT * FROM server_module WHERE name = ? LIMIT 1", (name,)
        ).fetchone()
        return None if row is None else self._row_to_server_module(row)

    def save_server_module(
        self,
        name: str,
        label: str,
        code: str,
        *,
        description: str | None = None,
        target_runtime: str = "node",
    ) -> ServerModuleRecord:
        clean_name = validate_identifier(name, "server module name")
        existing = self.get_server_module_by_name(clean_name)
        with self.conn:
            if existing is None:
                cur = self.conn.execute(
                    "INSERT INTO server_module (name, label, description, target_runtime, code) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        clean_name,
                        label.strip() or clean_name.replace("_", " ").title(),
                        description,
                        target_runtime,
                        code,
                    ),
                )
                mod_id = int(cur.lastrowid)
            else:
                self.conn.execute(
                    "UPDATE server_module SET label = ?, description = ?, target_runtime = ?, "
                    "code = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (
                        label.strip() or existing.label,
                        description if description is not None else existing.description,
                        target_runtime,
                        code,
                        existing.id,
                    ),
                )
                mod_id = existing.id
        rec = self.get_server_module(mod_id)
        assert rec is not None
        return rec

    def update_server_module(
        self,
        module_id: int,
        *,
        label: str | None = None,
        description: str | None = None,
        code: str | None = None,
        target_runtime: str | None = None,
    ) -> ServerModuleRecord:
        existing = self.get_server_module(module_id)
        if existing is None:
            raise ValueError(f"Server module '{module_id}' does not exist.")
        updates: list[str] = ["updated_at = CURRENT_TIMESTAMP"]
        params: list[Any] = []
        for col, val in (
            ("label", label),
            ("description", description),
            ("code", code),
            ("target_runtime", target_runtime),
        ):
            if val is not None:
                updates.append(f"{col} = ?")
                params.append(val)
        params.append(module_id)
        with self.conn:
            self.conn.execute(
                f"UPDATE server_module SET {', '.join(updates)} WHERE id = ?",
                params,
            )
        rec = self.get_server_module(module_id)
        assert rec is not None
        return rec

    def delete_server_module(self, module_id: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM server_module WHERE id = ?", (module_id,))

    @staticmethod
    def _row_to_client_module(row: sqlite3.Row) -> ClientModuleRecord:
        return ClientModuleRecord(
            id=int(row["id"]),
            name=str(row["name"]),
            label=str(row["label"]),
            description=None if row["description"] is None else str(row["description"]),
            code=str(row["code"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def list_client_modules(self) -> list[ClientModuleRecord]:
        rows = self.conn.execute(
            "SELECT * FROM client_module ORDER BY name ASC"
        ).fetchall()
        return [self._row_to_client_module(r) for r in rows]

    def get_client_module(self, module_id: int) -> ClientModuleRecord | None:
        row = self.conn.execute(
            "SELECT * FROM client_module WHERE id = ? LIMIT 1", (module_id,)
        ).fetchone()
        return None if row is None else self._row_to_client_module(row)

    def get_client_module_by_name(self, name: str) -> ClientModuleRecord | None:
        row = self.conn.execute(
            "SELECT * FROM client_module WHERE name = ? LIMIT 1", (name,)
        ).fetchone()
        return None if row is None else self._row_to_client_module(row)

    def save_client_module(
        self,
        name: str,
        label: str,
        code: str,
        *,
        description: str | None = None,
    ) -> ClientModuleRecord:
        clean_name = validate_identifier(name, "client module name")
        existing = self.get_client_module_by_name(clean_name)
        with self.conn:
            if existing is None:
                cur = self.conn.execute(
                    "INSERT INTO client_module (name, label, description, code) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        clean_name,
                        label.strip() or clean_name.replace("_", " ").title(),
                        description,
                        code,
                    ),
                )
                mod_id = int(cur.lastrowid)
            else:
                self.conn.execute(
                    "UPDATE client_module SET label = ?, description = ?, code = ?, "
                    "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (
                        label.strip() or existing.label,
                        description if description is not None else existing.description,
                        code,
                        existing.id,
                    ),
                )
                mod_id = existing.id
        rec = self.get_client_module(mod_id)
        assert rec is not None
        return rec

    def update_client_module(
        self,
        module_id: int,
        *,
        label: str | None = None,
        description: str | None = None,
        code: str | None = None,
    ) -> ClientModuleRecord:
        existing = self.get_client_module(module_id)
        if existing is None:
            raise ValueError(f"Client module '{module_id}' does not exist.")
        updates: list[str] = ["updated_at = CURRENT_TIMESTAMP"]
        params: list[Any] = []
        for col, val in (
            ("label", label),
            ("description", description),
            ("code", code),
        ):
            if val is not None:
                updates.append(f"{col} = ?")
                params.append(val)
        params.append(module_id)
        with self.conn:
            self.conn.execute(
                f"UPDATE client_module SET {', '.join(updates)} WHERE id = ?",
                params,
            )
        rec = self.get_client_module(module_id)
        assert rec is not None
        return rec

    def delete_client_module(self, module_id: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM client_module WHERE id = ?", (module_id,))
