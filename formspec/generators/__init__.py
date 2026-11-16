"""High-level generator orchestration for FormSpec and server-action targets."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from formspec.db import get_adapter
from formspec.generators.client_action import (
    CLIENT_MODULE_NAME,
    HEADER_MODULE_NAME,
    generate_detail_client_action,
    generate_list_client_action,
    get_client_module_code,
    get_header_client_module_code,
)
from formspec.generators.fspec import generate_fspec
from formspec.generators.server_action_node import generate_node_server_action
from formspec.generators.server_action_python import generate_python_server_action
from formspec.generators.server_module_node import (
    SERVER_MODULE_NAME,
    get_node_server_module_code,
)
from formspec.introspect import introspect_entity

TargetType = Literal["python", "node", "both"]


@dataclass(frozen=True)
class GeneratedArtifacts:
    """Paths written by `generate_for_entity`."""

    entity_slug: str
    fspec_path: Path
    action_paths: list[Path]


def generate_for_entity(
    dsn: str,
    entity_slug: str,
    out_dir: str | Path,
    target: TargetType = "python",
    include_pk: bool = False,
    layout: str = "single",
) -> GeneratedArtifacts:
    """Introspect `entity_slug` from `dsn` and write `.fspec` and server-action scripts."""
    output_root = Path(out_dir)
    output_root.mkdir(parents=True, exist_ok=True)

    with get_adapter(dsn) as adapter:
        schema = introspect_entity(adapter, entity_slug=entity_slug)

    fspec_content = generate_fspec(schema, include_pk=include_pk, layout=layout)
    fspec_path = output_root / f"{entity_slug}.fspec"
    fspec_path.write_text(fspec_content, encoding="utf-8")

    action_paths: list[Path] = []
    if target in ("python", "both"):
        py_path = output_root / f"{entity_slug}_action.py"
        py_path.write_text(generate_python_server_action(schema), encoding="utf-8")
        action_paths.append(py_path)

    if target in ("node", "both"):
        mod_path = output_root / f"{SERVER_MODULE_NAME}.mjs"
        mod_path.write_text(get_node_server_module_code(), encoding="utf-8")
        action_paths.append(mod_path)
        node_path = output_root / f"{entity_slug}_action.mjs"
        node_path.write_text(generate_node_server_action(schema), encoding="utf-8")
        action_paths.append(node_path)

    if not action_paths:
        raise ValueError(f"Unsupported target '{target}'. Expected 'python', 'node', or 'both'.")

    return GeneratedArtifacts(
        entity_slug=entity_slug,
        fspec_path=fspec_path,
        action_paths=action_paths,
    )


__all__ = [
    "CLIENT_MODULE_NAME",
    "GeneratedArtifacts",
    "HEADER_MODULE_NAME",
    "SERVER_MODULE_NAME",
    "TargetType",
    "generate_detail_client_action",
    "generate_for_entity",
    "generate_fspec",
    "generate_list_client_action",
    "generate_node_server_action",
    "generate_python_server_action",
    "get_client_module_code",
    "get_header_client_module_code",
    "get_node_server_module_code",
]
