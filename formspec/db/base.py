"""Base database adapter abstractions, schema models, and RBAC permission helpers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import re
from typing import Any, Literal

AccessLevel = Literal["none", "read", "write"]

_VALID_IDENT_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def validate_identifier(name: str, kind: str = "identifier") -> str:
    """Validate that `name` is a safe lowercase snake_case SQL identifier."""
    cleaned = name.strip()
    if not _VALID_IDENT_RE.match(cleaned):
        raise ValueError(
            f"Invalid {kind} '{name}': must match ^[a-z][a-z0-9_]*$ (lowercase snake_case)."
        )
    return cleaned

ACCESS_ORDER: dict[AccessLevel, int] = {
    "none": 0,
    "read": 1,
    "write": 2,
}


def max_access(a: AccessLevel, b: AccessLevel) -> AccessLevel:
    """Return the higher privilege between two AccessLevel values."""
    return a if ACCESS_ORDER[a] >= ACCESS_ORDER[b] else b


def parse_permission_string(
    perm_str: str,
    entity_slug: str,
    attribute_names: set[str],
) -> tuple[str | None, AccessLevel] | None:
    """Parse `<entity_slug>:<access>` or `<entity_slug>:<access>_<attribute>`.

    Returns `(None, access)` for entity-level permissions, `(attribute, access)`
    for attribute-level permissions, or `None` if unrelated to `entity_slug`.
    """
    prefix = f"{entity_slug}:"
    if not perm_str.startswith(prefix):
        return None

    spec = perm_str[len(prefix) :]
    if spec in ACCESS_ORDER:
        return (None, spec)  # type: ignore[return-value]

    for level in ("write", "read", "none"):
        level_prefix = f"{level}_"
        if spec.startswith(level_prefix):
            attr = spec[len(level_prefix) :]
            if attr in attribute_names:
                return (attr, level)  # type: ignore[return-value]
            return None

    return None


@dataclass(frozen=True)
class EntityLink:
    """Represents a foreign-key / `_id` reference to a target entity."""

    target_table: str
    key_col: str = "id"
    display_col: str = "name"


@dataclass
class ColumnSchema:
    """Normalized column metadata from a database table."""

    name: str
    sql_type: str
    fspec_type: str
    nullable: bool
    default: str | None = None
    is_pk: bool = False
    fk_table: str | None = None
    fk_column: str | None = None
    entity_link: EntityLink | None = None


@dataclass(frozen=True)
class EntityMetadata:
    """Optional display metadata stored in the `entity` table."""

    slug: str
    display_name: str | None = None
    display_attribute: str | None = None


@dataclass(frozen=True)
class RbacTableConfig:
    """Discovered RBAC table and column names for runtime query generation."""

    users_table: str = "users"
    roles_table: str = "roles"
    permissions_table: str = "permissions"
    user_roles_table: str = "user_roles"
    role_permissions_table: str = "role_permissions"
    role_name_col: str = "name"
    perm_name_col: str = "name"


@dataclass
class RolePermissionMatrix:
    """Resolved role-based permissions for an entity and its attributes."""

    roles: list[str] = field(default_factory=list)
    entity_perms: dict[str, AccessLevel] = field(default_factory=dict)
    attribute_perms: dict[str, dict[str, AccessLevel]] = field(default_factory=dict)
    rbac_config: RbacTableConfig = field(default_factory=RbacTableConfig)

    def effective_role_perm(self, role: str, attribute: str | None = None) -> AccessLevel:
        """Compute effective permission for `role` on an entity or attribute."""
        base = self.entity_perms.get(role, "none")
        if attribute is None:
            return base
        attr_override = self.attribute_perms.get(attribute, {}).get(role, "none")
        return max_access(base, attr_override)

    def write_capable_roles(self) -> list[str]:
        """Return roles holding `write` on the entity or at least one attribute."""
        writers: list[str] = []
        for role in self.roles:
            if self.entity_perms.get(role) == "write":
                writers.append(role)
                continue
            if any(
                role_map.get(role) == "write"
                for role_map in self.attribute_perms.values()
            ):
                writers.append(role)
        return writers


@dataclass
class EntitySchema:
    """Complete introspected representation of an entity for code/form generation."""

    table_name: str
    entity_slug: str
    display_name: str
    pk_column: str
    columns: list[ColumnSchema]
    rbac: RolePermissionMatrix


class DatabaseAdapter(ABC):
    """Abstract interface for database schema and RBAC introspection."""

    def __enter__(self) -> DatabaseAdapter:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    @abstractmethod
    def close(self) -> None:
        """Close underlying database connection."""

    @abstractmethod
    def list_tables(self) -> list[str]:
        """Return all user table names in the database."""

    @abstractmethod
    def introspect_table(self, table_name: str) -> list[ColumnSchema]:
        """Return normalized ColumnSchema entries for `table_name`."""

    @abstractmethod
    def read_entity_metadata(self, entity_slug: str) -> EntityMetadata | None:
        """Read display metadata from the `entity` table if present."""

    @abstractmethod
    def read_rbac_permissions(
        self,
        entity_slug: str,
        attribute_names: list[str],
    ) -> RolePermissionMatrix:
        """Read role permissions matching `<entity_slug>:<access>[_<attr>]`."""


@dataclass(frozen=True)
class AttributeSpec:
    """Column specification used when creating entities or adding attributes."""

    name: str
    fspec_type: str = "text"
    nullable: bool = True
    default: str | None = None
    fk_table: str | None = None


@dataclass(frozen=True)
class FormRecord:
    """Stored form definition row from the `forms` table."""

    id: int
    name: str
    label: str
    description: str | None
    entity_id: int | None
    entity_name: str | None
    fspec_source: str
    formatter: str
    theme: str
    created_at: str
    updated_at: str
    form_type: str = "detail"


@dataclass(frozen=True)
class UserRecord:
    """Stored user authentication record from `users`."""

    id: int
    username: str
    password_hash: str
    is_superuser: bool
    roles: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RbacSnapshot:
    """Complete RBAC state for the IDE permissions panel."""

    roles: list[str]
    permissions: list[str]
    grants: dict[str, list[str]]


@dataclass(frozen=True)
class ServerModuleRecord:
    """Stored reusable server module row from `server_module`."""

    id: int
    name: str
    label: str
    description: str | None
    target_runtime: str
    code: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ClientModuleRecord:
    """Stored reusable client module row from `client_module`."""

    id: int
    name: str
    label: str
    description: str | None
    code: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ServerActionRecord:
    """Stored server action row from the `server_action` table."""

    id: int
    name: str
    label: str
    description: str | None
    entity_id: int | None
    entity_name: str | None
    action_type: str
    target_runtime: str
    code: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ClientActionRecord:
    """Stored client action row from the `client_action` table."""

    id: int
    name: str
    label: str
    description: str | None
    target_form_id: int | None
    target_form_name: str | None
    action_type: str
    params_spec: str | None
    code: str
    created_at: str
    updated_at: str


class WorkspaceAdapter(DatabaseAdapter, ABC):
    """Extended database adapter supporting IDE bootstrap, DDL, RBAC, and forms."""

    @abstractmethod
    def is_initialized(self) -> bool:
        """Return True if core RBAC, entity, and forms tables exist."""

    @abstractmethod
    def has_superuser(self) -> bool:
        """Return True if at least one superuser exists in `users`."""

    @abstractmethod
    def initialize_schema(self) -> None:
        """Create core RBAC, entity, and forms tables and seed default roles."""

    @abstractmethod
    def create_superuser(self, username: str, password_hash: str) -> int:
        """Create the initial superuser and assign the `admin` role."""

    @abstractmethod
    def get_user_by_username(self, username: str) -> UserRecord | None:
        """Fetch user credentials and assigned roles by username."""

    @abstractmethod
    def get_user_by_id(self, user_id: int) -> UserRecord | None:
        """Fetch user record and assigned roles by user ID."""

    @abstractmethod
    def list_domain_entities(self) -> list[dict[str, Any]]:
        """List domain entity tables with metadata and column details."""

    @abstractmethod
    def create_entity(
        self,
        name: str,
        label: str,
        description: str | None = None,
        attributes: list[AttributeSpec] | None = None,
    ) -> int:
        """Create a singular entity table with `id` PK, metadata row, and default permissions."""

    @abstractmethod
    def add_attribute(self, entity_name: str, spec: AttributeSpec) -> None:
        """Add a scalar column or `<target>_id` foreign-key column to an entity table."""

    @abstractmethod
    def list_rbac_snapshot(self) -> RbacSnapshot:
        """Return all roles, permissions, and role->permission mappings."""

    @abstractmethod
    def create_role(self, role_name: str) -> None:
        """Create a new RBAC role."""

    @abstractmethod
    def ensure_permission(self, perm_name: str) -> None:
        """Ensure a permission string exists in `permissions`."""

    @abstractmethod
    def set_role_permission(
        self, role_name: str, perm_name: str, granted: bool
    ) -> None:
        """Grant or revoke a permission on a role."""

    @abstractmethod
    def list_forms(self) -> list[FormRecord]:
        """List all saved forms in `forms`."""

    @abstractmethod
    def get_form(self, form_id: int) -> FormRecord | None:
        """Fetch a single form by ID."""

    @abstractmethod
    def get_form_by_name(self, name: str) -> FormRecord | None:
        """Fetch a single form by unique name."""

    @abstractmethod
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
        """Insert or update a form by unique name."""

    @abstractmethod
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
        """Insert a new form record into `forms`."""

    @abstractmethod
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
        """Update an existing form record in `forms`."""

    @abstractmethod
    def delete_form(self, form_id: int) -> None:
        """Delete a form record by ID."""

    @abstractmethod
    def query_entity_options(
        self,
        table_name: str,
        key_col: str = "id",
        display_col: str = "name",
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Query `{id, name}` option rows for live form previews."""

    @abstractmethod
    def query_entity_records(
        self,
        table_name: str,
        filters: dict[str, Any] | None = None,
        search: str | None = None,
        order_col: str = "id",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Query optionally filtered rows from `table_name` for list forms."""

    @abstractmethod
    def get_entity_record(
        self,
        table_name: str,
        record_id: Any,
        pk_col: str = "id",
    ) -> dict[str, Any] | None:
        """Fetch a single entity record by primary key."""

    @abstractmethod
    def list_all_tables_schema(self) -> list[dict[str, Any]]:
        """List all database tables (including RBAC and system tables) with columns and FKs."""

    @abstractmethod
    def execute_sql(self, sql: str, limit: int = 500) -> dict[str, Any]:
        """Execute a single SQL query and return columns, rows, or affected count."""

    @abstractmethod
    def list_server_actions(self) -> list[ServerActionRecord]:
        """List all saved server actions."""

    @abstractmethod
    def get_server_action(self, action_id: int) -> ServerActionRecord | None:
        """Fetch a server action by ID."""

    @abstractmethod
    def get_server_action_by_name(self, name: str) -> ServerActionRecord | None:
        """Fetch a server action by unique name."""

    @abstractmethod
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
        """Insert or update a server action by unique name."""

    @abstractmethod
    def update_server_action(
        self,
        action_id: int,
        *,
        label: str | None = None,
        description: str | None = None,
        code: str | None = None,
        target_runtime: str | None = None,
    ) -> ServerActionRecord:
        """Update an existing server action by ID."""

    @abstractmethod
    def delete_server_action(self, action_id: int) -> None:
        """Delete a server action by ID."""

    @abstractmethod
    def list_client_actions(self) -> list[ClientActionRecord]:
        """List all saved client actions."""

    @abstractmethod
    def get_client_action(self, action_id: int) -> ClientActionRecord | None:
        """Fetch a client action by ID."""

    @abstractmethod
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
        """Create a new client action."""

    @abstractmethod
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
        """Update an existing client action."""

    @abstractmethod
    def delete_client_action(self, action_id: int) -> None:
        """Delete a client action by ID."""

    @abstractmethod
    def get_client_action_by_name(self, name: str) -> ClientActionRecord | None:
        """Fetch a client action by unique name."""

    @abstractmethod
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
        """Insert or update a client action by unique name."""

    @abstractmethod
    def list_server_modules(self) -> list[ServerModuleRecord]:
        """List all saved server modules."""

    @abstractmethod
    def get_server_module(self, module_id: int) -> ServerModuleRecord | None:
        """Fetch a server module by ID."""

    @abstractmethod
    def get_server_module_by_name(self, name: str) -> ServerModuleRecord | None:
        """Fetch a server module by unique name."""

    @abstractmethod
    def save_server_module(
        self,
        name: str,
        label: str,
        code: str,
        *,
        description: str | None = None,
        target_runtime: str = "node",
    ) -> ServerModuleRecord:
        """Insert or update a server module by unique name."""

    @abstractmethod
    def update_server_module(
        self,
        module_id: int,
        *,
        label: str | None = None,
        description: str | None = None,
        code: str | None = None,
        target_runtime: str | None = None,
    ) -> ServerModuleRecord:
        """Update an existing server module by ID."""

    @abstractmethod
    def delete_server_module(self, module_id: int) -> None:
        """Delete a server module by ID."""

    @abstractmethod
    def list_client_modules(self) -> list[ClientModuleRecord]:
        """List all saved client modules."""

    @abstractmethod
    def get_client_module(self, module_id: int) -> ClientModuleRecord | None:
        """Fetch a client module by ID."""

    @abstractmethod
    def get_client_module_by_name(self, name: str) -> ClientModuleRecord | None:
        """Fetch a client module by unique name."""

    @abstractmethod
    def save_client_module(
        self,
        name: str,
        label: str,
        code: str,
        *,
        description: str | None = None,
    ) -> ClientModuleRecord:
        """Insert or update a client module by unique name."""

    @abstractmethod
    def update_client_module(
        self,
        module_id: int,
        *,
        label: str | None = None,
        description: str | None = None,
        code: str | None = None,
    ) -> ClientModuleRecord:
        """Update an existing client module by ID."""

    @abstractmethod
    def delete_client_module(self, module_id: int) -> None:
        """Delete a client module by ID."""
