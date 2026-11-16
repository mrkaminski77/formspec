"""React JSX code generator formatter (`react-jsx`)."""

from __future__ import annotations

import json

from formspec.db.base import AccessLevel
from formspec.formatters.base import (
    FormFormatter,
    FormattedOutput,
    RenderContext,
    node_has_visible_descendant,
)
from formspec.parser import (
    AstNode,
    BlockNode,
    ButtonNode,
    CheckboxNode,
    ChoiceNode,
    DividerNode,
    EmbedNode,
    FormDocument,
    HeadingNode,
    InputNode,
    ProseNode,
    RowNode,
    resolve_effective_permission,
)


def _pascal_case(name: str) -> str:
    parts = [p for p in name.replace("-", "_").split("_") if p]
    return "".join(p.capitalize() for p in parts) or "GeneratedForm"


class ReactJsxFormatter(FormFormatter):
    """Translates FormSpec AST into a React JSX component using `.fs-*` classes."""

    id = "react-jsx"
    name = "React JSX Component"
    description = "Generates a React JSX component referencing separate .fs-* CSS classes"
    output_kind = "code"

    def render(self, document: FormDocument, ctx: RenderContext) -> FormattedOutput:
        form = document.primary_form
        if form is None:
            return FormattedOutput(self.id, "code", "// Empty document")

        comp_name = f"{_pascal_case(form.block_id or 'form')}Form"
        body = self._render_block(form, [], ctx, indent=2)
        jsx = (
            "import React from 'react';\n\n"
            f"export function {comp_name}({{ values = {{}}, options = {{}}, onSubmit }}) {{\n"
            "  return (\n"
            f"{body}\n"
            "  );\n"
            "}\n"
        )
        return FormattedOutput(self.id, "code", jsx, metadata={"role": ctx.role})

    def _next_chain(
        self, node: AstNode, perm_chain: list[dict[str, AccessLevel] | None]
    ) -> list[dict[str, AccessLevel] | None]:
        mods = getattr(node, "modifiers", None)
        return [*perm_chain, mods.permissions] if mods and mods.permissions else perm_chain

    def _render_block(
        self,
        block: BlockNode,
        perm_chain: list[dict[str, AccessLevel] | None],
        ctx: RenderContext,
        indent: int,
    ) -> str:
        pad = " " * indent
        next_chain = self._next_chain(block, perm_chain)
        eff = resolve_effective_permission(next_chain, ctx.role)
        if eff == "none" and not node_has_visible_descendant(block, perm_chain, ctx.role):
            return ""

        if block.kind == "list":
            cols = [
                ch
                for ch in block.children
                if isinstance(ch, (InputNode, ChoiceNode, CheckboxNode))
                and resolve_effective_permission(self._next_chain(ch, next_chain), ctx.role) != "none"
            ]
            btns = [
                ch
                for ch in block.children
                if isinstance(ch, ButtonNode)
                and resolve_effective_permission(self._next_chain(ch, next_chain), ctx.role) != "none"
            ]
            ths = "".join(f"<th>{c.label}</th>" for c in cols) + ("<th>Actions</th>" if btns else "")
            tds = "".join(f"<td>{{row.{c.field_id}}}</td>" for c in cols)
            if btns:
                tds += "".join(
                    f'<td><button type="button" onClick={{() => onNavigate?.({json.dumps(b.link_target)}, row.id)}}>{b.label}</button></td>'
                    for b in btns
                )
            return (
                f'{pad}<table className="fs-table">\n'
                f"{pad}  <thead><tr>{ths}</tr></thead>\n"
                f"{pad}  <tbody>{{(rows || []).map(row => <tr key={{row.id}}>{tds}</tr>)}}</tbody>\n"
                f"{pad}</table>"
            )

        children_jsx = [
            rendered
            for ch in block.children
            if (rendered := self._render_node(ch, next_chain, ctx, indent + 2))
        ]
        inner = "\n".join(children_jsx)
        if block.kind == "form":
            title_jsx = (
                f'{pad}  <h1 className="fs-form-title">{block.title}</h1>\n'
                if block.title
                else ""
            )
            return f'{pad}<form className="fs-form" onSubmit={{onSubmit}}>\n{title_jsx}{inner}\n{pad}</form>'
        if block.kind == "section":
            legend = f'{pad}  <legend className="fs-section-title">{block.title}</legend>\n' if block.title else ""
            return f'{pad}<fieldset className="fs-section">\n{legend}{inner}\n{pad}</fieldset>'
        return f'{pad}<div className="fs-{block.kind}">\n{inner}\n{pad}</div>'

    def _render_node(
        self,
        node: AstNode,
        perm_chain: list[dict[str, AccessLevel] | None],
        ctx: RenderContext,
        indent: int,
    ) -> str:
        pad = " " * indent
        if isinstance(node, BlockNode):
            return self._render_block(node, perm_chain, ctx, indent)
        if isinstance(node, RowNode):
            ratios = node.ratios or [1.0] * len(node.elements)
            items = [
                self._render_node(el, perm_chain, ctx, indent + 2)
                for el in node.elements
            ]
            items = [i for i in items if i]
            if not items:
                return ""
            tpl = " ".join(f"{r:g}fr" for r in ratios[: len(items)])
            return (
                f'{pad}<div className="fs-row" style={{{{ gridTemplateColumns: {json.dumps(tpl)} }}}}>\n'
                + "\n".join(items)
                + f"\n{pad}</div>"
            )
        if isinstance(node, HeadingNode):
            return f'{pad}<h{node.level} className="fs-heading">{node.text}</h{node.level}>'
        if isinstance(node, ProseNode):
            return f'{pad}<p className="fs-prose">{node.text}</p>'
        if isinstance(node, DividerNode):
            return f'{pad}<hr className="fs-divider" />'
        if isinstance(node, EmbedNode):
            return f'{pad}<EmbeddedForm target={json.dumps(node.target)} />'

        eff = resolve_effective_permission(self._next_chain(node, perm_chain), ctx.role)
        if eff == "none":
            return ""
        disabled_prop = " disabled" if eff == "read" else ""

        if isinstance(node, InputNode):
            return (
                f'{pad}<div className="fs-field">\n'
                f'{pad}  <label className="fs-label">{node.label}{" *" if node.required else ""}</label>\n'
                f'{pad}  <input className="fs-input" name={json.dumps(node.field_id)} '
                f'type={json.dumps(node.field_type)} defaultValue={{values.{node.field_id}}}{disabled_prop} />\n'
                f"{pad}</div>"
            )
        if isinstance(node, ChoiceNode):
            return (
                f'{pad}<div className="fs-field">\n'
                f'{pad}  <label className="fs-label">{node.label}{" *" if node.required else ""}</label>\n'
                f'{pad}  <select className="fs-select" name={json.dumps(node.field_id)}{disabled_prop}>\n'
                f'{pad}    {{(options.{node.field_id} || []).map(o => <option key={{o.id}} value={{o.id}}>{{o.name}}</option>)}}\n'
                f"{pad}  </select>\n"
                f"{pad}</div>"
            )
        if isinstance(node, CheckboxNode):
            return (
                f'{pad}<label className="fs-checkbox-label">\n'
                f'{pad}  <input type="checkbox" name={json.dumps(node.field_id)} '
                f'defaultChecked={{{str(node.checked).lower()}}}{disabled_prop} /> {node.label}\n'
                f"{pad}</label>"
            )
        if isinstance(node, ButtonNode):
            return (
                f'{pad}<button className="fs-btn" type={json.dumps(node.kind)} '
                f'data-action={json.dumps(node.action_id)}>{node.label}</button>'
            )
        return ""
