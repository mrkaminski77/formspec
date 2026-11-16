"""Tests for built-in FormSpec formatters and dynamic custom formatter plugins."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from formspec.formatters import RenderContext, create_default_registry
from formspec.parser import parse_formspec

_SAMPLE_FSPEC = (
    '::: form user "User" @(admin:write, viewer:read, *:none)\n'
    '\t::: section "Details"\n'
    "\t\t[*Username](username:text) @(viewer:write)\n"
    "\t\t[*Department]{department_id -> department.key(id).display(name).order(name asc)}\n"
    "\t\t[Salary](salary:decimal) @(viewer:none)\n"
    "\t[[Submit]](submit) @(admin:write, *:none)\n"
)


class FormatterTests(unittest.TestCase):
    def test_html5_formatter_respects_role_permissions_and_options(self) -> None:
        doc = parse_formspec(_SAMPLE_FSPEC)
        registry = create_default_registry()
        html5 = registry.get("html5")

        opts = {"department_id": [{"id": 1, "name": "Engineering"}]}

        # admin: everything editable + Submit button visible
        admin_out = html5.render(doc, RenderContext(role="admin", options_by_field=opts))
        self.assertIn('name="username"', admin_out.content)
        self.assertIn('name="salary"', admin_out.content)
        self.assertIn("<option value=\"1\">Engineering</option>", admin_out.content)
        self.assertIn('type="submit"', admin_out.content)

        # viewer: username editable (override), department_id disabled, salary hidden, submit hidden
        viewer_out = html5.render(doc, RenderContext(role="viewer", options_by_field=opts))
        self.assertIn('name="username"', viewer_out.content)
        self.assertNotIn('name="salary"', viewer_out.content)
        self.assertIn('name="department_id" required disabled', viewer_out.content)
        self.assertNotIn('type="submit"', viewer_out.content)

        # anon: nothing visible
        anon_out = html5.render(doc, RenderContext(role="anon", options_by_field=opts))
        self.assertEqual(anon_out.content.strip(), "")

    def test_react_jsx_and_json_ast_formatters(self) -> None:
        doc = parse_formspec(_SAMPLE_FSPEC)
        registry = create_default_registry()

        jsx_out = registry.get("react-jsx").render(doc, RenderContext(role="admin"))
        self.assertIn("export function UserForm", jsx_out.content)
        self.assertIn("className=\"fs-form\"", jsx_out.content)

        json_out = registry.get("json-ast").render(doc, RenderContext(role="viewer"))
        parsed = json.loads(json_out.content)
        self.assertEqual(parsed["role"], "viewer")
        self.assertEqual(parsed["forms"][0]["block_id"], "user")

    def test_loads_custom_formatter_plugin_from_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plugin_path = Path(tmp) / "markdown_summary.py"
            plugin_path.write_text(
                "from formspec.formatters.base import FormFormatter, FormattedOutput, RenderContext\n"
                "from formspec.parser import FormDocument\n\n"
                "class MarkdownSummaryFormatter(FormFormatter):\n"
                "    id = 'md-summary'\n"
                "    name = 'Markdown Summary'\n"
                "    description = 'Custom test plugin'\n"
                "    output_kind = 'code'\n"
                "    def render(self, document: FormDocument, ctx: RenderContext) -> FormattedOutput:\n"
                "        title = document.primary_form.title if document.primary_form else 'None'\n"
                "        return FormattedOutput(self.id, 'code', f'# {title} ({ctx.role})')\n",
                encoding="utf-8",
            )
            registry = create_default_registry(plugins_dir=tmp)
            custom = registry.get("md-summary")
            out = custom.render(parse_formspec(_SAMPLE_FSPEC), RenderContext(role="admin"))
            self.assertEqual(out.content, "# User (admin)")

    def test_html5_renders_list_form_table_with_role_columns_and_open_link(self) -> None:
        list_src = (
            '::: form user_list(type: list) "User List" @(admin:write, viewer:read, *:none)\n'
            '\t[Search](q:text) | [[+ New]](create -> user_form) @(admin:write, *:none) | [3:1]\n'
            '\t::: list user(username ~= $q).order(id asc)\n'
            '\t\t[Username](username:text)\n'
            '\t\t[Department]{department_id -> department.key(id).display(name)}\n'
            '\t\t[Salary](salary:decimal) @(viewer:none)\n'
            '\t\t[[Open]](open -> user_form(id = $row.id))\n'
        )
        doc = parse_formspec(list_src)
        html5 = create_default_registry().get("html5")
        ctx = RenderContext(
            role="viewer",
            options_by_field={"department_id": [{"id": 1, "name": "Engineering"}]},
            list_rows_by_entity={
                "user": [{"id": 7, "username": "alice", "department_id": 1, "salary": 150000}]
            },
        )
        out = html5.render(doc, ctx)
        self.assertIn('<table class="fs-table">', out.content)
        self.assertIn("<td>alice</td>", out.content)
        self.assertIn("<td>Engineering</td>", out.content)
        self.assertNotIn("Salary", out.content)
        self.assertNotIn("150000", out.content)
        self.assertNotIn("+ New", out.content)
        self.assertIn('data-link-target="user_form"', out.content)
        self.assertIn('data-record-id="7"', out.content)

    def test_html5_hidden_param_injection_and_name_cell_link(self) -> None:
        detail_src = (
            '::: form user_detail(type: detail, params: [id, org_id]) "User" @(admin:write)\n'
            '\t[ID](id:hidden)\n'
            '\t[Name](name:text)\n'
            '\t[[Submit]](submit:submit -> server_action:user_upsert)\n'
        )
        html5 = create_default_registry().get("html5")
        detail_out = html5.render(
            parse_formspec(detail_src),
            RenderContext(role="admin", params={"id": 42, "org_id": 9}),
        )
        self.assertIn('<input type="hidden" id="f_id" name="id" value="42" />', detail_out.content)
        self.assertIn('<input type="hidden" name="org_id" value="9" data-injected-param="true" />', detail_out.content)
        self.assertIn('data-server-action="user_upsert"', detail_out.content)

        list_src = (
            '::: form user_list(type: list) "Users" @(viewer:read)\n'
            '\t[[Open Selected]](open -> client_action:open_user(id = $selected.id))\n'
            '\t::: list user.order(id asc)\n'
            '\t\t[ID](id:hidden)\n'
            '\t\t[Name](name:text -> user_detail(id = $row.id))\n'
            '\t\t[Department ID](department_id:hidden)\n'
            '\t\t[Department]{department_id -> department.key(id).display(name)}\n'
        )
        list_out = html5.render(
            parse_formspec(list_src),
            RenderContext(
                role="viewer",
                options_by_field={"department_id": [{"id": 3, "name": "Ops"}]},
                list_rows_by_entity={"user": [{"id": 7, "name": "Alice", "department_id": 3}]},
            ),
        )
        self.assertIn('data-client-action="open_user"', list_out.content)
        self.assertIn('class="fs-cell-link"', list_out.content)
        self.assertIn(">Alice</a>", list_out.content)
        self.assertIn("<td>Ops</td>", list_out.content)
        self.assertIn('<td class="fs-col-hidden" hidden><input type="hidden" name="department_id" value="3" /></td>', list_out.content)
        self.assertIn('data-row-id="7"', list_out.content)

    def test_html5_renders_inline_markdown_in_prose_and_headings(self) -> None:
        src = (
            '::: form home "Home" @(admin:write, *:read)\n'
            "\t# Welcome **Studio**\n"
            "\tAdd links (`[[Label]](open -> form_name)`) with *italic* hints.\n"
        )
        html5 = create_default_registry().get("html5")
        out = html5.render(parse_formspec(src), RenderContext(role="guest"))
        self.assertIn('<h1 class="fs-heading fs-h1">Welcome <strong>Studio</strong></h1>', out.content)
        self.assertIn('<code class="fs-inline-code">[[Label]](open -&gt; form_name)</code>', out.content)
        self.assertIn("<em>italic</em>", out.content)
        self.assertNotIn("`", out.content)

    def test_html5_pipe_columns_render_grid_tracks_and_hoist_hidden_inputs(self) -> None:
        src = (
            '::: form cols "Cols" @(admin:write)\n'
            "\t| [ID](id:hidden) | [First](first:text) | [Last](last:text) | [1:2:1]\n"
            "\t### Overview | [[Open]](open -> target)\n"
        )
        html5 = create_default_registry().get("html5")
        out = html5.render(parse_formspec(src), RenderContext(role="admin"))
        # Hidden input hoisted out of CSS grid tracks so visible columns stay 2fr 1fr
        self.assertIn('<input type="hidden" id="f_id" name="id" value="" />\n<div class="fs-row" style="grid-template-columns: 2fr 1fr;">', out.content)
        self.assertIn('<div class="fs-row" style="grid-template-columns: 1fr 1fr;">\n<h3 class="fs-heading fs-h3">Overview</h3>', out.content)


if __name__ == "__main__":
    unittest.main()
