"""Pluggable multi-RDBMS database initialization SQL script generator.

Supports SQLite, PostgreSQL, MySQL, and SQL Server out of the box, with a
registry (`register_dialect`) so additional RDBMS engines can be added cleanly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass
from typing import Any

from formspec.db.base import WorkspaceAdapter


@dataclass(frozen=True)
class ColumnDef:
    """Dialect-agnostic column specification used to render DDL."""

    name: str
    fspec_type: str
    is_pk: bool = False
    nullable: bool = True
    unique: bool = False
    default_expr: str | None = None
    fk_table: str | None = None
    fk_column: str = "id"
    on_delete: str | None = None


@dataclass(frozen=True)
class TableDef:
    """Dialect-agnostic table specification used to render DDL."""

    name: str
    columns: list[ColumnDef]
    primary_key_cols: tuple[str, ...] = ()


class DatabaseDialect(ABC):
    """Abstract base class for RDBMS SQL dialect renderers."""

    id: str
    label: str
    description: str = ""

    @abstractmethod
    def quote_ident(self, ident: str) -> str:
        """Quote a table or column identifier."""

    @abstractmethod
    def pk_auto_type(self) -> str:
        """Return auto-incrementing integer primary key column type definition."""

    @abstractmethod
    def sql_type(self, fspec_type: str, *, unique: bool = False, is_fk: bool = False) -> str:
        """Map a FormSpec type to the target RDBMS SQL type."""

    def bool_literal(self, val: bool) -> str:
        return "TRUE" if val else "FALSE"

    def default_sql(self, raw_default: str | None, fspec_type: str) -> str | None:
        if raw_default is None:
            return None
        upper = raw_default.strip().upper()
        if upper == "CURRENT_TIMESTAMP":
            return "CURRENT_TIMESTAMP"
        if fspec_type == "boolean":
            if upper in ("1", "TRUE", "'1'", "'TRUE'"):
                return self.bool_literal(True)
            if upper in ("0", "FALSE", "'0'", "'FALSE'"):
                return self.bool_literal(False)
        return raw_default

    def format_value(self, val: Any, fspec_type: str | None = None) -> str:
        if val is None:
            return "NULL"
        if fspec_type == "boolean" or isinstance(val, bool):
            return self.bool_literal(bool(val))
        if isinstance(val, (int, float)):
            return str(val)
        return self.quote_string(str(val))

    def quote_string(self, text: str) -> str:
        return "'" + text.replace("'", "''") + "'"

    def header_sql(self) -> list[str]:
        return []

    def table_suffix_sql(self) -> str:
        return ""

    def render_table(self, table: TableDef) -> str:
        col_lines: list[str] = []
        for col in table.columns:
            if col.is_pk and not table.primary_key_cols:
                col_lines.append(f"    {self.quote_ident(col.name)} {self.pk_auto_type()}")
                continue
            ctype = self.sql_type(
                col.fspec_type, unique=col.unique, is_fk=bool(col.fk_table)
            )
            parts = [f"    {self.quote_ident(col.name)} {ctype}"]
            if not col.nullable or col.is_pk:
                parts.append("NOT NULL")
            if col.unique:
                parts.append("UNIQUE")
            d_expr = self.default_sql(col.default_expr, col.fspec_type)
            if d_expr is not None:
                parts.append(f"DEFAULT {d_expr}")
            if col.fk_table:
                ref = (
                    f"REFERENCES {self.quote_ident(col.fk_table)}"
                    f"({self.quote_ident(col.fk_column)})"
                )
                if col.on_delete:
                    ref += f" ON DELETE {col.on_delete}"
                parts.append(ref)
            col_lines.append(" ".join(parts))

        if table.primary_key_cols:
            pk_sql = ", ".join(self.quote_ident(c) for c in table.primary_key_cols)
            col_lines.append(f"    PRIMARY KEY ({pk_sql})")

        body = ",\n".join(col_lines)
        return f"CREATE TABLE {self.quote_ident(table.name)} (\n{body}\n){self.table_suffix_sql()};"

    def render_insert(
        self,
        table_name: str,
        columns: list[str],
        rows: list[tuple[Any, ...]],
        *,
        col_types: dict[str, str] | None = None,
    ) -> list[str]:
        if not rows:
            return []
        cols_sql = ", ".join(self.quote_ident(c) for c in columns)
        out: list[str] = []
        for row in rows:
            vals_sql = ", ".join(
                self.format_value(v, (col_types or {}).get(col))
                for col, v in zip(columns, row)
            )
            out.append(
                f"INSERT INTO {self.quote_ident(table_name)} ({cols_sql}) VALUES ({vals_sql});"
            )
        return out

    def post_seed_table(self, table_name: str, has_explicit_pk: bool) -> list[str]:
        return []

    def wrap_seed_inserts(
        self, table_name: str, inserts: list[str], has_explicit_pk: bool
    ) -> list[str]:
        return inserts

    def footer_sql(self) -> list[str]:
        return []


class SQLiteDialect(DatabaseDialect):
    id = "sqlite"
    label = "SQLite"
    description = "Embedded SQLite 3 database script"

    def quote_ident(self, ident: str) -> str:
        return '"' + ident.replace('"', '""') + '"'

    def pk_auto_type(self) -> str:
        return "INTEGER PRIMARY KEY AUTOINCREMENT"

    def sql_type(self, fspec_type: str, *, unique: bool = False, is_fk: bool = False) -> str:
        t = fspec_type.lower()
        if t in ("int", "integer") or is_fk:
            return "INTEGER"
        if t in ("decimal", "number", "float", "currency"):
            return "REAL"
        if t == "boolean":
            return "BOOLEAN"
        return "TEXT"

    def bool_literal(self, val: bool) -> str:
        return "1" if val else "0"

    def header_sql(self) -> list[str]:
        return ["PRAGMA foreign_keys = ON;"]


class PostgresDialect(DatabaseDialect):
    id = "postgres"
    label = "PostgreSQL"
    description = "PostgreSQL 14+ SQL initialization script"

    def quote_ident(self, ident: str) -> str:
        return '"' + ident.replace('"', '""') + '"'

    def pk_auto_type(self) -> str:
        return "INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY"

    def sql_type(self, fspec_type: str, *, unique: bool = False, is_fk: bool = False) -> str:
        t = fspec_type.lower()
        if t in ("int", "integer") or is_fk:
            return "INTEGER"
        if t in ("decimal", "number", "float", "currency"):
            return "DOUBLE PRECISION"
        if t == "boolean":
            return "BOOLEAN"
        if t == "date":
            return "DATE"
        if t == "datetime":
            return "TIMESTAMP"
        if t == "time":
            return "TIME"
        return "TEXT"

    def post_seed_table(self, table_name: str, has_explicit_pk: bool) -> list[str]:
        if not has_explicit_pk:
            return []
        qname = table_name.replace("'", "''")
        qtable = self.quote_ident(table_name)
        return [
            f"SELECT setval(pg_get_serial_sequence('{qname}', 'id'), COALESCE((SELECT MAX(id) FROM {qtable}), 1), true);"
        ]


class MySqlDialect(DatabaseDialect):
    id = "mysql"
    label = "MySQL"
    description = "MySQL 8.0+ / MariaDB InnoDB initialization script"

    def quote_ident(self, ident: str) -> str:
        return "`" + ident.replace("`", "``") + "`"

    def pk_auto_type(self) -> str:
        return "INT NOT NULL AUTO_INCREMENT PRIMARY KEY"

    def sql_type(self, fspec_type: str, *, unique: bool = False, is_fk: bool = False) -> str:
        t = fspec_type.lower()
        if t in ("int", "integer") or is_fk:
            return "INT"
        if t in ("decimal", "number", "float", "currency"):
            return "DOUBLE"
        if t == "boolean":
            return "TINYINT(1)"
        if t == "date":
            return "DATE"
        if t == "datetime":
            return "DATETIME"
        if t == "time":
            return "TIME"
        return "VARCHAR(255)" if unique else "LONGTEXT"

    def bool_literal(self, val: bool) -> str:
        return "1" if val else "0"

    def quote_string(self, text: str) -> str:
        return "'" + text.replace("\\", "\\\\").replace("'", "''") + "'"

    def header_sql(self) -> list[str]:
        return ["SET FOREIGN_KEY_CHECKS = 0;"]

    def table_suffix_sql(self) -> str:
        return " ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"

    def footer_sql(self) -> list[str]:
        return ["SET FOREIGN_KEY_CHECKS = 1;"]


class SqlServerDialect(DatabaseDialect):
    id = "mssql"
    label = "SQL Server"
    description = "Microsoft SQL Server (T-SQL) initialization script"

    def quote_ident(self, ident: str) -> str:
        return "[" + ident.replace("]", "]]") + "]"

    def pk_auto_type(self) -> str:
        return "INT IDENTITY(1,1) NOT NULL PRIMARY KEY"

    def sql_type(self, fspec_type: str, *, unique: bool = False, is_fk: bool = False) -> str:
        t = fspec_type.lower()
        if t in ("int", "integer") or is_fk:
            return "INT"
        if t in ("decimal", "number", "float", "currency"):
            return "FLOAT"
        if t == "boolean":
            return "BIT"
        if t == "date":
            return "DATE"
        if t == "datetime":
            return "DATETIME2"
        if t == "time":
            return "TIME"
        return "NVARCHAR(255)" if unique else "NVARCHAR(MAX)"

    def bool_literal(self, val: bool) -> str:
        return "1" if val else "0"

    def quote_string(self, text: str) -> str:
        return "N'" + text.replace("'", "''") + "'"

    def wrap_seed_inserts(
        self, table_name: str, inserts: list[str], has_explicit_pk: bool
    ) -> list[str]:
        if not inserts or not has_explicit_pk:
            return inserts
        qt = self.quote_ident(table_name)
        return [
            f"SET IDENTITY_INSERT {qt} ON;",
            *inserts,
            f"SET IDENTITY_INSERT {qt} OFF;",
        ]


_DIALECT_REGISTRY: dict[str, DatabaseDialect] = {}


def register_dialect(dialect: DatabaseDialect) -> None:
    """Register an RDBMS SQL dialect renderer."""
    _DIALECT_REGISTRY[dialect.id.lower()] = dialect


def get_dialect(dialect_id: str) -> DatabaseDialect:
    """Look up a registered SQL dialect by ID."""
    key = dialect_id.strip().lower()
    if key not in _DIALECT_REGISTRY:
        supported = ", ".join(sorted(_DIALECT_REGISTRY))
        raise ValueError(f"Unsupported SQL dialect '{dialect_id}'. Supported: {supported}.")
    return _DIALECT_REGISTRY[key]


def list_dialects() -> list[dict[str, str]]:
    """Return metadata for all registered RDBMS dialects."""
    order = ["sqlite", "postgres", "mysql", "mssql"]
    ordered_keys = [k for k in order if k in _DIALECT_REGISTRY] + [
        k for k in sorted(_DIALECT_REGISTRY) if k not in order
    ]
    return [
        {
            "id": _DIALECT_REGISTRY[k].id,
            "label": _DIALECT_REGISTRY[k].label,
            "description": _DIALECT_REGISTRY[k].description,
        }
        for k in ordered_keys
    ]


for _builtin in (SQLiteDialect(), PostgresDialect(), MySqlDialect(), SqlServerDialect()):
    register_dialect(_builtin)


_CORE_TABLE_DEFS: list[TableDef] = [
    TableDef(
        name="users",
        columns=[
            ColumnDef("id", "int", is_pk=True, nullable=False),
            ColumnDef("username", "text", nullable=False, unique=True),
            ColumnDef("password_hash", "text", nullable=False, default_expr="''"),
            ColumnDef("is_superuser", "boolean", nullable=False, default_expr="0"),
        ],
    ),
    TableDef(
        name="roles",
        columns=[
            ColumnDef("id", "int", is_pk=True, nullable=False),
            ColumnDef("name", "text", nullable=False, unique=True),
        ],
    ),
    TableDef(
        name="permissions",
        columns=[
            ColumnDef("id", "int", is_pk=True, nullable=False),
            ColumnDef("name", "text", nullable=False, unique=True),
        ],
    ),
    TableDef(
        name="user_roles",
        columns=[
            ColumnDef("user_id", "int", nullable=False, fk_table="users", on_delete="CASCADE"),
            ColumnDef("role_id", "int", nullable=False, fk_table="roles", on_delete="CASCADE"),
        ],
        primary_key_cols=("user_id", "role_id"),
    ),
    TableDef(
        name="role_permissions",
        columns=[
            ColumnDef("role_id", "int", nullable=False, fk_table="roles", on_delete="CASCADE"),
            ColumnDef("permission_id", "int", nullable=False, fk_table="permissions", on_delete="CASCADE"),
        ],
        primary_key_cols=("role_id", "permission_id"),
    ),
    TableDef(
        name="entity",
        columns=[
            ColumnDef("id", "int", is_pk=True, nullable=False),
            ColumnDef("name", "text", nullable=False, unique=True),
            ColumnDef("label", "text", nullable=False),
            ColumnDef("description", "text", nullable=True),
        ],
    ),
    TableDef(
        name="forms",
        columns=[
            ColumnDef("id", "int", is_pk=True, nullable=False),
            ColumnDef("name", "text", nullable=False, unique=True),
            ColumnDef("label", "text", nullable=False),
            ColumnDef("description", "text", nullable=True),
            ColumnDef("entity_id", "int", nullable=True, fk_table="entity", on_delete="SET NULL"),
            ColumnDef("form_type", "text", nullable=False, default_expr="'detail'"),
            ColumnDef("fspec_source", "text", nullable=False),
            ColumnDef("formatter", "text", nullable=False, default_expr="'html5'"),
            ColumnDef("theme", "text", nullable=False, default_expr="'default'"),
            ColumnDef("created_at", "datetime", nullable=False, default_expr="CURRENT_TIMESTAMP"),
            ColumnDef("updated_at", "datetime", nullable=False, default_expr="CURRENT_TIMESTAMP"),
        ],
    ),
    TableDef(
        name="server_module",
        columns=[
            ColumnDef("id", "int", is_pk=True, nullable=False),
            ColumnDef("name", "text", nullable=False, unique=True),
            ColumnDef("label", "text", nullable=False),
            ColumnDef("description", "text", nullable=True),
            ColumnDef("target_runtime", "text", nullable=False, default_expr="'node'"),
            ColumnDef("code", "text", nullable=False),
            ColumnDef("created_at", "datetime", nullable=False, default_expr="CURRENT_TIMESTAMP"),
            ColumnDef("updated_at", "datetime", nullable=False, default_expr="CURRENT_TIMESTAMP"),
        ],
    ),
    TableDef(
        name="server_action",
        columns=[
            ColumnDef("id", "int", is_pk=True, nullable=False),
            ColumnDef("name", "text", nullable=False, unique=True),
            ColumnDef("label", "text", nullable=False),
            ColumnDef("description", "text", nullable=True),
            ColumnDef("entity_id", "int", nullable=True, fk_table="entity", on_delete="SET NULL"),
            ColumnDef("action_type", "text", nullable=False, default_expr="'upsert'"),
            ColumnDef("target_runtime", "text", nullable=False, default_expr="'node'"),
            ColumnDef("code", "text", nullable=False),
            ColumnDef("created_at", "datetime", nullable=False, default_expr="CURRENT_TIMESTAMP"),
            ColumnDef("updated_at", "datetime", nullable=False, default_expr="CURRENT_TIMESTAMP"),
        ],
    ),
    TableDef(
        name="client_module",
        columns=[
            ColumnDef("id", "int", is_pk=True, nullable=False),
            ColumnDef("name", "text", nullable=False, unique=True),
            ColumnDef("label", "text", nullable=False),
            ColumnDef("description", "text", nullable=True),
            ColumnDef("code", "text", nullable=False),
            ColumnDef("created_at", "datetime", nullable=False, default_expr="CURRENT_TIMESTAMP"),
            ColumnDef("updated_at", "datetime", nullable=False, default_expr="CURRENT_TIMESTAMP"),
        ],
    ),
    TableDef(
        name="client_action",
        columns=[
            ColumnDef("id", "int", is_pk=True, nullable=False),
            ColumnDef("name", "text", nullable=False, unique=True),
            ColumnDef("label", "text", nullable=False),
            ColumnDef("description", "text", nullable=True),
            ColumnDef("target_form_id", "int", nullable=True, fk_table="forms", on_delete="SET NULL"),
            ColumnDef("action_type", "text", nullable=False, default_expr="'link'"),
            ColumnDef("params_spec", "text", nullable=True),
            ColumnDef("code", "text", nullable=False, default_expr="''"),
            ColumnDef("created_at", "datetime", nullable=False, default_expr="CURRENT_TIMESTAMP"),
            ColumnDef("updated_at", "datetime", nullable=False, default_expr="CURRENT_TIMESTAMP"),
        ],
    ),
]


def _topological_entity_tables(domain_entities: list[dict[str, Any]]) -> list[TableDef]:
    """Order domain entity TableDefs so referenced parent tables precede child FK tables."""
    defs_by_name: dict[str, TableDef] = {}
    deps: dict[str, set[str]] = {}
    for ent in domain_entities:
        name = str(ent["name"])
        cols: list[ColumnDef] = []
        ent_deps: set[str] = set()
        for col in ent.get("columns", []):
            fk_tbl = col.get("fk_table")
            if fk_tbl and fk_tbl != name:
                ent_deps.add(str(fk_tbl))
            cols.append(
                ColumnDef(
                    name=str(col["name"]),
                    fspec_type=str(col.get("fspec_type", "text")),
                    is_pk=bool(col.get("is_pk", False)),
                    nullable=bool(col.get("nullable", True)),
                    default_expr=col.get("default"),
                    fk_table=fk_tbl,
                    fk_column=str(col.get("fk_column") or "id"),
                )
            )
        defs_by_name[name] = TableDef(name=name, columns=cols)
        deps[name] = ent_deps

    children: dict[str, set[str]] = {n: set() for n in defs_by_name}
    indegree: dict[str, int] = {n: 0 for n in defs_by_name}
    for n, parent_set in deps.items():
        for p in parent_set:
            if p in defs_by_name:
                children[p].add(n)
                indegree[n] += 1

    queue = deque(sorted(n for n, deg in indegree.items() if deg == 0))
    ordered: list[TableDef] = []
    seen: set[str] = set()
    while queue:
        cur = queue.popleft()
        seen.add(cur)
        ordered.append(defs_by_name[cur])
        for ch in sorted(children[cur]):
            indegree[ch] -= 1
            if indegree[ch] == 0:
                queue.append(ch)
    for n in sorted(defs_by_name):
        if n not in seen:
            ordered.append(defs_by_name[n])
    return ordered


def _fetch_table_rows(
    adapter: WorkspaceAdapter, table_name: str, columns: list[str]
) -> list[tuple[Any, ...]]:
    conn = getattr(adapter, "conn", None)
    if conn is None:
        return []
    cols_sql = ", ".join('"' + c.replace('"', '""') + '"' for c in columns)
    qtable = '"' + table_name.replace('"', '""') + '"'
    order_col = '"id"' if "id" in columns else ", ".join('"' + c.replace('"', '""') + '"' for c in columns)
    rows = conn.execute(f"SELECT {cols_sql} FROM {qtable} ORDER BY {order_col}").fetchall()
    return [tuple(r[c] for c in columns) for r in rows]


def generate_db_init_sql(
    adapter: WorkspaceAdapter,
    dialect_id: str,
    *,
    include_entity_rows: bool = False,
) -> str:
    """Generate a full RDBMS init script creating tables and seeding RBAC & app metadata."""
    dialect = get_dialect(dialect_id)
    lines: list[str] = [
        f"-- FormSpec Web App Initialization Script ({dialect.label})",
        f"-- Target dialect: {dialect.id}",
        "",
    ]
    lines.extend(dialect.header_sql())
    if dialect.header_sql():
        lines.append("")

    domain_entities = adapter.list_domain_entities()
    domain_tables = _topological_entity_tables(domain_entities)

    lines.append("-- 1. Core RBAC & Application Schema Tables")
    for tdef in _CORE_TABLE_DEFS:
        lines.append(dialect.render_table(tdef))
    lines.append("")

    if domain_tables:
        lines.append("-- 2. Domain Entity Tables")
        for tdef in domain_tables:
            lines.append(dialect.render_table(tdef))
        lines.append("")

    seed_specs: list[tuple[str, list[str], dict[str, str], bool]] = [
        ("roles", ["id", "name"], {}, True),
        ("permissions", ["id", "name"], {}, True),
        ("role_permissions", ["role_id", "permission_id"], {}, False),
        ("entity", ["id", "name", "label", "description"], {}, True),
        (
            "forms",
            ["id", "name", "label", "description", "entity_id", "form_type", "fspec_source", "formatter", "theme"],
            {},
            True,
        ),
        (
            "server_module",
            ["id", "name", "label", "description", "target_runtime", "code"],
            {},
            True,
        ),
        (
            "server_action",
            ["id", "name", "label", "description", "entity_id", "action_type", "target_runtime", "code"],
            {},
            True,
        ),
        (
            "client_module",
            ["id", "name", "label", "description", "code"],
            {},
            True,
        ),
        (
            "client_action",
            ["id", "name", "label", "description", "target_form_id", "action_type", "params_spec", "code"],
            {},
            True,
        ),
    ]

    lines.append("-- 3. Seeded Roles, Permissions, Forms, Actions & Modules")
    for tbl, cols, col_types, has_pk in seed_specs:
        rows = _fetch_table_rows(adapter, tbl, cols)
        if not rows:
            continue
        inserts = dialect.render_insert(tbl, cols, rows, col_types=col_types)
        lines.extend(dialect.wrap_seed_inserts(tbl, inserts, has_pk))
        lines.extend(dialect.post_seed_table(tbl, has_pk))
    lines.append("")

    if include_entity_rows and domain_tables:
        lines.append("-- 4. Domain Entity Seed Data")
        for tdef in domain_tables:
            cols = [c.name for c in tdef.columns]
            col_types = {c.name: c.fspec_type for c in tdef.columns}
            rows = _fetch_table_rows(adapter, tdef.name, cols)
            if not rows:
                continue
            has_pk = any(c.is_pk for c in tdef.columns)
            inserts = dialect.render_insert(tdef.name, cols, rows, col_types=col_types)
            lines.extend(dialect.wrap_seed_inserts(tdef.name, inserts, has_pk))
            lines.extend(dialect.post_seed_table(tdef.name, has_pk))
        lines.append("")

    lines.extend(dialect.footer_sql())
    return "\n".join(lines).rstrip() + "\n"
