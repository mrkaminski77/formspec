"""Startup empty-database check and auto-initialization for the packaged web app."""

from __future__ import annotations

import os
from pathlib import Path

from formspec.db import get_adapter
from formspec.db.base import WorkspaceAdapter
from formspec.ide.auth import hash_password

_REQUIRED_APP_TABLES = frozenset(
    {
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
    }
)


def resolve_runtime_dsn(explicit_dsn: str | None = None) -> str:
    """Resolve runtime DB connection string: CLI > DATABASE_CONNECTION_STRING > SQLite default."""
    return (
        explicit_dsn
        or os.environ.get("DATABASE_CONNECTION_STRING")
        or os.environ.get("FORMSPEC_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
        or "sqlite:///./data/app.db"
    )


def detect_dialect_from_dsn(dsn: str) -> str:
    """Infer target RDBMS dialect ID from a connection string."""
    lower = dsn.strip().lower()
    if "://" not in lower or lower.startswith("sqlite"):
        return "sqlite"
    if lower.startswith(("postgresql", "postgres")):
        return "postgres"
    if lower.startswith(("mysql", "mariadb")):
        return "mysql"
    if lower.startswith(("mssql", "sqlserver", "odbc")):
        return "mssql"
    return "sqlite"


def is_database_empty(adapter: WorkspaceAdapter) -> bool:
    """Return True if the database has no tables or is missing core application tables."""
    existing = set(adapter.list_tables())
    if not existing:
        return True
    return not _REQUIRED_APP_TABLES.issubset(existing)


def resolve_init_sql_path(
    dialect: str,
    explicit_path: str | Path | None = None,
    app_data_dir: str | Path | None = None,
) -> Path | None:
    """Locate the initialization SQL script matching `dialect` (with SQLite fallback)."""
    if explicit_path:
        p = Path(explicit_path)
        return p if p.is_file() else None

    search_roots = [
        Path(app_data_dir) if app_data_dir else Path.cwd() / "app_data",
        Path(__file__).resolve().parents[2] / "app_data",
    ]
    for root in search_roots:
        for candidate in (
            root / f"init_db.{dialect}.sql",
            root / "init_db.sql",
            root / "init_db.sqlite.sql" if dialect == "sqlite" else None,
        ):
            if candidate is not None and candidate.is_file():
                return candidate
    return None


def _bootstrap_admin_if_configured(adapter: WorkspaceAdapter) -> None:
    """If FORMSPEC_ADMIN_PASSWORD is set and no superuser exists, seed the admin user."""
    pw = os.environ.get("FORMSPEC_ADMIN_PASSWORD") or os.environ.get("ADMIN_PASSWORD")
    if pw and not adapter.has_superuser():
        username = (os.environ.get("FORMSPEC_ADMIN_USERNAME") or "admin").strip()
        adapter.create_superuser(username, hash_password(pw))


def ensure_runtime_database_initialized(
    dsn: str,
    *,
    init_sql_path: str | Path | None = None,
    app_data_dir: str | Path | None = None,
) -> bool:
    """Check if DB is empty on launch; if empty, execute init script and return True."""
    dialect = detect_dialect_from_dsn(dsn)
    if dialect == "sqlite" and ":///" in dsn:
        raw_path = dsn.split(":///", 1)[1]
        if raw_path != ":memory:":
            Path(raw_path).parent.mkdir(parents=True, exist_ok=True)

    with get_adapter(dsn) as adapter:
        if not isinstance(adapter, WorkspaceAdapter):
            raise RuntimeError("Configured DatabaseAdapter must implement WorkspaceAdapter.")

        if not is_database_empty(adapter):
            _bootstrap_admin_if_configured(adapter)
            return False

        sql_file = resolve_init_sql_path(
            dialect, explicit_path=init_sql_path, app_data_dir=app_data_dir
        )
        if sql_file is not None:
            sql_text = sql_file.read_text(encoding="utf-8")
            conn = getattr(adapter, "conn", None)
            if conn is not None and dialect == "sqlite":
                conn.executescript(sql_text)
            elif hasattr(adapter, "execute_script"):
                adapter.execute_script(sql_text)  # type: ignore[attr-defined]
            else:
                adapter.initialize_schema()
        else:
            adapter.initialize_schema()

        _bootstrap_admin_if_configured(adapter)
        return True
