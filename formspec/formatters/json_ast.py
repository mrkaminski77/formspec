"""Structured JSON AST formatter (`json-ast`)."""

from __future__ import annotations

from dataclasses import asdict
import json
from typing import Any

from formspec.db.base import AccessLevel
from formspec.formatters.base import FormFormatter, FormattedOutput, RenderContext
from formspec.parser import (
    AstNode,
    BlockNode,
    FormDocument,
    RowNode,
    resolve_effective_permission,
)


def _serialize_node(
    node: AstNode,
    perm_chain: list[dict[str, AccessLevel] | None],
    role: str,
) -> dict[str, Any]:
    mods = getattr(node, "modifiers", None)
    next_chain = [*perm_chain, mods.permissions] if mods and mods.permissions else perm_chain
    data = asdict(node)
    data["node_type"] = type(node).__name__
    data["effective_permission"] = resolve_effective_permission(next_chain, role)

    if isinstance(node, BlockNode):
        data["children"] = [
            _serialize_node(ch, next_chain, role) for ch in node.children
        ]
    elif isinstance(node, RowNode):
        data["elements"] = [
            _serialize_node(el, next_chain, role) for el in node.elements
        ]
    return data


class JsonAstFormatter(FormFormatter):
    """Serializes the FormSpec AST and resolved role permissions as formatted JSON."""

    id = "json-ast"
    name = "JSON AST"
    description = "Structured JSON FormSpec AST with resolved permissions and options"
    output_kind = "json"

    def render(self, document: FormDocument, ctx: RenderContext) -> FormattedOutput:
        payload = {
            "role": ctx.role,
            "diagnostics": [asdict(d) for d in document.diagnostics],
            "forms": [_serialize_node(f, [], ctx.role) for f in document.forms],
            "options": ctx.options_by_field,
        }
        return FormattedOutput(
            formatter_id=self.id,
            output_kind="json",
            content=json.dumps(payload, indent=2),
            metadata={"role": ctx.role},
        )
