"""Standalone packaged web application runtime (distinct from the IDE)."""

from formspec.runtime.auth import (
    AuthProvider,
    AuthUser,
    AzureEasyAuthProvider,
    LocalDatabaseAuthProvider,
    get_auth_provider,
    list_auth_providers,
    register_auth_provider,
)
from formspec.runtime.db_bootstrap import (
    detect_dialect_from_dsn,
    ensure_runtime_database_initialized,
    resolve_runtime_dsn,
)
from formspec.runtime.server import AppRuntime, create_app_server

__all__ = [
    "AppRuntime",
    "AuthProvider",
    "AuthUser",
    "AzureEasyAuthProvider",
    "LocalDatabaseAuthProvider",
    "create_app_server",
    "detect_dialect_from_dsn",
    "ensure_runtime_database_initialized",
    "get_auth_provider",
    "list_auth_providers",
    "register_auth_provider",
    "resolve_runtime_dsn",
]
