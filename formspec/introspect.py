"""Entity, `_id` relationship, and RBAC introspection."""

from __future__ import annotations

from formspec.db.base import (
    ColumnSchema,
    DatabaseAdapter,
    EntityLink,
    EntitySchema,
)


def humanize_identifier(name: str) -> str:
    """Convert a snake_case identifier (stripping `_id`) to a human-readable label."""
    base = name[:-3] if name.endswith("_id") and len(name) > 3 else name
    return base.replace("_", " ").strip().title()


def _resolve_entity_link(
    adapter: DatabaseAdapter,
    col: ColumnSchema,
    all_tables: set[str],
    table_cols_cache: dict[str, set[str]],
) -> EntityLink | None:
    """Resolve an `_id` attribute to its singular target entity and display column."""
    if not col.name.endswith("_id") or len(col.name) <= 3:
        return None

    target_table = col.fk_table or col.name[:-3]
    if target_table not in all_tables:
        return None

    if target_table not in table_cols_cache:
        table_cols_cache[target_table] = {
            c.name for c in adapter.introspect_table(target_table)
        }
    target_cols = table_cols_cache[target_table]
    if not target_cols:
        return None

    key_col = (
        col.fk_column
        if col.fk_column and col.fk_column in target_cols
        else ("id" if "id" in target_cols else next(iter(sorted(target_cols))))
    )

    target_meta = adapter.read_entity_metadata(target_table)
    if (
        target_meta is not None
        and target_meta.display_attribute
        and target_meta.display_attribute in target_cols
    ):
        display_col = target_meta.display_attribute
    elif "name" in target_cols:
        display_col = "name"
    else:
        display_col = key_col

    return EntityLink(
        target_table=target_table,
        key_col=key_col,
        display_col=display_col,
    )


def introspect_entity(
    adapter: DatabaseAdapter,
    entity_slug: str,
    table_name: str | None = None,
) -> EntitySchema:
    """Introspect a singular database entity, its `_id` links, and RBAC permissions."""
    target_table = table_name or entity_slug
    all_tables = set(adapter.list_tables())
    columns = adapter.introspect_table(target_table)

    table_cols_cache: dict[str, set[str]] = {
        target_table: {c.name for c in columns}
    }
    for col in columns:
        if col.is_pk and col.name == "id":
            continue
        col.entity_link = _resolve_entity_link(
            adapter, col, all_tables, table_cols_cache
        )

    meta = adapter.read_entity_metadata(entity_slug)
    display_name = (
        meta.display_name
        if meta and meta.display_name
        else humanize_identifier(entity_slug)
    )

    pk_col = next((c.name for c in columns if c.is_pk), None)
    if pk_col is None:
        pk_col = "id" if "id" in table_cols_cache[target_table] else columns[0].name

    rbac = adapter.read_rbac_permissions(entity_slug, [c.name for c in columns])
    return EntitySchema(
        table_name=target_table,
        entity_slug=entity_slug,
        display_name=display_name,
        pk_column=pk_col,
        columns=columns,
        rbac=rbac,
    )
