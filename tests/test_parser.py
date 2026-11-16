"""Tests for the FormSpec (.fspec) parser, tab nesting, pipe ratios, and permissions."""

from __future__ import annotations

import unittest

from formspec.parser import (
    BlockNode,
    ButtonNode,
    CheckboxNode,
    ChoiceNode,
    HeadingNode,
    InputNode,
    ProseNode,
    RowNode,
    parse_formspec,
    resolve_effective_permission,
)


class ParserTests(unittest.TestCase):
    def test_parses_tab_nested_blocks_pipe_ratios_and_links(self) -> None:
        source = (
            '::: form employee "Employee" @(hr:write, manager:read, *:none)\n'
            '\t::: section "Profile"\n'
            '\t\t[*First Name](first_name:text) | [*Last Name](last_name:text) | [2:1]\n'
            '\t\t[*Department]{dept_id -> department.key(id).display(name).order(name asc)} @(manager:write)\n'
            '\t\t[x] Active (active)\n'
            '\t\t[[Add Dept]](add_dept -> department(mode: "modal"))\n'
            '\n'
            '\t[[Submit]](submit) @(hr:write, *:none)\n'
        )
        doc = parse_formspec(source)
        self.assertEqual(doc.diagnostics, [])
        form = doc.primary_form
        self.assertIsNotNone(form)
        assert form is not None
        self.assertEqual(form.block_id, "employee")
        self.assertEqual(form.title, "Employee")

        section = form.children[0]
        self.assertIsInstance(section, BlockNode)
        assert isinstance(section, BlockNode)
        self.assertEqual(section.title, "Profile")

        row = section.children[0]
        self.assertIsInstance(row, RowNode)
        assert isinstance(row, RowNode)
        self.assertEqual(row.ratios, [2.0, 1.0])
        self.assertIsInstance(row.elements[0], InputNode)
        self.assertIsInstance(row.elements[1], InputNode)

        choice = section.children[1]
        self.assertIsInstance(choice, ChoiceNode)
        assert isinstance(choice, ChoiceNode)
        self.assertEqual(choice.field_id, "dept_id")
        self.assertIsNotNone(choice.entity_query)
        assert choice.entity_query is not None
        self.assertEqual(choice.entity_query.target_table, "department")
        self.assertEqual(choice.entity_query.display_col, "name")

        cb = section.children[2]
        self.assertIsInstance(cb, CheckboxNode)
        assert isinstance(cb, CheckboxNode)
        self.assertTrue(cb.checked)

        link_btn = section.children[3]
        self.assertIsInstance(link_btn, ButtonNode)
        assert isinstance(link_btn, ButtonNode)
        self.assertEqual(link_btn.kind, "link")
        self.assertEqual(link_btn.link_target, "department")

        # Permission inheritance & override check
        self.assertEqual(
            resolve_effective_permission([form.modifiers.permissions, choice.modifiers.permissions], "manager"),
            "write",
        )
        self.assertEqual(
            resolve_effective_permission([form.modifiers.permissions], "manager"),
            "read",
        )
        self.assertEqual(
            resolve_effective_permission([form.modifiers.permissions], "anon"),
            "none",
        )

    def test_reports_space_indentation_and_ratio_mismatch(self) -> None:
        source = (
            '::: form bad "Bad"\n'
            '  [Name](name:text)\n'
            '\t[A](a:text) | [B](b:text) | [1:2:3]\n'
        )
        doc = parse_formspec(source)
        messages = [d.message for d in doc.diagnostics]
        self.assertTrue(any("tabs" in m for m in messages))
        self.assertTrue(any("Ratio count" in m for m in messages))

    def test_parses_list_form_and_list_block(self) -> None:
        source = (
            '::: form user_list(type: list) "Users" @(admin:write, viewer:read, *:none)\n'
            '\t[Search](q:text) | [[+ New]](create -> user_form) @(admin:write, *:none) | [3:1]\n'
            '\n'
            '\t::: list user(username ~= $q).order(id asc)\n'
            '\t\t[Username](username:text)\n'
            '\t\t[[Open]](open -> user_form(id = $row.id))\n'
        )
        doc = parse_formspec(source)
        self.assertEqual(doc.diagnostics, [])
        form = doc.primary_form
        assert form is not None
        self.assertEqual(form.form_type, "list")
        list_blk = form.children[1]
        self.assertIsInstance(list_blk, BlockNode)
        assert isinstance(list_blk, BlockNode)
        self.assertEqual(list_blk.kind, "list")
        self.assertIsNotNone(list_blk.entity_query)
        assert list_blk.entity_query is not None
        self.assertEqual(list_blk.entity_query.target_table, "user")
        self.assertEqual(list_blk.entity_query.order_col, "id")

    def test_parses_form_params_name_links_and_server_client_actions(self) -> None:
        source = (
            '::: form user_detail(type: detail, params: [id, department_id]) "User"\n'
            '\t[ID](id:hidden)\n'
            '\t[Name](name:text -> user_detail(id = $row.id))\n'
            '\t[[Inspect]](inspect:link -> client_action:open_user(id = $selected.id))\n'
            '\t[[Submit]](submit:submit -> server_action:user_upsert)\n'
        )
        doc = parse_formspec(source)
        self.assertEqual(doc.diagnostics, [])
        form = doc.primary_form
        assert form is not None
        self.assertEqual(form.params, ["id", "department_id"])

        name_col = form.children[1]
        self.assertIsInstance(name_col, InputNode)
        assert isinstance(name_col, InputNode)
        self.assertEqual(name_col.link_kind, "form")
        self.assertEqual(name_col.link_target, "user_detail")
        self.assertEqual(name_col.link_params, {"id": "$row.id"})

        ca_btn = form.children[2]
        self.assertIsInstance(ca_btn, ButtonNode)
        assert isinstance(ca_btn, ButtonNode)
        self.assertEqual(ca_btn.link_kind, "client_action")
        self.assertEqual(ca_btn.link_target, "open_user")
        self.assertEqual(ca_btn.link_params, {"id": "$selected.id"})

        sa_btn = form.children[3]
        self.assertIsInstance(sa_btn, ButtonNode)
        assert isinstance(sa_btn, ButtonNode)
        self.assertEqual(sa_btn.link_kind, "server_action")
        self.assertEqual(sa_btn.link_target, "user_upsert")

    def test_pipe_column_shortcuts_support_leading_trailing_headings_prose_and_backticks(self) -> None:
        source = (
            '::: form layout_test "Layout Test" @(admin:write)\n'
            "\t| [First](first:text) | [Last](last:text) |\n"
            "\t### Column One | ### Column Two | [1:2]\n"
            "\tManage employees | [[Open List]](open -> employee_list) | [2:1]\n"
            "\tExample syntax `[[A]](a) | [[B]](b)` stays prose.\n"
        )
        doc = parse_formspec(source)
        self.assertEqual(doc.diagnostics, [])
        form = doc.primary_form
        assert form is not None
        self.assertEqual(len(form.children), 4)

        r1, r2, r3, p4 = form.children
        self.assertIsInstance(r1, RowNode)
        assert isinstance(r1, RowNode)
        self.assertEqual([e.field_id for e in r1.elements], ["first", "last"])

        self.assertIsInstance(r2, RowNode)
        assert isinstance(r2, RowNode)
        self.assertEqual(r2.ratios, [1.0, 2.0])
        self.assertIsInstance(r2.elements[0], HeadingNode)
        self.assertIsInstance(r2.elements[1], HeadingNode)
        assert isinstance(r2.elements[0], HeadingNode)
        self.assertEqual(r2.elements[0].text, "Column One")

        self.assertIsInstance(r3, RowNode)
        assert isinstance(r3, RowNode)
        self.assertEqual(r3.ratios, [2.0, 1.0])
        self.assertIsInstance(r3.elements[0], ProseNode)
        self.assertIsInstance(r3.elements[1], ButtonNode)

        self.assertIsInstance(p4, ProseNode)


if __name__ == "__main__":
    unittest.main()
