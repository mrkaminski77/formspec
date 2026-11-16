"""Pluggable formatter contract, render context, and dynamic plugin registry."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import importlib.util
import inspect
from pathlib import Path
from typing import Any, Literal

from formspec.db.base import AccessLevel
from formspec.parser import (
    AstNode,
    BlockNode,
    FormDocument,
    ParseDiagnostic,
    RowNode,
    resolve_effective_permission,
)


@dataclass
class RenderContext:
    """Runtime context passed to a FormFormatter."""

    role: str = "superuser"
    options_by_field: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    list_rows_by_entity: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    values: dict[str, Any] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)
    diagnostics: list[ParseDiagnostic] = field(default_factory=list)


@dataclass(frozen=True)
class FormattedOutput:
    """Output returned by a FormFormatter."""

    formatter_id: str
    output_kind: Literal["html", "code", "json"]
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)


def node_has_visible_descendant(
    node: AstNode,
    perm_chain: list[dict[str, AccessLevel] | None],
    role: str,
) -> bool:
    """Return True if `node` or any descendant resolves to >= `read` for `role`."""
    mods = getattr(node, "modifiers", None)
    next_chain = perm_chain + ([mods.permissions] if mods and mods.permissions else [])
    if isinstance(node, BlockNode):
        return any(node_has_visible_descendant(ch, next_chain, role) for ch in node.children)
    if isinstance(node, RowNode):
        return any(node_has_visible_descendant(el, next_chain, role) for el in node.elements)
    if mods is not None or hasattr(node, "field_id") or hasattr(node, "action_id"):
        return resolve_effective_permission(next_chain, role) != "none"
    return resolve_effective_permission(next_chain, role) != "none"


class FormFormatter(ABC):
    """Abstract base class for pluggable FormSpec translators."""

    id: str
    name: str
    description: str
    output_kind: Literal["html", "code", "json"] = "html"

    @abstractmethod
    def render(self, document: FormDocument, ctx: RenderContext) -> FormattedOutput:
        """Translate a parsed FormDocument into target markup or code."""


class FormatterRegistry:
    """Registry of built-in and dynamically loaded FormFormatter plugins."""

    def __init__(self) -> None:
        self._formatters: dict[str, FormFormatter] = {}

    def register(self, formatter: FormFormatter) -> None:
        self._formatters[formatter.id] = formatter

    def get(self, formatter_id: str) -> FormFormatter:
        if formatter_id not in self._formatters:
            available = ", ".join(sorted(self._formatters))
            raise KeyError(
                f"Unknown formatter '{formatter_id}'. Available: {available}."
            )
        return self._formatters[formatter_id]

    def list_metadata(self) -> list[dict[str, str]]:
        return [
            {
                "id": f.id,
                "name": f.name,
                "description": f.description,
                "output_kind": f.output_kind,
            }
            for f in self._formatters.values()
        ]

    def load_plugins_from_dir(self, directory: str | Path) -> list[str]:
        """Discover and register concrete FormFormatter subclasses in `*.py` files."""
        plugin_dir = Path(directory)
        if not plugin_dir.is_dir():
            return []

        loaded: list[str] = []
        for py_file in sorted(plugin_dir.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            spec = importlib.util.spec_from_file_location(
                f"formspec_plugin_{py_file.stem}", py_file
            )
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            for _, obj in inspect.getmembers(module, inspect.isclass):
                if (
                    issubclass(obj, FormFormatter)
                    and obj is not FormFormatter
                    and obj.__module__ == module.__name__
                ):
                    instance = obj()
                    self.register(instance)
                    loaded.append(instance.id)
        return loaded
