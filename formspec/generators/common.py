"""Shared configuration builder for server-action code generators."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from formspec.db.base import EntitySchema


def build_entity_config(schema: EntitySchema) -> dict[str, Any]:
    """Serialize an EntitySchema into the runtime config dict embedded in server actions."""
    columns_cfg: list[dict[str, Any]] = []
    for col in schema.columns:
        columns_cfg.append(
            {
                "name": col.name,
                "fspec_type": col.fspec_type,
                "required": (not col.nullable),
                "has_default": (col.default is not None),
                "is_pk": col.is_pk,
                "entity_link": (
                    {
                        "table": col.entity_link.target_table,
                        "key_col": col.entity_link.key_col,
                        "display_col": col.entity_link.display_col,
                    }
                    if col.entity_link is not None
                    else None
                ),
            }
        )

    return {
        "entity_slug": schema.entity_slug,
        "table_name": schema.table_name,
        "display_name": schema.display_name,
        "pk_column": schema.pk_column,
        "rbac": asdict(schema.rbac.rbac_config),
        "columns": columns_cfg,
    }
