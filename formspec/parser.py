"""FormSpec (.fspec) parser, AST model, and RBAC permission evaluator."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Literal

from formspec.db.base import ACCESS_ORDER, AccessLevel

# ---------------------------------------------------------------------------
# AST & Diagnostics
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParseDiagnostic:
    """Syntax or semantic diagnostic emitted while parsing FormSpec source."""

    line: int
    column: int
    message: str
    severity: Literal["error", "warning"] = "error"


@dataclass(frozen=True)
class ChoiceOption:
    """Static choice option inside `{...}`."""

    value: str
    label: str


@dataclass(frozen=True)
class EntityQuerySpec:
    """Parsed `->` entity query summary for option loading and formatters."""

    raw: str
    target_table: str | None = None
    key_col: str = "id"
    display_col: str = "name"
    order_col: str | None = None


@dataclass(frozen=True)
class ElementModifiers:
    """Trailing modifiers attached to a block or control."""

    permissions: dict[str, AccessLevel] | None = None
    when_expr: str | None = None
    span: int | None = None
    help_text: str | None = None
    placeholder: str | None = None
    readonly: bool = False
    disabled: bool = False


@dataclass
class AstNode:
    """Base AST node."""

    line: int


@dataclass
class HeadingNode(AstNode):
    level: int
    text: str


@dataclass
class ProseNode(AstNode):
    text: str


@dataclass
class DividerNode(AstNode):
    pass


@dataclass
class EmbedNode(AstNode):
    target: str
    args_raw: str | None = None
    modifiers: ElementModifiers = field(default_factory=ElementModifiers)


@dataclass
class InputNode(AstNode):
    field_id: str
    label: str
    field_type: str = "text"
    required: bool = False
    default: str | None = None
    constraints_raw: str | None = None
    link_kind: Literal["form", "server_action", "client_action"] | None = None
    link_target: str | None = None
    link_args: str | None = None
    link_params: dict[str, str] = field(default_factory=dict)
    modifiers: ElementModifiers = field(default_factory=ElementModifiers)


@dataclass
class ChoiceNode(AstNode):
    field_id: str
    label: str
    multiple: bool = False
    required: bool = False
    default: str | None = None
    options: list[ChoiceOption] = field(default_factory=list)
    entity_query: EntityQuerySpec | None = None
    presentation: str | None = None
    modifiers: ElementModifiers = field(default_factory=ElementModifiers)


@dataclass
class CheckboxNode(AstNode):
    field_id: str
    label: str
    checked: bool = False
    required: bool = False
    default: str | None = None
    modifiers: ElementModifiers = field(default_factory=ElementModifiers)


@dataclass
class ButtonNode(AstNode):
    action_id: str
    label: str
    kind: Literal["submit", "reset", "action", "link"] = "submit"
    link_kind: Literal["form", "server_action", "client_action"] | None = None
    link_target: str | None = None
    link_args: str | None = None
    link_params: dict[str, str] = field(default_factory=dict)
    modifiers: ElementModifiers = field(default_factory=ElementModifiers)


@dataclass
class RowNode(AstNode):
    elements: list[AstNode] = field(default_factory=list)
    ratios: list[float] | None = None


@dataclass
class BlockNode(AstNode):
    kind: str
    block_id: str | None = None
    title: str | None = None
    args_raw: str | None = None
    ratios: list[float] | None = None
    form_type: Literal["detail", "list"] = "detail"
    params: list[str] = field(default_factory=list)
    entity_query: EntityQuerySpec | None = None
    modifiers: ElementModifiers = field(default_factory=ElementModifiers)
    children: list[AstNode] = field(default_factory=list)


@dataclass
class FormDocument:
    """Root parsed FormSpec document."""

    forms: list[BlockNode] = field(default_factory=list)
    diagnostics: list[ParseDiagnostic] = field(default_factory=list)

    @property
    def primary_form(self) -> BlockNode | None:
        return self.forms[0] if self.forms else None


# ---------------------------------------------------------------------------
# Permission Resolution (SPEC.md Section 6.4)
# ---------------------------------------------------------------------------


def resolve_effective_permission(
    perm_chain: list[dict[str, AccessLevel] | None],
    role: str,
) -> AccessLevel:
    """Resolve effective permission along an ancestor chain for a single role."""
    if role == "superuser":
        return "write"

    resolved: dict[str, AccessLevel] = {"*": "write"}
    has_any_sigil = False
    for sigil in perm_chain:
        if not sigil:
            continue
        has_any_sigil = True
        if "*" in sigil:
            # Explicit wildcard re-baselines unmentioned inherited roles
            resolved = {"*": sigil["*"]}
        for k, v in sigil.items():
            if k != "*":
                resolved[k] = v

    if not has_any_sigil:
        return "write"
    if role in resolved:
        return resolved[role]
    return resolved.get("*", "none")


def collect_declared_roles(node: BlockNode | AstNode) -> set[str]:
    """Collect all explicit role names mentioned in `@(...)` sigils within `node`."""
    roles: set[str] = set()
    mods = getattr(node, "modifiers", None)
    if mods and mods.permissions:
        for role in mods.permissions:
            if role != "*":
                roles.add(role)
    if isinstance(node, BlockNode):
        for child in node.children:
            roles.update(collect_declared_roles(child))
    elif isinstance(node, RowNode):
        for elem in node.elements:
            roles.update(collect_declared_roles(elem))
    return roles


# ---------------------------------------------------------------------------
# Token / Expression Scanning Helpers
# ---------------------------------------------------------------------------

_RATIO_RE = re.compile(r"^\[\s*(\d+(?:\.\d+)?(?:\s*:\s*\d+(?:\.\d+)?)+)\s*\]$")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")


def _split_top_level(text: str, sep: str) -> list[str]:
    """Split `text` by `sep` outside quotes and `()`, `[]`, `{}` brackets."""
    parts: list[str] = []
    buf: list[str] = []
    depth_paren = 0
    depth_brace = 0
    depth_bracket = 0
    quote: str | None = None
    escaped = False

    for ch in text:
        if escaped:
            buf.append(ch)
            escaped = False
            continue
        if quote is not None:
            buf.append(ch)
            if ch == "\\" and quote != "`":
                escaped = True
            elif ch == quote:
                quote = None
            continue

        if ch in ('"', "'", "`"):
            quote = ch
            buf.append(ch)
        elif ch == "(":
            depth_paren += 1
            buf.append(ch)
        elif ch == ")":
            depth_paren = max(0, depth_paren - 1)
            buf.append(ch)
        elif ch == "{":
            depth_brace += 1
            buf.append(ch)
        elif ch == "}":
            depth_brace = max(0, depth_brace - 1)
            buf.append(ch)
        elif ch == "[":
            depth_bracket += 1
            buf.append(ch)
        elif ch == "]":
            depth_bracket = max(0, depth_bracket - 1)
            buf.append(ch)
        elif ch == sep and depth_paren == 0 and depth_brace == 0 and depth_bracket == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)

    parts.append("".join(buf))
    return parts


def _find_matching_delim(text: str, start: int, open_ch: str, close_ch: str) -> int:
    """Find closing delimiter matching `text[start] == open_ch`, respecting quotes."""
    depth = 0
    quote: str | None = None
    escaped = False
    for idx in range(start, len(text)):
        ch = text[idx]
        if escaped:
            escaped = False
            continue
        if quote is not None:
            if ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
            continue
        if ch in ('"', "'"):
            quote = ch
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return idx
    return -1


def _strip_quotes(val: str) -> str:
    s = val.strip()
    if len(s) >= 2 and ((s[0] == '"' and s[-1] == '"') or (s[0] == "'" and s[-1] == "'")):
        return s[1:-1].replace('\\"', '"').replace("\\'", "'")
    return s


def _parse_perm_sigil(
    body: str, line_no: int, diagnostics: list[ParseDiagnostic]
) -> dict[str, AccessLevel]:
    perms: dict[str, AccessLevel] = {}
    for entry in _split_top_level(body, ","):
        part = entry.strip()
        if not part:
            continue
        if ":" not in part:
            diagnostics.append(
                ParseDiagnostic(line_no, 1, f"Invalid permission entry '{part}'; expected role:level.")
            )
            continue
        role, level = [x.strip() for x in part.split(":", 1)]
        if level not in ACCESS_ORDER:
            diagnostics.append(
                ParseDiagnostic(line_no, 1, f"Invalid access level '{level}' in permission sigil.")
            )
            continue
        perms[role] = level  # type: ignore[assignment]
    return perms


def _parse_modifiers(
    raw: str, line_no: int, diagnostics: list[ParseDiagnostic]
) -> ElementModifiers:
    text = raw.strip()
    if not text:
        return ElementModifiers()

    perms: dict[str, AccessLevel] | None = None
    when_expr: str | None = None
    span: int | None = None
    help_text: str | None = None
    placeholder: str | None = None
    readonly = False
    disabled = False

    idx = 0
    n = len(text)
    while idx < n:
        while idx < n and text[idx].isspace():
            idx += 1
        if idx >= n:
            break

        if text[idx : idx + 2] == "@(":
            end = _find_matching_delim(text, idx + 1, "(", ")")
            if end == -1:
                diagnostics.append(ParseDiagnostic(line_no, idx + 1, "Unclosed '@(...)' permission sigil."))
                break
            perms = _parse_perm_sigil(text[idx + 2 : end], line_no, diagnostics)
            idx = end + 1
            continue

        matched = False
        for kw in ("when", "span", "help", "placeholder", "disabled"):
            if text.startswith(f"{kw}(", idx):
                end = _find_matching_delim(text, idx + len(kw), "(", ")")
                if end == -1:
                    diagnostics.append(ParseDiagnostic(line_no, idx + 1, f"Unclosed '{kw}(...)' modifier."))
                    idx = n
                    matched = True
                    break
                inner = text[idx + len(kw) + 1 : end].strip()
                if kw == "when":
                    when_expr = inner
                elif kw == "span":
                    try:
                        span = int(inner)
                    except ValueError:
                        diagnostics.append(ParseDiagnostic(line_no, idx + 1, f"Invalid span value '{inner}'."))
                elif kw == "help":
                    help_text = _strip_quotes(inner)
                elif kw == "placeholder":
                    placeholder = _strip_quotes(inner)
                elif kw == "disabled":
                    disabled = True
                idx = end + 1
                matched = True
                break
        if matched:
            continue

        if text.startswith("readonly", idx) and (
            idx + 8 == n or text[idx + 8].isspace()
        ):
            readonly = True
            idx += 8
            continue
        if text.startswith("disabled", idx) and (
            idx + 8 == n or text[idx + 8].isspace()
        ):
            disabled = True
            idx += 8
            continue

        # Unknown trailing token
        next_ws = text.find(" ", idx)
        bad_tok = text[idx:] if next_ws == -1 else text[idx:next_ws]
        diagnostics.append(ParseDiagnostic(line_no, idx + 1, f"Unrecognized modifier '{bad_tok}'."))
        break

    return ElementModifiers(
        permissions=perms,
        when_expr=when_expr,
        span=span,
        help_text=help_text,
        placeholder=placeholder,
        readonly=readonly,
        disabled=disabled,
    )


def _parse_entity_query(raw: str) -> EntityQuerySpec:
    text = raw.strip()
    target_match = re.match(r"^([a-zA-Z_][a-zA-Z0-9_]*)", text)
    target_table = target_match.group(1) if target_match else None

    key_match = re.search(r"\.key\(\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\)", text)
    display_match = re.search(r"\.display\(\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\)", text)
    order_match = re.search(r"\.order\(\s*([a-zA-Z_][a-zA-Z0-9_]*)", text)

    return EntityQuerySpec(
        raw=text,
        target_table=target_table,
        key_col=key_match.group(1) if key_match else "id",
        display_col=display_match.group(1) if display_match else "name",
        order_col=order_match.group(1) if order_match else None,
    )


def _parse_options_list(raw: str) -> list[ChoiceOption]:
    options: list[ChoiceOption] = []
    for item in _split_top_level(raw, ","):
        tok = item.strip()
        if not tok:
            continue
        paren_idx = tok.find("(")
        if paren_idx != -1 and tok.endswith(")"):
            val = _strip_quotes(tok[:paren_idx].strip())
            lbl = _strip_quotes(tok[paren_idx + 1 : -1].strip())
            options.append(ChoiceOption(value=val, label=lbl))
        else:
            val = _strip_quotes(tok)
            options.append(ChoiceOption(value=val, label=val.replace("_", " ").title()))
    return options


def _parse_label_prefix(raw_label: str) -> tuple[bool, str]:
    stripped = raw_label.strip()
    if stripped.startswith("\\*"):
        return False, "*" + stripped[2:]
    if stripped.startswith("*"):
        return True, stripped[1:].strip()
    return False, stripped


def _split_arrow(text: str) -> tuple[str, str | None]:
    """Split `text` on the first top-level `->` outside quotes and brackets."""
    dp = db = dk = 0
    quote: str | None = None
    escaped = False
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if escaped:
            escaped = False
            i += 1
            continue
        if quote is not None:
            if ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
            i += 1
            continue
        if ch in ('"', "'"):
            quote = ch
        elif ch == "(":
            dp += 1
        elif ch == ")":
            dp = max(0, dp - 1)
        elif ch == "{":
            db += 1
        elif ch == "}":
            db = max(0, db - 1)
        elif ch == "[":
            dk += 1
        elif ch == "]":
            dk = max(0, dk - 1)
        elif ch == "-" and i + 1 < n and text[i + 1] == ">" and dp == 0 and db == 0 and dk == 0:
            return text[:i].strip(), text[i + 2 :].strip()
        i += 1
    return text.strip(), None


def _parse_link_target(
    raw_right: str,
) -> tuple[Literal["form", "server_action", "client_action"], str, str | None, dict[str, str]]:
    """Parse `form(args)`, `server_action:name`, or `client_action:name(args)`."""
    text = raw_right.strip()
    link_kind: Literal["form", "server_action", "client_action"] = "form"
    if text.startswith("server_action:"):
        link_kind = "server_action"
        text = text[len("server_action:") :].strip()
    elif text.startswith("client_action:"):
        link_kind = "client_action"
        text = text[len("client_action:") :].strip()

    p_idx = text.find("(")
    link_args: str | None = None
    if p_idx != -1 and text.endswith(")"):
        target = _strip_quotes(text[:p_idx].strip())
        link_args = text[p_idx + 1 : -1].strip()
    else:
        target = _strip_quotes(text)

    link_params: dict[str, str] = {}
    if link_args:
        for item in _split_top_level(link_args, ","):
            part = item.strip()
            if "=" in part:
                k, v = [x.strip() for x in part.split("=", 1)]
                if k:
                    link_params[k] = v

    return link_kind, target, link_args, link_params


def _parse_element(
    text: str, line_no: int, diagnostics: list[ParseDiagnostic]
) -> AstNode | None:
    s = text.strip()
    if not s:
        return None

    # 1. Button: [[Label]](id[:kind] [-> target]) [modifiers]
    if s.startswith("[["):
        close_lbl = s.find("]]", 2)
        if close_lbl == -1:
            diagnostics.append(ParseDiagnostic(line_no, 1, "Unclosed button label '[['."))
            return None
        label = s[2:close_lbl].strip()
        rest = s[close_lbl + 2 :].lstrip()
        if not rest.startswith("("):
            diagnostics.append(ParseDiagnostic(line_no, close_lbl + 3, "Expected '(action_id)' after button label."))
            return None
        close_paren = _find_matching_delim(rest, 0, "(", ")")
        if close_paren == -1:
            diagnostics.append(ParseDiagnostic(line_no, close_lbl + 3, "Unclosed button '(...)'."))
            return None
        spec = rest[1:close_paren].strip()
        mods = _parse_modifiers(rest[close_paren + 1 :], line_no, diagnostics)

        left_part, right_part = _split_arrow(spec)
        link_kind: Literal["form", "server_action", "client_action"] | None = None
        link_target: str | None = None
        link_args: str | None = None
        link_params: dict[str, str] = {}
        if right_part is not None:
            link_kind, link_target, link_args, link_params = _parse_link_target(right_part)

        kind: Literal["submit", "reset", "action", "link"]
        if ":" in left_part:
            action_id, raw_kind = [p.strip() for p in left_part.split(":", 1)]
            if raw_kind not in ("submit", "reset", "action", "link"):
                diagnostics.append(ParseDiagnostic(line_no, 1, f"Unknown button kind '{raw_kind}'."))
                raw_kind = "submit"
            kind = raw_kind  # type: ignore[assignment]
        else:
            action_id = left_part.strip()
            kind = "link" if link_target is not None else "submit"

        return ButtonNode(
            line=line_no,
            action_id=action_id,
            label=label,
            kind=kind,
            link_kind=link_kind,
            link_target=link_target,
            link_args=link_args,
            link_params=link_params,
            modifiers=mods,
        )

    # 2. Checkbox: [x] Label (id [= default]) or [ ] Label (id)
    cb_match = re.match(r"^\[([ xX])\]\s*(.+)$", s)
    if cb_match:
        checked = cb_match.group(1).lower() == "x"
        rest = cb_match.group(2).strip()
        open_p = rest.find("(")
        if open_p == -1:
            diagnostics.append(ParseDiagnostic(line_no, 1, "Expected '(field_id)' on checkbox element."))
            return None
        close_p = _find_matching_delim(rest, open_p, "(", ")")
        if close_p == -1:
            diagnostics.append(ParseDiagnostic(line_no, open_p + 1, "Unclosed checkbox '(...)'."))
            return None
        required, label = _parse_label_prefix(rest[:open_p])
        inner = rest[open_p + 1 : close_p].strip()
        mods = _parse_modifiers(rest[close_p + 1 :], line_no, diagnostics)
        default: str | None = None
        if "=" in inner:
            field_id, default = [p.strip() for p in inner.split("=", 1)]
        else:
            field_id = inner
        return CheckboxNode(
            line=line_no,
            field_id=field_id,
            label=label,
            checked=checked,
            required=required,
            default=default,
            modifiers=mods,
        )

    # 3. Bracketed label -> Input (...) or Choice {...}
    if s.startswith("["):
        close_lbl = _find_matching_delim(s, 0, "[", "]")
        if close_lbl == -1:
            diagnostics.append(ParseDiagnostic(line_no, 1, "Unclosed '[' in element label."))
            return None
        required, label = _parse_label_prefix(s[1:close_lbl])
        rest = s[close_lbl + 1 :].lstrip()

        if rest.startswith("("):
            close_p = _find_matching_delim(rest, 0, "(", ")")
            if close_p == -1:
                diagnostics.append(ParseDiagnostic(line_no, close_lbl + 2, "Unclosed '(...)' in input field."))
                return None
            inner = rest[1:close_p].strip()
            mods = _parse_modifiers(rest[close_p + 1 :], line_no, diagnostics)

            left_inner, right_link = _split_arrow(inner)
            in_link_kind: Literal["form", "server_action", "client_action"] | None = None
            in_link_target: str | None = None
            in_link_args: str | None = None
            in_link_params: dict[str, str] = {}
            if right_link is not None:
                in_link_kind, in_link_target, in_link_args, in_link_params = _parse_link_target(right_link)

            # Split default `=` outside nested `(...)`
            eq_parts = _split_top_level(left_inner, "=")
            head = eq_parts[0].strip()
            default = "=".join(eq_parts[1:]).strip() if len(eq_parts) > 1 else None

            field_type = "text"
            constraints_raw: str | None = None
            if ":" in head:
                field_id, type_part = [p.strip() for p in head.split(":", 1)]
                c_idx = type_part.find("(")
                if c_idx != -1 and type_part.endswith(")"):
                    field_type = type_part[:c_idx].strip()
                    constraints_raw = type_part[c_idx + 1 : -1].strip()
                else:
                    field_type = type_part
            else:
                field_id = head

            return InputNode(
                line=line_no,
                field_id=field_id,
                label=label,
                field_type=field_type,
                required=required,
                default=default,
                constraints_raw=constraints_raw,
                link_kind=in_link_kind,
                link_target=in_link_target,
                link_args=in_link_args,
                link_params=in_link_params,
                modifiers=mods,
            )

        if rest.startswith("{"):
            close_b = _find_matching_delim(rest, 0, "{", "}")
            if close_b == -1:
                diagnostics.append(ParseDiagnostic(line_no, close_lbl + 2, "Unclosed '{...}' in choice field."))
                return None
            inner = rest[1:close_b].strip()
            mods = _parse_modifiers(rest[close_b + 1 :], line_no, diagnostics)

            # Optional `| presentation` inside `{...}`
            top_pipe = _split_top_level(inner, "|")
            body = top_pipe[0].strip()
            presentation = top_pipe[1].strip() if len(top_pipe) > 1 else None

            entity_query: EntityQuerySpec | None = None
            options: list[ChoiceOption] = []
            if "->" in body:
                left, right = [p.strip() for p in body.split("->", 1)]
                entity_query = _parse_entity_query(right)
            elif ":" in body:
                left, right = [p.strip() for p in body.split(":", 1)]
                options = _parse_options_list(right)
            else:
                diagnostics.append(ParseDiagnostic(line_no, 1, "Choice '{...}' must contain ':' options or '->' query."))
                left = body

            default = None
            if "=" in left:
                left, default = [p.strip() for p in left.split("=", 1)]
            multiple = left.endswith("[]")
            field_id = left[:-2].strip() if multiple else left.strip()

            return ChoiceNode(
                line=line_no,
                field_id=field_id,
                label=label,
                multiple=multiple,
                required=required,
                default=default,
                options=options,
                entity_query=entity_query,
                presentation=presentation,
                modifiers=mods,
            )

    return None


def _parse_block_header(
    content: str, line_no: int, diagnostics: list[ParseDiagnostic]
) -> BlockNode | EmbedNode:
    """Parse `::: <kind> [id] ["Title"] [args] [[ratios]] [modifiers]`."""
    rest = content[3:].strip()
    if not rest:
        diagnostics.append(ParseDiagnostic(line_no, 1, "Expected block type after ':::'."))
        return BlockNode(line=line_no, kind="group")

    # Extract kind
    match_kind = re.match(r"^([a-zA-Z_][a-zA-Z0-9_]*)\s*(.*)$", rest)
    assert match_kind is not None
    kind = match_kind.group(1)
    tail = match_kind.group(2).strip()

    if kind == "embed":
        # target[(args)] [modifiers]
        t_match = re.match(r'^([a-zA-Z_][a-zA-Z0-9_]*|"[^"]+")\s*(.*)$', tail)
        if not t_match:
            diagnostics.append(ParseDiagnostic(line_no, 1, "Expected target form after '::: embed'."))
            return EmbedNode(line=line_no, target="")
        target = _strip_quotes(t_match.group(1))
        rem = t_match.group(2).strip()
        args_raw: str | None = None
        if rem.startswith("("):
            end_p = _find_matching_delim(rem, 0, "(", ")")
            if end_p != -1:
                args_raw = rem[1:end_p].strip()
                rem = rem[end_p + 1 :].strip()
        mods = _parse_modifiers(rem, line_no, diagnostics)
        return EmbedNode(line=line_no, target=target, args_raw=args_raw, modifiers=mods)

    block_id: str | None = None
    title: str | None = None
    args_raw = None
    ratios: list[float] | None = None
    form_type: Literal["detail", "list"] = "detail"
    params: list[str] = []
    entity_query: EntityQuerySpec | None = None

    if kind == "list":
        # Parse `<entity_query>` optionally followed by `"Title"` and modifiers
        q_buf = tail
        stage_match = re.match(
            r"^([a-zA-Z_][a-zA-Z0-9_]*(?:\([^)]*\))?(?:\.[a-zA-Z_][a-zA-Z0-9_]*\([^)]*\))*)\s*(.*)$",
            q_buf,
        )
        if stage_match:
            query_str = stage_match.group(1).strip()
            tail = stage_match.group(2).strip()
            entity_query = _parse_entity_query(query_str)
            block_id = entity_query.target_table
        else:
            diagnostics.append(ParseDiagnostic(line_no, 1, "Expected entity query after '::: list'."))
    else:
        # Optional identifier (not a modifier keyword)
        id_match = re.match(r"^([a-zA-Z_][a-zA-Z0-9_]*)\b(.*)$", tail)
        if id_match and id_match.group(1) not in ("when", "span", "help", "placeholder", "readonly", "disabled"):
            if not id_match.group(2).lstrip().startswith("(") or kind in ("form", "repeat"):
                block_id = id_match.group(1)
                tail = id_match.group(2).strip()

        # Optional `(args)` right after kind/id
        if tail.startswith("("):
            end_p = _find_matching_delim(tail, 0, "(", ")")
            if end_p != -1:
                args_raw = tail[1:end_p].strip()
                tail = tail[end_p + 1 :].strip()

        if kind == "form" and args_raw:
            type_m = re.search(r"\btype\s*:\s*(detail|list)\b", args_raw)
            if type_m:
                form_type = type_m.group(1)  # type: ignore[assignment]
            params_m = re.search(r"\bparams\s*:\s*\[([^\]]*)\]", args_raw)
            if params_m:
                params = [p.strip() for p in params_m.group(1).split(",") if p.strip()]

    # Optional `"Title"`
    if tail.startswith('"') or tail.startswith("'"):
        q = tail[0]
        end_q = tail.find(q, 1)
        if end_q != -1:
            title = _strip_quotes(tail[: end_q + 1])
            tail = tail[end_q + 1 :].strip()

    # Optional `[r1:r2:...]`
    if tail.startswith("["):
        end_r = tail.find("]")
        if end_r != -1:
            ratio_tok = tail[: end_r + 1]
            r_match = _RATIO_RE.match(ratio_tok)
            if r_match:
                ratios = [float(x.strip()) for x in r_match.group(1).split(":")]
                tail = tail[end_r + 1 :].strip()

    mods = _parse_modifiers(tail, line_no, diagnostics)
    return BlockNode(
        line=line_no,
        kind=kind,
        block_id=block_id,
        title=title,
        args_raw=args_raw,
        ratios=ratios,
        form_type=form_type,
        params=params,
        entity_query=entity_query,
        modifiers=mods,
    )


def parse_formspec(source: str) -> FormDocument:
    """Parse FormSpec `.fspec` source into a `FormDocument` AST + diagnostics."""
    diagnostics: list[ParseDiagnostic] = []
    root = BlockNode(line=1, kind="form", block_id="root", title="Form")
    stack: list[tuple[BlockNode, int]] = [(root, -1)]
    explicit_forms: list[BlockNode] = []

    raw_lines = source.splitlines()
    idx = 0
    in_block_comment = False

    while idx < len(raw_lines):
        line_no = idx + 1
        raw_line = raw_lines[idx]
        idx += 1

        if in_block_comment:
            if "*/" in raw_line:
                in_block_comment = False
            continue

        stripped = raw_line.strip()
        if not stripped:
            continue
        if stripped.startswith("//"):
            continue
        if stripped.startswith("/*"):
            if "*/" not in stripped[2:]:
                in_block_comment = True
            continue

        # Count leading tabs strictly
        tabs = 0
        while tabs < len(raw_line) and raw_line[tabs] == "\t":
            tabs += 1
        if tabs < len(raw_line) and raw_line[tabs] == " ":
            diagnostics.append(
                ParseDiagnostic(
                    line_no,
                    tabs + 1,
                    "Indentation must use tabs ('\\t'), not leading spaces.",
                )
            )
            # Recover by counting spaces gracefully after reporting error
            content = raw_line.lstrip(" \t")
        else:
            content = raw_line[tabs:]

        # Join balanced multi-line delimiters `(...)` / `{...}`
        def _balance_delta(s: str) -> tuple[int, int]:
            dp = db = 0
            q: str | None = None
            esc = False
            for ch in s:
                if esc:
                    esc = False
                    continue
                if q:
                    if ch == "\\":
                        esc = True
                    elif ch == q:
                        q = None
                    continue
                if ch in ('"', "'"):
                    q = ch
                elif ch == "(":
                    dp += 1
                elif ch == ")":
                    dp -= 1
                elif ch == "{":
                    db += 1
                elif ch == "}":
                    db -= 1
            return dp, db

        dp, db = _balance_delta(content)
        while (dp > 0 or db > 0) and idx < len(raw_lines):
            next_line = raw_lines[idx].strip()
            idx += 1
            content = f"{content} {next_line}"
            dp, db = _balance_delta(content)

        # Pop closed tab blocks
        while len(stack) > 1 and tabs <= stack[-1][1]:
            stack.pop()
        parent_block, parent_depth = stack[-1]

        if len(stack) > 1 and tabs > parent_depth + 1:
            diagnostics.append(
                ParseDiagnostic(
                    line_no,
                    1,
                    f"Unexpected indentation depth ({tabs} tabs; expected {parent_depth + 1}).",
                )
            )

        # Parse statement
        if content.startswith(":::"):
            parsed = _parse_block_header(content, line_no, diagnostics)
            if isinstance(parsed, EmbedNode):
                parent_block.children.append(parsed)
            else:
                if parsed.kind == "form" and len(stack) == 1:
                    explicit_forms.append(parsed)
                else:
                    parent_block.children.append(parsed)
                stack.append((parsed, tabs))
            continue

        if content == "---":
            parent_block.children.append(DividerNode(line=line_no))
            continue

        # Row line (pipe-separated columns)
        pipe_parts = _split_top_level(content, "|")
        if len(pipe_parts) > 1:
            ratios: list[float] | None = None
            trailing = pipe_parts[-1].strip()
            if trailing == "":
                elem_strs = pipe_parts[:-1]
            else:
                r_match = _RATIO_RE.match(trailing)
                if r_match:
                    ratios = [float(x.strip()) for x in r_match.group(1).split(":")]
                    elem_strs = pipe_parts[:-1]
                else:
                    elem_strs = pipe_parts

            if elem_strs and elem_strs[0].strip() == "":
                elem_strs = elem_strs[1:]

            row_elems: list[AstNode] = []
            for seg in elem_strs:
                seg_clean = seg.strip()
                if not seg_clean:
                    continue
                diag_before = len(diagnostics)
                el = _parse_element(seg_clean, line_no, diagnostics)
                if el is not None:
                    row_elems.append(el)
                    continue
                seg_h = _HEADING_RE.match(seg_clean)
                if seg_h:
                    row_elems.append(
                        HeadingNode(
                            line=line_no,
                            level=len(seg_h.group(1)),
                            text=seg_h.group(2).strip(),
                        )
                    )
                elif seg_clean == "---":
                    row_elems.append(DividerNode(line=line_no))
                elif seg_clean.startswith("[") or seg_clean.startswith(":::"):
                    if len(diagnostics) == diag_before:
                        diagnostics.append(
                            ParseDiagnostic(
                                line_no, 1, f"Invalid element in pipe row: '{seg_clean}'."
                            )
                        )
                else:
                    row_elems.append(ProseNode(line=line_no, text=seg_clean))

            if ratios is not None and len(ratios) != len(row_elems):
                diagnostics.append(
                    ParseDiagnostic(
                        line_no,
                        1,
                        f"Ratio count ({len(ratios)}) does not match element count ({len(row_elems)}).",
                    )
                )

            if row_elems:
                parent_block.children.append(
                    RowNode(line=line_no, elements=row_elems, ratios=ratios)
                )
            continue

        h_match = _HEADING_RE.match(content)
        if h_match:
            parent_block.children.append(
                HeadingNode(
                    line=line_no,
                    level=len(h_match.group(1)),
                    text=h_match.group(2).strip(),
                )
            )
            continue

        elem = _parse_element(content, line_no, diagnostics)
        if elem is not None:
            parent_block.children.append(elem)
        else:
            parent_block.children.append(ProseNode(line=line_no, text=content))

    forms = explicit_forms if explicit_forms else [root]
    return FormDocument(forms=forms, diagnostics=diagnostics)
