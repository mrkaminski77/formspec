"""Semantic HTML5 FormSpec formatter (styled via external CSS themes)."""

from __future__ import annotations

import html
import json
import re

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

_INPUT_TYPE_MAP = {
    "text": "text",
    "email": "email",
    "url": "url",
    "tel": "tel",
    "password": "password",
    "int": "number",
    "integer": "number",
    "number": "number",
    "decimal": "number",
    "currency": "number",
    "date": "date",
    "time": "time",
    "datetime": "datetime-local",
    "color": "color",
    "hidden": "hidden",
    "file": "file",
    "files": "file",
}


def _collect_field_ids(node: AstNode) -> set[str]:
    ids: set[str] = set()
    if isinstance(node, (InputNode, ChoiceNode, CheckboxNode)):
        ids.add(node.field_id)
    elif isinstance(node, BlockNode):
        for ch in node.children:
            ids.update(_collect_field_ids(ch))
    elif isinstance(node, RowNode):
        for el in node.elements:
            ids.update(_collect_field_ids(el))
    return ids


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _strip_literal(val: str | None) -> str:
    if val is None:
        return ""
    s = val.strip()
    if len(s) >= 2 and ((s[0] == '"' and s[-1] == '"') or (s[0] == "'" and s[-1] == "'")):
        return s[1:-1]
    return s


_INLINE_MD_RE = re.compile(r"(`[^`]+`|\*\*[^*\n]+\*\*|\*[^*\n]+\*)")


def _render_inline_markdown(text: str) -> str:
    """Render inline Markdown (`code`, **bold**, *italic*) safely as HTML."""
    parts: list[str] = []
    pos = 0
    for match in _INLINE_MD_RE.finditer(text):
        if match.start() > pos:
            parts.append(_esc(text[pos : match.start()]))
        tok = match.group(0)
        if tok.startswith("`") and tok.endswith("`"):
            parts.append(f'<code class="fs-inline-code">{_esc(tok[1:-1])}</code>')
        elif tok.startswith("**") and tok.endswith("**"):
            parts.append(f"<strong>{_esc(tok[2:-2])}</strong>")
        elif tok.startswith("*") and tok.endswith("*"):
            parts.append(f"<em>{_esc(tok[1:-1])}</em>")
        else:
            parts.append(_esc(tok))
        pos = match.end()
    if pos < len(text):
        parts.append(_esc(text[pos:]))
    return "".join(parts)


class Html5Formatter(FormFormatter):
    """Renders FormSpec AST to semantic HTML5 with `.fs-*` CSS class hooks."""

    id = "html5"
    name = "HTML5 Semantic"
    description = "Semantic HTML5 form markup styled by separate .css themes"
    output_kind = "html"

    def render(self, document: FormDocument, ctx: RenderContext) -> FormattedOutput:
        chunks = [
            self._render_block(form, [], ctx)
            for form in document.forms
        ]
        return FormattedOutput(
            formatter_id=self.id,
            output_kind="html",
            content="\n".join(c for c in chunks if c),
            metadata={"role": ctx.role},
        )

    def _next_chain(
        self,
        node: AstNode,
        perm_chain: list[dict[str, AccessLevel] | None],
    ) -> list[dict[str, AccessLevel] | None]:
        mods = getattr(node, "modifiers", None)
        if mods and mods.permissions:
            return [*perm_chain, mods.permissions]
        return perm_chain

    def _wrapper_attrs(self, node: AstNode, extra_class: str = "") -> str:
        mods = getattr(node, "modifiers", None)
        classes = ["fs-field"]
        if extra_class:
            classes.append(extra_class)
        attrs = [f'class="{" ".join(classes)}"']
        if mods:
            if mods.span:
                attrs.append(f'style="grid-column: span {mods.span};"')
            if mods.when_expr:
                attrs.append(f'data-when="{_esc(mods.when_expr)}"')
        return " ".join(attrs)

    def _render_block(
        self,
        block: BlockNode,
        perm_chain: list[dict[str, AccessLevel] | None],
        ctx: RenderContext,
    ) -> str:
        next_chain = self._next_chain(block, perm_chain)
        eff = resolve_effective_permission(next_chain, ctx.role)
        if eff == "none" and not node_has_visible_descendant(block, perm_chain, ctx.role):
            return ""

        if block.kind == "list":
            return self._render_list(block, next_chain, ctx)

        inner = "\n".join(
            part
            for ch in block.children
            if (part := self._render_node(ch, next_chain, ctx))
        )
        if block.kind == "form":
            title_html = (
                f'<header class="fs-form-header"><h1 class="fs-form-title">{_esc(block.title)}</h1></header>'
                if block.title
                else ""
            )
            form_id = _esc(block.block_id or "form")
            declared_ids = _collect_field_ids(block)
            injected_html = "\n".join(
                f'<input type="hidden" name="{_esc(k)}" value="{_esc("" if v is None else v)}" data-injected-param="true" />'
                for k, v in ctx.params.items()
                if k not in declared_ids
            )
            if not inner and not title_html:
                inner = '<p class="fs-empty">No elements visible for this role.</p>'
            return (
                f'<form class="fs-form" data-form-id="{form_id}" data-form-type="{_esc(block.form_type)}" '
                f'data-role="{_esc(ctx.role)}" onsubmit="return false;">\n'
                f"{title_html}\n{injected_html}\n{inner}\n</form>"
            )

        if block.kind == "section":
            legend = f'<legend class="fs-section-title">{_esc(block.title)}</legend>' if block.title else ""
            return f'<fieldset class="fs-section">\n{legend}\n{inner}\n</fieldset>'

        if block.kind == "row":
            if block.ratios:
                cols_tpl = " ".join(f"{r:g}fr" for r in block.ratios)
            else:
                count = max(1, len(block.children))
                cols_tpl = " ".join(["1fr"] * count)
            return f'<div class="fs-row" style="grid-template-columns: {cols_tpl};">\n{inner}\n</div>'

        if block.kind == "grid":
            cols = "2"
            if block.args_raw:
                m = re.search(r"cols\s*:\s*(\d+)", block.args_raw)
                if m:
                    cols = m.group(1)
            return f'<div class="fs-grid" style="grid-template-columns: repeat({cols}, minmax(0, 1fr));">\n{inner}\n</div>'

        if block.kind == "repeat":
            title = _esc(block.title or block.block_id or "Item")
            return (
                f'<div class="fs-repeat" data-repeat="{_esc(block.block_id or "")}">\n'
                f'<div class="fs-repeat-header"><span>{title}</span>'
                f'<button type="button" class="fs-btn fs-btn-secondary">+ Add</button></div>\n'
                f'<div class="fs-repeat-item">{inner}</div>\n</div>'
            )

        cls = f"fs-{block.kind}"
        return f'<div class="{_esc(cls)}">\n{inner}\n</div>'

    def _render_node(
        self,
        node: AstNode,
        perm_chain: list[dict[str, AccessLevel] | None],
        ctx: RenderContext,
    ) -> str:
        if isinstance(node, BlockNode):
            return self._render_block(node, perm_chain, ctx)
        if isinstance(node, RowNode):
            return self._render_row(node, perm_chain, ctx)
        if isinstance(node, HeadingNode):
            lvl = min(6, max(1, node.level))
            return f'<h{lvl} class="fs-heading fs-h{lvl}">{_render_inline_markdown(node.text)}</h{lvl}>'
        if isinstance(node, ProseNode):
            return f'<p class="fs-prose">{_render_inline_markdown(node.text)}</p>'
        if isinstance(node, DividerNode):
            return '<hr class="fs-divider" />'
        if isinstance(node, EmbedNode):
            return self._render_embed(node, perm_chain, ctx)
        if isinstance(node, InputNode):
            return self._render_input(node, perm_chain, ctx)
        if isinstance(node, ChoiceNode):
            return self._render_choice(node, perm_chain, ctx)
        if isinstance(node, CheckboxNode):
            return self._render_checkbox(node, perm_chain, ctx)
        if isinstance(node, ButtonNode):
            return self._render_button(node, perm_chain, ctx)
        return ""

    def _render_row(
        self,
        row: RowNode,
        perm_chain: list[dict[str, AccessLevel] | None],
        ctx: RenderContext,
    ) -> str:
        hidden_items: list[str] = []
        rendered_items: list[tuple[str, float]] = []
        ratios = row.ratios or [1.0] * len(row.elements)
        for idx, elem in enumerate(row.elements):
            markup = self._render_node(elem, perm_chain, ctx)
            if not markup:
                continue
            if isinstance(elem, InputNode) and elem.field_type == "hidden":
                hidden_items.append(markup)
                continue
            weight = ratios[idx] if idx < len(ratios) else 1.0
            rendered_items.append((markup, weight))
        if not rendered_items and not hidden_items:
            return ""
        hidden_prefix = ("\n".join(hidden_items) + "\n") if hidden_items else ""
        if not rendered_items:
            return hidden_prefix.rstrip()
        tpl = " ".join(f"{w:g}fr" for _, w in rendered_items)
        body = "\n".join(item for item, _ in rendered_items)
        return f'{hidden_prefix}<div class="fs-row" style="grid-template-columns: {tpl};">\n{body}\n</div>'

    def _render_embed(
        self,
        node: EmbedNode,
        perm_chain: list[dict[str, AccessLevel] | None],
        ctx: RenderContext,
    ) -> str:
        eff = resolve_effective_permission(self._next_chain(node, perm_chain), ctx.role)
        if eff == "none":
            return ""
        return (
            f'<div class="fs-embed" data-target="{_esc(node.target)}">'
            f'Embedded Form: <strong>{_esc(node.target)}</strong>'
            f'{f" ({_esc(node.args_raw)})" if node.args_raw else ""}'
            f"</div>"
        )

    def _render_input(
        self,
        node: InputNode,
        perm_chain: list[dict[str, AccessLevel] | None],
        ctx: RenderContext,
    ) -> str:
        eff = resolve_effective_permission(self._next_chain(node, perm_chain), ctx.role)
        if eff == "none" and not (node.field_type == "hidden" and node.field_id == "id"):
            return ""

        raw_val = ctx.params.get(
            node.field_id,
            ctx.values.get(node.field_id, _strip_literal(node.default)),
        )
        val = _esc("" if raw_val is None else raw_val)

        if node.field_type == "hidden":
            return (
                f'<input type="hidden" id="f_{_esc(node.field_id)}" '
                f'name="{_esc(node.field_id)}" value="{val}" />'
            )

        readonly_attr = " disabled readonly" if (eff == "read" or node.modifiers.readonly or node.modifiers.disabled) else ""
        ro_class = "is-readonly" if readonly_attr else ""
        req_mark = '<span class="fs-required" aria-hidden="true">*</span>' if node.required else ""
        req_attr = " required" if node.required else ""
        ph = f' placeholder="{_esc(node.modifiers.placeholder)}"' if node.modifiers.placeholder else ""
        help_html = (
            f'<small class="fs-help">{_esc(node.modifiers.help_text)}</small>'
            if node.modifiers.help_text
            else ""
        )

        if node.field_type == "textarea":
            control = (
                f'<textarea class="fs-input fs-textarea" id="f_{_esc(node.field_id)}" '
                f'name="{_esc(node.field_id)}"{ph}{req_attr}{readonly_attr}>{val}</textarea>'
            )
        else:
            html_type = _INPUT_TYPE_MAP.get(node.field_type, "text")
            step = ' step="any"' if node.field_type in ("decimal", "number", "currency") else ""
            val_attr = "" if html_type == "file" else f' value="{val}"'
            multi = " multiple" if node.field_type == "files" else ""
            control = (
                f'<input class="fs-input" type="{html_type}" id="f_{_esc(node.field_id)}" '
                f'name="{_esc(node.field_id)}"{val_attr}{ph}{step}{multi}{req_attr}{readonly_attr} />'
            )

        return (
            f'<div {self._wrapper_attrs(node, ro_class)}>\n'
            f'  <label class="fs-label" for="f_{_esc(node.field_id)}">{_esc(node.label)}{req_mark}</label>\n'
            f"  {control}\n"
            f"  {help_html}\n"
            f"</div>"
        )

    def _render_choice(
        self,
        node: ChoiceNode,
        perm_chain: list[dict[str, AccessLevel] | None],
        ctx: RenderContext,
    ) -> str:
        eff = resolve_effective_permission(self._next_chain(node, perm_chain), ctx.role)
        if eff == "none":
            return ""

        disabled_attr = " disabled" if (eff == "read" or node.modifiers.readonly or node.modifiers.disabled) else ""
        ro_class = "is-readonly" if disabled_attr else ""
        req_mark = '<span class="fs-required" aria-hidden="true">*</span>' if node.required else ""
        req_attr = " required" if node.required else ""
        selected_val = str(ctx.values.get(node.field_id, _strip_literal(node.default)))

        if node.entity_query is not None:
            raw_opts = ctx.options_by_field.get(node.field_id, [])
            opts = [(str(o["id"]), str(o["name"])) for o in raw_opts]
        else:
            opts = [(o.value, o.label) for o in node.options]

        if node.presentation == "radio" and not node.multiple:
            items = []
            for val, lbl in opts:
                chk = " checked" if val == selected_val else ""
                items.append(
                    f'<label class="fs-choice-item"><input type="radio" name="{_esc(node.field_id)}" '
                    f'value="{_esc(val)}"{chk}{disabled_attr} /> {_esc(lbl)}</label>'
                )
            control = f'<div class="fs-choice-group">{"".join(items)}</div>'
        else:
            multi = " multiple" if node.multiple else ""
            opt_tags = ['<option value="">-- Select --</option>'] if not node.multiple else []
            for val, lbl in opts:
                sel = " selected" if val == selected_val else ""
                opt_tags.append(f'<option value="{_esc(val)}"{sel}>{_esc(lbl)}</option>')
            control = (
                f'<select class="fs-select" id="f_{_esc(node.field_id)}" '
                f'name="{_esc(node.field_id)}"{multi}{req_attr}{disabled_attr}>\n'
                + "\n".join(opt_tags)
                + "\n</select>"
            )

        query_badge = (
            f'<small class="fs-help">Linked: {_esc(node.entity_query.target_table or node.entity_query.raw)}</small>'
            if node.entity_query
            else ""
        )
        return (
            f'<div {self._wrapper_attrs(node, ro_class)}>\n'
            f'  <label class="fs-label" for="f_{_esc(node.field_id)}">{_esc(node.label)}{req_mark}</label>\n'
            f"  {control}\n"
            f"  {query_badge}\n"
            f"</div>"
        )

    def _render_checkbox(
        self,
        node: CheckboxNode,
        perm_chain: list[dict[str, AccessLevel] | None],
        ctx: RenderContext,
    ) -> str:
        eff = resolve_effective_permission(self._next_chain(node, perm_chain), ctx.role)
        if eff == "none":
            return ""
        disabled_attr = " disabled" if (eff == "read" or node.modifiers.readonly or node.modifiers.disabled) else ""
        ro_class = "is-readonly" if disabled_attr else ""
        checked_val = ctx.values.get(node.field_id, node.checked)
        chk_attr = " checked" if checked_val else ""
        req_mark = '<span class="fs-required" aria-hidden="true">*</span>' if node.required else ""
        return (
            f'<div {self._wrapper_attrs(node, f"fs-field-checkbox {ro_class}".strip())}>\n'
            f'  <label class="fs-checkbox-label">'
            f'<input class="fs-checkbox" type="checkbox" name="{_esc(node.field_id)}"{chk_attr}{disabled_attr} /> '
            f'<span>{_esc(node.label)}{req_mark}</span></label>\n'
            f"</div>"
        )

    def _render_button(
        self,
        node: ButtonNode,
        perm_chain: list[dict[str, AccessLevel] | None],
        ctx: RenderContext,
    ) -> str:
        eff = resolve_effective_permission(self._next_chain(node, perm_chain), ctx.role)
        # SPEC Section 6.3: Submit/action buttons visible and enabled when >= read
        if eff == "none":
            return ""
        btn_type = "submit" if node.kind == "submit" else ("reset" if node.kind == "reset" else "button")
        variant = "fs-btn-primary" if node.kind == "submit" else "fs-btn-secondary"
        extra_attrs: list[str] = []
        if node.link_target:
            if node.link_kind == "server_action":
                extra_attrs.append(f'data-server-action="{_esc(node.link_target)}"')
            elif node.link_kind == "client_action":
                extra_attrs.append(f'data-client-action="{_esc(node.link_target)}"')
            else:
                extra_attrs.append(f'data-link-target="{_esc(node.link_target)}"')
        if node.link_params:
            extra_attrs.append(f'data-link-params="{_esc(json.dumps(node.link_params))}"')
        extra_str = (" " + " ".join(extra_attrs)) if extra_attrs else ""
        return (
            f'<div {self._wrapper_attrs(node, "fs-field-button")}>\n'
            f'  <button type="{btn_type}" class="fs-btn {variant}" data-action="{_esc(node.action_id)}"{extra_str}>'
            f"{_esc(node.label)}</button>\n"
            f"</div>"
        )

    def _render_list(
        self,
        block: BlockNode,
        perm_chain: list[dict[str, AccessLevel] | None],
        ctx: RenderContext,
    ) -> str:
        entity_name = (
            (block.entity_query.target_table if block.entity_query else None)
            or block.block_id
            or ""
        )
        col_nodes: list[InputNode | ChoiceNode | CheckboxNode] = []
        row_buttons: list[ButtonNode] = []

        for child in block.children:
            child_eff = resolve_effective_permission(
                self._next_chain(child, perm_chain), ctx.role
            )
            is_hidden_id = isinstance(child, InputNode) and child.field_type == "hidden" and child.field_id == "id"
            if child_eff == "none" and not is_hidden_id:
                continue
            if isinstance(child, (InputNode, ChoiceNode, CheckboxNode)):
                col_nodes.append(child)
            elif isinstance(child, ButtonNode):
                row_buttons.append(child)

        if not col_nodes and not row_buttons:
            return ""

        option_lookup: dict[str, dict[str, str]] = {}
        for col in col_nodes:
            if isinstance(col, ChoiceNode):
                opts = ctx.options_by_field.get(col.field_id, [])
                option_lookup[col.field_id] = {
                    str(o["id"]): str(o["name"]) for o in opts
                }

        def _is_hidden_col(col: AstNode) -> bool:
            return isinstance(col, InputNode) and col.field_type == "hidden"

        visible_count = sum(1 for c in col_nodes if not _is_hidden_col(c))
        headers = "".join(
            f'<th class="fs-col-hidden" hidden>{_esc(col.label)}</th>'
            if _is_hidden_col(col)
            else f"<th>{_esc(col.label)}</th>"
            for col in col_nodes
        )
        if row_buttons:
            headers += "<th>Actions</th>"

        rows = ctx.list_rows_by_entity.get(entity_name, [])
        body_rows: list[str] = []
        for row in rows:
            cells: list[str] = []
            row_payload: dict[str, object] = {}
            rec_id = row.get("id", "")
            for col in col_nodes:
                raw_val = row.get(col.field_id)
                row_payload[col.field_id] = raw_val
                if _is_hidden_col(col):
                    cells.append(
                        f'<td class="fs-col-hidden" hidden>'
                        f'<input type="hidden" name="{_esc(col.field_id)}" value="{_esc("" if raw_val is None else raw_val)}" />'
                        f"</td>"
                    )
                elif isinstance(col, ChoiceNode) and raw_val is not None:
                    display = option_lookup.get(col.field_id, {}).get(
                        str(raw_val), str(raw_val)
                    )
                    cells.append(f"<td>{_esc(display)}</td>")
                elif isinstance(col, CheckboxNode):
                    cells.append(f"<td>{'Yes' if raw_val else 'No'}</td>")
                elif isinstance(col, InputNode) and col.link_target:
                    txt = _esc("" if raw_val is None else raw_val)
                    params_attr = (
                        f' data-link-params="{_esc(json.dumps(col.link_params))}"'
                        if col.link_params
                        else ""
                    )
                    cells.append(
                        f'<td><a href="#" class="fs-cell-link" '
                        f'data-link-kind="{_esc(col.link_kind or "form")}" '
                        f'data-link-target="{_esc(col.link_target)}"{params_attr} '
                        f'data-record-id="{_esc(rec_id)}">{txt}</a></td>'
                    )
                else:
                    cells.append(f"<td>{_esc('' if raw_val is None else raw_val)}</td>")

            if row_buttons:
                btns_html = " ".join(
                    f'<button type="button" class="fs-btn fs-btn-secondary fs-row-btn" '
                    f'data-action="{_esc(b.action_id)}" '
                    f'data-link-kind="{_esc(b.link_kind or "form")}" '
                    f'data-link-target="{_esc(b.link_target or "")}" '
                    f'data-link-params="{_esc(json.dumps(b.link_params))}" '
                    f'data-record-id="{_esc(rec_id)}">{_esc(b.label)}</button>'
                    for b in row_buttons
                )
                cells.append(f'<td class="fs-table-actions">{btns_html}</td>')

            body_rows.append(
                f'<tr class="fs-list-row" data-row-id="{_esc(rec_id)}" '
                f'data-row="{_esc(json.dumps(row_payload))}">{"".join(cells)}</tr>'
            )

        if not body_rows:
            colspan = visible_count + (1 if row_buttons else 0)
            body_rows.append(
                f'<tr><td colspan="{max(1, colspan)}" class="fs-empty">No records found.</td></tr>'
            )

        return (
            f'<div class="fs-list-wrap" data-entity="{_esc(entity_name)}">\n'
            f'  <table class="fs-table">\n'
            f"    <thead><tr>{headers}</tr></thead>\n"
            f"    <tbody>\n" + "\n".join(f"      {r}" for r in body_rows) + "\n    </tbody>\n"
            f"  </table>\n"
            f"</div>"
        )
