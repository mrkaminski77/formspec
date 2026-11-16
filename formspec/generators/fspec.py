"""FormSpec (.fspec) file generator and RBAC permission synchronizer."""

from __future__ import annotations

from formspec.db.base import ACCESS_ORDER, AccessLevel, ColumnSchema, EntitySchema
from formspec.introspect import humanize_identifier
from formspec.parser import (
    AstNode,
    BlockNode,
    ButtonNode,
    CheckboxNode,
    ChoiceNode,
    InputNode,
    RowNode,
    _find_matching_delim,
    _split_top_level,
    parse_formspec,
)


def _format_perm_sigil(entries: list[tuple[str, AccessLevel]], include_wildcard_none: bool = True) -> str:
    """Format `@(role:perm, ...)` sigil deterministically."""
    ordered = sorted(
        entries,
        key=lambda item: (-ACCESS_ORDER[item[1]], item[0]),
    )
    parts = [f"{role}:{perm}" for role, perm in ordered]
    if include_wildcard_none:
        parts.append("*:none")
    return f"@({', '.join(parts)})"


def compute_form_perm_sigil(schema: EntitySchema) -> str:
    """Compute the form-level `@(...)` sigil from `schema.rbac`."""
    entity_entries = [
        (role, perm)
        for role, perm in schema.rbac.entity_perms.items()
        if perm != "none"
    ]
    return _format_perm_sigil(entity_entries, include_wildcard_none=True)


def compute_field_override_sigil(col_name: str, schema: EntitySchema) -> str | None:
    """Compute attribute-level `@(...)` overrides differing from form-level permissions."""
    all_roles = sorted(
        set(schema.rbac.roles)
        | set(schema.rbac.entity_perms)
        | set(schema.rbac.attribute_perms.get(col_name, {}))
    )
    overrides: list[tuple[str, AccessLevel]] = []
    for role in all_roles:
        form_perm = schema.rbac.entity_perms.get(role, "none")
        eff_perm = schema.rbac.effective_role_perm(role, col_name)
        if eff_perm != form_perm:
            overrides.append((role, eff_perm))
    if not overrides:
        return None
    return _format_perm_sigil(overrides, include_wildcard_none=False)


def compute_submit_perm_sigil(schema: EntitySchema) -> str:
    """Compute the `[[Submit]](submit)` `@(...)` sigil for write-capable roles."""
    submit_entries: list[tuple[str, AccessLevel]] = [
        (role, "write") for role in schema.rbac.write_capable_roles()
    ]
    return _format_perm_sigil(submit_entries, include_wildcard_none=True)


def compute_open_link_override_sigil(schema: EntitySchema) -> str | None:
    """Grant `read` on `[[Open]]` to roles that only have attribute-level access."""
    overrides: list[tuple[str, AccessLevel]] = []
    for role in sorted(schema.rbac.roles):
        if schema.rbac.entity_perms.get(role, "none") != "none":
            continue
        has_attr_read = any(
            schema.rbac.effective_role_perm(role, c.name) != "none"
            for c in schema.columns
        )
        if has_attr_read:
            overrides.append((role, "read"))
    if not overrides:
        return None
    return _format_perm_sigil(overrides, include_wildcard_none=False)


def _format_default(col: ColumnSchema) -> str | None:
    """Convert a SQL column default expression into a FormSpec literal/token."""
    if col.default is None:
        return None
    raw = col.default.strip()
    upper = raw.upper()

    if upper in ("CURRENT_TIMESTAMP", "NOW()", "DATETIME('NOW')"):
        return "$now"
    if upper in ("CURRENT_DATE", "DATE('NOW')"):
        return "$today"

    if col.fspec_type == "boolean":
        if upper in ("1", "TRUE", "'TRUE'", "'1'"):
            return "true"
        if upper in ("0", "FALSE", "'FALSE'", "'0'"):
            return "false"

    if (raw.startswith("'") and raw.endswith("'")) or (
        raw.startswith('"') and raw.endswith('"')
    ):
        inner = raw[1:-1].replace('"', '\\"')
        return f'"{inner}"'

    if col.fspec_type in ("int", "decimal"):
        return raw

    return f'"{raw}"'


def _format_field(
    col: ColumnSchema,
    schema: EntitySchema,
    *,
    mark_required: bool = True,
) -> str:
    """Render a single ColumnSchema into a FormSpec control line."""
    label = humanize_identifier(col.name)
    default_expr = _format_default(col)

    override_sigil = compute_field_override_sigil(col.name, schema)
    perm_suffix = f" {override_sigil}" if override_sigil else ""

    if col.fspec_type == "boolean" and col.entity_link is None:
        checked = "x" if default_expr == "true" else " "
        req = "*" if (mark_required and not col.nullable and default_expr is None) else ""
        return f"[{checked}] {req}{label} ({col.name}){perm_suffix}"

    req = "*" if (mark_required and not col.nullable) else ""
    if col.entity_link is not None:
        link = col.entity_link
        default_part = f" = {default_expr}" if (mark_required and default_expr is not None) else ""
        query = (
            f"{link.target_table}.key({link.key_col})"
            f".display({link.display_col})"
            f".order({link.display_col} asc)"
        )
        return f"[{req}{label}]{{{col.name}{default_part} -> {query}}}{perm_suffix}"

    default_part = f" = {default_expr}" if (mark_required and default_expr is not None) else ""
    return f"[{req}{label}]({col.name}:{col.fspec_type}{default_part}){perm_suffix}"


def _should_skip_column(col: ColumnSchema, include_pk: bool) -> bool:
    """Skip auto-generated PK `id` unless explicitly requested."""
    if col.is_pk and not include_pk:
        return True
    return False


def _pick_primary_name_column(schema: EntitySchema) -> str | None:
    """Select the primary display/name column for linking list rows to detail forms."""
    non_pk = [c for c in schema.columns if not c.is_pk and c.entity_link is None]
    by_name = {c.name: c for c in non_pk}
    for candidate in ("name", "full_name", "username", "title", "label"):
        if candidate in by_name:
            return candidate
    for c in non_pk:
        if c.fspec_type in ("text", "textarea", "email"):
            return c.name
    return None


def _generate_list_fspec(
    schema: EntitySchema,
    include_pk: bool = False,
    detail_form_name: str | None = None,
) -> str:
    """Generate a `type: list` FormSpec document with hidden IDs, resolved FKs, and name link."""
    form_perm_sigil = compute_form_perm_sigil(schema)
    write_sigil = compute_submit_perm_sigil(schema)
    target_detail = detail_form_name or f"{schema.entity_slug}_form"
    primary_name_col = _pick_primary_name_column(schema)

    filter_elems: list[str] = ['[Search](q:text) placeholder("Filter...")']
    filter_ratios: list[str] = ["2"]
    predicates: list[str] = []

    if primary_name_col:
        predicates.append(f"{primary_name_col} ~= $q")

    for col in schema.columns:
        if col.entity_link is not None:
            link = col.entity_link
            label = humanize_identifier(col.name)
            query = (
                f"{link.target_table}.key({link.key_col})"
                f".display({link.display_col})"
                f".order({link.display_col} asc)"
            )
            filter_elems.append(f"[{label}]{{{col.name} -> {query}}}")
            filter_ratios.append("2")
            predicates.append(f"{col.name} == ${col.name}")

    filter_elems.append(
        f'[[+ New]](create -> {target_detail}(mode: "modal")) {write_sigil}'
    )
    filter_ratios.append("1")

    pred_part = f"({', '.join(predicates)})" if predicates else ""
    list_header = f"\t::: list {schema.entity_slug}{pred_part}.order({schema.pk_column} asc)"

    lines: list[str] = [
        f'::: form {schema.entity_slug}_list(type: list) "{schema.display_name} List" {form_perm_sigil}',
        f"\t{' | '.join(filter_elems)} | [{':'.join(filter_ratios)}]",
        "",
        list_header,
        f"\t\t[ID]({schema.pk_column}:hidden)",
    ]

    for col in schema.columns:
        if _should_skip_column(col, include_pk):
            continue
        override_sigil = compute_field_override_sigil(col.name, schema)
        perm_suffix = f" {override_sigil}" if override_sigil else ""
        if col.name == primary_name_col:
            label = humanize_identifier(col.name)
            lines.append(
                f"\t\t[{label}]({col.name}:{col.fspec_type} -> {target_detail}(id = $row.{schema.pk_column})){perm_suffix}"
            )
        elif col.entity_link is not None:
            label = humanize_identifier(col.name)
            lines.append(f"\t\t[{label} ID]({col.name}:hidden){perm_suffix}")
            lines.append(f"\t\t{_format_field(col, schema, mark_required=False)}")
        else:
            lines.append(f"\t\t{_format_field(col, schema, mark_required=False)}")

    return "\n".join(lines) + "\n"


def generate_fspec(
    schema: EntitySchema,
    include_pk: bool = False,
    layout: str = "single",
    form_type: str = "detail",
    detail_form_name: str | None = None,
    client_action_name: str | None = None,
) -> str:
    """Generate a FormSpec (.fspec) document from an introspected EntitySchema."""
    if form_type == "list":
        return _generate_list_fspec(
            schema, include_pk=include_pk, detail_form_name=detail_form_name
        )

    form_perm_sigil = compute_form_perm_sigil(schema)

    lines: list[str] = [
        f'::: form {schema.entity_slug} "{schema.display_name}" {form_perm_sigil}',
        '\t::: section "Details"',
        f"\t\t[ID]({schema.pk_column}:hidden)",
    ]

    fields = [
        _format_field(col, schema)
        for col in schema.columns
        if not _should_skip_column(col, include_pk)
    ]

    if layout == "two-col" and len(fields) > 1:
        for idx in range(0, len(fields), 2):
            pair = fields[idx : idx + 2]
            if len(pair) == 2:
                lines.append(f"\t\t{pair[0]} | {pair[1]} | [1:1]")
            else:
                lines.append(f"\t\t{pair[0]}")
    else:
        for field_line in fields:
            lines.append(f"\t\t{field_line}")

    submit_sigil = compute_submit_perm_sigil(schema)
    submit_client_action = client_action_name or f"{schema.entity_slug}_form_submit"
    lines.append("")
    lines.append(
        f"\t[[Submit]](submit:submit -> client_action:{submit_client_action}) {submit_sigil}"
    )
    return "\n".join(lines) + "\n"


def _strip_perm_sigils(segment: str) -> str:
    """Remove any `@(...)` permission sigils from `segment` while preserving other modifiers."""
    out: list[str] = []
    idx = 0
    n = len(segment)
    quote: str | None = None
    escaped = False

    while idx < n:
        ch = segment[idx]
        if escaped:
            out.append(ch)
            escaped = False
            idx += 1
            continue
        if quote is not None:
            out.append(ch)
            if ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
            idx += 1
            continue
        if ch in ('"', "'"):
            quote = ch
            out.append(ch)
            idx += 1
            continue
        if segment[idx : idx + 2] == "@(":
            end = _find_matching_delim(segment, idx + 1, "(", ")")
            if end != -1:
                idx = end + 1
                continue
        out.append(ch)
        idx += 1

    cleaned = " ".join("".join(out).split())
    return cleaned


def _desired_sigil_for_node(
    node: AstNode,
    schema: EntitySchema,
    col_names: set[str],
) -> tuple[bool, str | None]:
    """Return `(should_sync, new_sigil)` for an AST element."""
    if isinstance(node, BlockNode) and node.kind == "form":
        return True, compute_form_perm_sigil(schema)
    if isinstance(node, ButtonNode):
        if node.kind == "submit" or (node.kind == "link" and node.action_id == "create"):
            return True, compute_submit_perm_sigil(schema)
        if node.kind == "link" and node.action_id == "open":
            return True, compute_open_link_override_sigil(schema)
    if isinstance(node, (InputNode, ChoiceNode, CheckboxNode)) and node.field_id in col_names:
        return True, compute_field_override_sigil(node.field_id, schema)
    return False, None


def _index_sync_targets(
    node: AstNode,
    targets: dict[int, AstNode],
) -> None:
    targets[node.line] = node
    if isinstance(node, BlockNode):
        for child in node.children:
            _index_sync_targets(child, targets)


def sync_fspec_permissions(fspec_source: str, schema: EntitySchema) -> str:
    """Update `@(...)` permission sigils in `fspec_source` to match `schema.rbac`.

    Preserves form layout, pipe ratios, custom labels, defaults, and non-permission modifiers.
    """
    doc = parse_formspec(fspec_source)
    col_names = {c.name for c in schema.columns}
    targets: dict[int, AstNode] = {}
    for form in doc.forms:
        _index_sync_targets(form, targets)

    lines = fspec_source.splitlines()
    updated_lines: list[str] = []

    for idx, raw_line in enumerate(lines):
        line_no = idx + 1
        node = targets.get(line_no)
        if node is None:
            updated_lines.append(raw_line)
            continue

        tabs = 0
        while tabs < len(raw_line) and raw_line[tabs] == "\t":
            tabs += 1
        prefix = raw_line[:tabs]
        content = raw_line[tabs:]

        if isinstance(node, RowNode):
            parts = _split_top_level(content, "|")
            new_parts: list[str] = []
            for seg_idx, seg in enumerate(parts):
                if seg_idx < len(node.elements):
                    should_sync, new_sigil = _desired_sigil_for_node(
                        node.elements[seg_idx], schema, col_names
                    )
                    if should_sync:
                        base = _strip_perm_sigils(seg.strip())
                        new_parts.append(f"{base} {new_sigil}" if new_sigil else base)
                        continue
                new_parts.append(seg.strip())
            updated_lines.append(prefix + " | ".join(new_parts))
            continue

        should_sync, new_sigil = _desired_sigil_for_node(node, schema, col_names)
        if should_sync:
            base = _strip_perm_sigils(content.strip())
            updated_lines.append(prefix + (f"{base} {new_sigil}" if new_sigil else base))
        else:
            updated_lines.append(raw_line)

    result = "\n".join(updated_lines)
    if fspec_source.endswith("\n"):
        result += "\n"
    return result
