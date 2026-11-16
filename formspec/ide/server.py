"""HTTP server and REST API for the FormSpec Prototype IDE."""

from __future__ import annotations

from dataclasses import asdict
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Any
from urllib.parse import unquote, urlparse

from formspec.db import get_adapter
from formspec.db.base import AttributeSpec, WorkspaceAdapter
from formspec.db.sqlite import SQLiteAdapter
from formspec.formatters import FormatterRegistry, RenderContext, create_default_registry
from formspec.generators.db_init import list_dialects
from formspec.generators.client_action import (
    CLIENT_MODULE_NAME,
    HEADER_MODULE_NAME,
    generate_detail_client_action,
    generate_list_client_action,
    get_client_module_code,
    get_header_client_module_code,
)
from formspec.generators.fspec import generate_fspec, sync_fspec_permissions
from formspec.generators.server_action_node import (
    generate_node_server_action,
    sync_entity_config_in_node_action,
)
from formspec.generators.server_action_python import generate_python_server_action
from formspec.generators.server_module_node import (
    SERVER_MODULE_NAME,
    get_node_server_module_code,
)
from formspec.ide.auth import SessionManager, hash_password, verify_password
from formspec.ide.git_sync import GitConfig, GitSyncError, GitWorkspaceManager
from formspec.ide.packager import AppPackager
from formspec.introspect import introspect_entity
from formspec.parser import (
    AstNode,
    BlockNode,
    ButtonNode,
    ChoiceNode,
    EmbedNode,
    FormDocument,
    InputNode,
    RowNode,
    collect_declared_roles,
    parse_formspec,
)

_STATIC_DIR = Path(__file__).parent / "static"
_BUILTIN_THEMES_DIR = Path(__file__).parent / "themes"


class HttpError(Exception):
    """Structured HTTP error returned as JSON."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _collect_entity_queries(node: AstNode) -> list[ChoiceNode]:
    """Find all ChoiceNode elements with `->` entity queries in an AST."""
    found: list[ChoiceNode] = []
    if isinstance(node, ChoiceNode) and node.entity_query is not None:
        found.append(node)
    elif isinstance(node, BlockNode):
        for child in node.children:
            found.extend(_collect_entity_queries(child))
    elif isinstance(node, RowNode):
        for elem in node.elements:
            found.extend(_collect_entity_queries(elem))
    return found


def _collect_list_blocks(node: AstNode) -> list[BlockNode]:
    """Find all `::: list` blocks in an AST."""
    found: list[BlockNode] = []
    if isinstance(node, BlockNode):
        if node.kind == "list":
            found.append(node)
        for child in node.children:
            found.extend(_collect_list_blocks(child))
    return found


_NAVIGATE_CALL_RE = re.compile(
    r"""(?:navigateToForm\s*\(\s*ctx\s*,|ctx\.navigate\s*\()\s*["']([a-zA-Z0-9_]+)["']"""
)

_DEFAULT_HOME_FSPEC = (
    '::: form home "Home" @(admin:write, guest:read, anon:read, *:read)\n'
    "\t# Home\n"
    '\tEntry page of the application. Add navigation links (`[[Label]](open -> form_name)`) below.\n'
    "\n"
    '\t::: section "Navigation"\n'
)


def _walk_form_links(
    node: AstNode,
    out: list[tuple[str | None, str, str, str]],
) -> None:
    """Collect `(link_kind, target, label, kind_tag)` edges from parsed FormSpec AST."""
    if isinstance(node, EmbedNode):
        out.append(("form", node.target, "embed", "embed"))
    elif isinstance(node, (ButtonNode, InputNode)) and node.link_target:
        out.append((node.link_kind, node.link_target, node.label, node.link_kind or "form"))
    elif isinstance(node, BlockNode):
        for child in node.children:
            _walk_form_links(child, out)
    elif isinstance(node, RowNode):
        for elem in node.elements:
            _walk_form_links(elem, out)


def _build_form_map(adapter: WorkspaceAdapter) -> dict[str, Any]:
    """Build node/edge graph and Mermaid flowchart source for all forms."""
    forms = adapter.list_forms()
    form_names = {f.name for f in forms}
    client_actions = {ca.name: ca for ca in adapter.list_client_actions()}

    nodes = [
        {
            "id": f.id,
            "name": f.name,
            "label": f.label,
            "form_type": f.form_type,
            "entity_name": f.entity_name,
            "is_home": f.name == "home",
        }
        for f in forms
    ]

    edges: list[dict[str, str]] = []
    seen_edges: set[tuple[str, str, str, str]] = set()

    def add_edge(src: str, dst: str, label: str, kind: str) -> None:
        key = (src, dst, label, kind)
        if key not in seen_edges:
            seen_edges.add(key)
            edges.append({"from": src, "to": dst, "label": label, "kind": kind})

    for form in forms:
        doc = parse_formspec(form.fspec_source)
        raw_links: list[tuple[str | None, str, str, str]] = []
        for ast_form in doc.forms:
            _walk_form_links(ast_form, raw_links)

        for link_kind, target, label, kind_tag in raw_links:
            if link_kind in (None, "form"):
                add_edge(form.name, target, label, kind_tag)
            elif link_kind == "client_action":
                ca = client_actions.get(target)
                targets: list[str] = []
                if ca is not None:
                    if ca.target_form_name:
                        targets.append(ca.target_form_name)
                    for match in _NAVIGATE_CALL_RE.findall(ca.code or ""):
                        if match not in targets:
                            targets.append(match)
                for dst in targets:
                    if dst in form_names or dst != form.name:
                        add_edge(form.name, dst, f"{label} ({target})", "client_action")

    mermaid_lines = ["flowchart LR"]
    for n in nodes:
        safe_title = f"{n['label']} ({n['name']})".replace('"', "'")
        if n["is_home"]:
            mermaid_lines.append(f'  {n["name"]}(["{safe_title}"])')
        elif n["form_type"] == "list":
            mermaid_lines.append(f'  {n["name"]}[["{safe_title}"]]')
        else:
            mermaid_lines.append(f'  {n["name"]}["{safe_title}"]')

    for e in edges:
        safe_lbl = e["label"].replace('"', "'")
        arrow = "-.->" if e["kind"] == "embed" else "-->"
        mermaid_lines.append(f'  {e["from"]} {arrow}|"{safe_lbl}"| {e["to"]}')

    return {
        "nodes": nodes,
        "edges": edges,
        "mermaid": "\n".join(mermaid_lines) + "\n",
    }


def resolve_default_dsn(explicit_dsn: str | None = None) -> str:
    """Resolve database connection string from CLI arg, env vars, or SQLite default."""
    return (
        explicit_dsn
        or os.environ.get("FORMSPEC_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
        or "sqlite:///formspec_studio.db"
    )


class IDEApp:
    """Stateful application controller backing the FormSpec IDE HTTP endpoints."""

    def __init__(
        self,
        dsn: str | None = None,
        *,
        formatters_dir: str | Path | None = None,
        styles_dir: str | Path | None = None,
        secret: str | None = None,
        git_config: GitConfig | None = None,
        package_staging_dir: Path | None = None,
    ) -> None:
        self.dsn = resolve_default_dsn(dsn)
        self.formatters_dir = formatters_dir or os.environ.get("FORMSPEC_FORMATTERS_DIR")
        self.styles_dir = styles_dir or os.environ.get("FORMSPEC_STYLES_DIR")
        self.registry: FormatterRegistry = create_default_registry(self.formatters_dir)
        self.sessions = SessionManager(secret=secret)
        self.git = GitWorkspaceManager(git_config)
        self.packager = AppPackager(staging_root=package_staging_dir)

        # Initialize database tables on first open if not yet initialized
        with self.open_adapter() as adapter:
            if not adapter.is_initialized():
                adapter.initialize_schema()
            self._ensure_default_workspace(adapter)

    def _sync_git_on_login(self) -> tuple[dict[str, Any], str | None]:
        """Clone configured remote Git repo into ephemeral storage on login."""
        if not self.git.config.configured:
            return self.git.status(), None
        try:
            return self.git.ensure_cloned(), None
        except GitSyncError as exc:
            st = self.git.status()
            return st, str(exc)

    def _ensure_default_workspace(self, adapter: WorkspaceAdapter) -> None:
        """Ensure shared modules and entry `home` form exist in DB."""
        if adapter.get_server_module_by_name(SERVER_MODULE_NAME) is None:
            adapter.save_server_module(
                name=SERVER_MODULE_NAME,
                label="FormSpec Server Runtime",
                code=get_node_server_module_code(),
                description="Shared Node.js DB, RBAC, and upsert helpers reused by server actions.",
                target_runtime="node",
            )
        if adapter.get_client_module_by_name(CLIENT_MODULE_NAME) is None:
            adapter.save_client_module(
                name=CLIENT_MODULE_NAME,
                label="FormSpec Client Runtime",
                code=get_client_module_code(),
                description="Shared browser validation and action invocation helpers reused by client actions.",
            )
        if adapter.get_client_module_by_name(HEADER_MODULE_NAME) is None:
            adapter.save_client_module(
                name=HEADER_MODULE_NAME,
                label="App Header & Breadcrumbs",
                code=get_header_client_module_code(),
                description="Customizable page header and navigation breadcrumb renderer.",
            )
        existing_forms = adapter.list_forms()
        if not any(f.name == "home" for f in existing_forms):
            home_rec = adapter.create_form(
                name="home",
                label="Home",
                fspec_source=_DEFAULT_HOME_FSPEC,
                description="Default entry page for the web application.",
                form_type="detail",
            )
            self._create_form_client_action(adapter, home_rec)

    def _ensure_entity_server_action(
        self,
        adapter: WorkspaceAdapter,
        entity_name: str,
        *,
        sync_existing: bool = True,
    ) -> Any:
        """Create `<entity>_upsert` server action on entity creation, or refresh its ENTITY_CONFIG."""
        schema = introspect_entity(adapter, entity_name)
        sa_name = f"{entity_name}_upsert"
        existing = adapter.get_server_action_by_name(sa_name)
        if existing is None:
            code = generate_node_server_action(schema)
            return adapter.save_server_action(
                name=sa_name,
                label=f"{schema.display_name} Upsert",
                code=code,
                entity_name=entity_name,
                description=f"Auto-generated Node.js upsert server action for {entity_name}",
                action_type="upsert",
                target_runtime="node",
            )
        if sync_existing and existing.target_runtime == "node":
            refreshed = sync_entity_config_in_node_action(existing.code, schema)
            if refreshed != existing.code:
                return adapter.update_server_action(existing.id, code=refreshed)
        return existing

    def _create_form_client_action(
        self,
        adapter: WorkspaceAdapter,
        form_rec: Any,
        *,
        detail_form_name: str | None = None,
    ) -> Any:
        """Create a default client action whenever a form is created."""
        if form_rec.name == "home" and not form_rec.entity_name:
            base_name = "home_open"
            existing_home_ca = adapter.get_client_action_by_name(base_name)
            if existing_home_ca is not None:
                return existing_home_ca
            code = generate_list_client_action(
                "home", target_form_name="home"
            )
            return adapter.create_client_action(
                name=base_name,
                label="Navigate From Home",
                target_form_name="home",
                description="Entry navigation client action for the home form.",
                action_type="link",
                params_spec="{}",
                code=code,
            )

        suffix = "open" if form_rec.form_type == "list" else "submit"
        base_name = f"{form_rec.name}_{suffix}"
        existing = adapter.get_client_action_by_name(base_name)
        if existing is not None:
            return existing

        if form_rec.form_type == "list":
            target_detail = detail_form_name
            if not target_detail and form_rec.entity_name:
                matching = next(
                    (
                        f
                        for f in adapter.list_forms()
                        if f.entity_name == form_rec.entity_name and f.form_type == "detail"
                    ),
                    None,
                )
                target_detail = matching.name if matching else f"{form_rec.entity_name}_form"
            target_detail = target_detail or f"{form_rec.name}_detail"
            code = generate_list_client_action(
                form_rec.name,
                target_form_name=target_detail,
                entity_name=form_rec.entity_name,
            )
            return adapter.create_client_action(
                name=base_name,
                label=f"Open {form_rec.label}",
                target_form_name=target_detail,
                description=f"Auto-generated client action for list form {form_rec.name}",
                action_type="link",
                params_spec='{"id": "$selected.id"}',
                code=code,
            )

        schema = (
            introspect_entity(adapter, form_rec.entity_name)
            if form_rec.entity_name
            else None
        )
        code = generate_detail_client_action(
            form_rec.name,
            entity_name=form_rec.entity_name,
            schema=schema,
        )
        return adapter.create_client_action(
            name=base_name,
            label=f"Submit {form_rec.label}",
            description=f"Auto-generated validation & submit client action for {form_rec.name}",
            action_type="submit",
            code=code,
        )

    def open_adapter(self) -> WorkspaceAdapter:
        adapter = get_adapter(self.dsn)
        if not isinstance(adapter, WorkspaceAdapter):
            adapter.close()
            raise RuntimeError("Configured DatabaseAdapter does not implement WorkspaceAdapter.")
        return adapter

    def list_themes(self) -> list[dict[str, str]]:
        themes: dict[str, dict[str, str]] = {}
        for directory in (_BUILTIN_THEMES_DIR, Path(self.styles_dir) if self.styles_dir else None):
            if directory is None or not directory.is_dir():
                continue
            for css_file in sorted(directory.glob("*.css")):
                tid = css_file.stem
                themes[tid] = {
                    "id": tid,
                    "name": tid.replace("_", " ").replace("-", " ").title(),
                    "path": f"/themes/{tid}.css",
                }
        return list(themes.values())

    def read_theme_css(self, theme_id: str) -> str | None:
        safe_id = Path(theme_id).name
        if not safe_id or "/" in theme_id or "\\" in theme_id:
            return None
        for directory in (Path(self.styles_dir) if self.styles_dir else None, _BUILTIN_THEMES_DIR):
            if directory is None:
                continue
            candidate = directory / f"{safe_id}.css"
            if candidate.is_file():
                return candidate.read_text(encoding="utf-8")
        return None

    def _require_auth(self, adapter: WorkspaceAdapter, token: str | None) -> dict[str, Any]:
        if not adapter.has_superuser():
            raise HttpError(401, "Database requires initial superuser setup.")
        uid = self.sessions.verify_token(token)
        if uid is None:
            raise HttpError(401, "Authentication required.")
        user = adapter.get_user_by_id(uid)
        if user is None:
            raise HttpError(401, "Invalid or expired session.")
        return {
            "id": user.id,
            "username": user.username,
            "is_superuser": user.is_superuser,
            "roles": user.roles,
        }

    def _execute_stored_server_action(
        self,
        adapter: WorkspaceAdapter,
        sa_code: str,
        runtime: str,
        role: str,
        auth_user_id: int,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Execute a stored Node.js or Python server action script under `role`."""
        db_path = getattr(adapter, "db_path", None)
        if not db_path or db_path == ":memory:":
            raise HttpError(400, "Stored script execution requires a file-backed SQLite DB.")

        temp_user_id: int | None = None
        acting_user_id = auth_user_id

        if role != "superuser":
            role_row = adapter.conn.execute(  # type: ignore[attr-defined]
                "SELECT id FROM roles WHERE name = ? LIMIT 1", (role,)
            ).fetchone()
            if role_row is None:
                raise HttpError(403, f"Unknown role '{role}'.")
            role_id = int(role_row["id"])
            with adapter.conn:  # type: ignore[attr-defined]
                cur = adapter.conn.execute(  # type: ignore[attr-defined]
                    "INSERT INTO users (username, password_hash, is_superuser) VALUES (?, '', 0)",
                    (f"__preview_{role}_{os.urandom(4).hex()}",),
                )
                temp_user_id = int(cur.lastrowid)
                adapter.conn.execute(  # type: ignore[attr-defined]
                    "INSERT INTO user_roles (user_id, role_id) VALUES (?, ?)",
                    (temp_user_id, role_id),
                )
            acting_user_id = temp_user_id

        try:
            suffix = ".mjs" if runtime == "node" else ".py"
            cmd_bin = "node" if runtime == "node" else sys.executable
            with tempfile.TemporaryDirectory() as tmpdir:
                tmp_path = Path(tmpdir)
                if runtime == "node":
                    for mod in adapter.list_server_modules():
                        if mod.target_runtime == "node":
                            (tmp_path / f"{mod.name}.mjs").write_text(
                                mod.code, encoding="utf-8"
                            )
                    if not (tmp_path / f"{SERVER_MODULE_NAME}.mjs").exists():
                        (tmp_path / f"{SERVER_MODULE_NAME}.mjs").write_text(
                            get_node_server_module_code(), encoding="utf-8"
                        )
                script_path = tmp_path / f"action{suffix}"
                script_path.write_text(sa_code, encoding="utf-8")
                proc = subprocess.run(
                    [
                        cmd_bin,
                        str(script_path),
                        "--db",
                        str(db_path),
                        "--user-id",
                        str(acting_user_id),
                        "--action",
                        "upsert",
                        "--payload",
                        json.dumps(payload),
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
        finally:
            if temp_user_id is not None:
                with adapter.conn:  # type: ignore[attr-defined]
                    adapter.conn.execute(  # type: ignore[attr-defined]
                        "DELETE FROM users WHERE id = ?", (temp_user_id,)
                    )

        raw_out = (proc.stdout or "").strip()
        try:
            parsed = json.loads(raw_out) if raw_out else {}
        except json.JSONDecodeError as exc:
            raise HttpError(
                500, f"Server action output error: {proc.stderr or raw_out}"
            ) from exc

        if proc.returncode != 0:
            raise HttpError(
                int(parsed.get("status_code") or 500),
                str(parsed.get("error") or proc.stderr or "Server action failed."),
            )
        return parsed

    def handle_api(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        session_token: str | None = None,
    ) -> tuple[int, dict[str, Any], str | None]:
        """Dispatch an API call and return `(status_code, json_payload, set_cookie_header)`."""
        payload = body or {}
        set_cookie: str | None = None

        with self.open_adapter() as adapter:
            # --- Public auth & bootstrap endpoints ---
            if path == "/api/auth/status" and method == "GET":
                has_su = adapter.has_superuser()
                uid = self.sessions.verify_token(session_token) if has_su else None
                user = adapter.get_user_by_id(uid) if uid is not None else None
                return (
                    200,
                    {
                        "initialized": adapter.is_initialized(),
                        "has_superuser": has_su,
                        "is_sqlite": isinstance(adapter, SQLiteAdapter),
                        "authenticated": user is not None,
                        "user": (
                            {
                                "id": user.id,
                                "username": user.username,
                                "is_superuser": user.is_superuser,
                                "roles": user.roles,
                            }
                            if user
                            else None
                        ),
                    },
                    None,
                )

            if path == "/api/auth/setup" and method == "POST":
                if adapter.has_superuser():
                    raise HttpError(409, "Superuser has already been created.")
                username = str(payload.get("username") or "superuser").strip()
                password = str(payload.get("password") or "")
                try:
                    pw_hash = hash_password(password)
                    uid = adapter.create_superuser(username, pw_hash)
                except ValueError as exc:
                    raise HttpError(400, str(exc)) from exc
                token = self.sessions.create_token(uid)
                set_cookie = (
                    f"{SessionManager.COOKIE_NAME}={token}; HttpOnly; Path=/; SameSite=Lax"
                )
                user = adapter.get_user_by_id(uid)
                git_status, git_error = self._sync_git_on_login()
                return (
                    201,
                    {
                        "authenticated": True,
                        "user": {
                            "id": uid,
                            "username": user.username if user else username,
                            "is_superuser": True,
                            "roles": user.roles if user else ["admin"],
                        },
                        "git": git_status,
                        "git_error": git_error,
                    },
                    set_cookie,
                )

            if path == "/api/auth/login" and method == "POST":
                username = str(payload.get("username") or "").strip()
                password = str(payload.get("password") or "")
                user = adapter.get_user_by_username(username)
                if user is None or not verify_password(password, user.password_hash):
                    raise HttpError(401, "Invalid username or password.")
                token = self.sessions.create_token(user.id)
                set_cookie = (
                    f"{SessionManager.COOKIE_NAME}={token}; HttpOnly; Path=/; SameSite=Lax"
                )
                git_status, git_error = self._sync_git_on_login()
                return (
                    200,
                    {
                        "authenticated": True,
                        "user": {
                            "id": user.id,
                            "username": user.username,
                            "is_superuser": user.is_superuser,
                            "roles": user.roles,
                        },
                        "git": git_status,
                        "git_error": git_error,
                    },
                    set_cookie,
                )

            if path == "/api/auth/logout" and method == "POST":
                self.git.cleanup()
                clear_cookie = f"{SessionManager.COOKIE_NAME}=; HttpOnly; Path=/; Max-Age=0"
                return (200, {"authenticated": False}, clear_cookie)

            # --- Authenticated workspace endpoints ---
            auth_user = self._require_auth(adapter, session_token)

            # --- Git integration endpoints ---
            if path == "/api/git/status" and method == "GET":
                git_error: str | None = None
                if self.git.config.configured and not self.git.is_cloned:
                    st, git_error = self._sync_git_on_login()
                else:
                    st = self.git.status()
                return (200, {"git": st, "git_error": git_error}, None)

            if path == "/api/git/checkout" and method == "POST":
                branch = str(payload.get("branch") or "").strip()
                create = bool(payload.get("create", False))
                try:
                    st = self.git.checkout_branch(branch, create=create)
                except GitSyncError as exc:
                    raise HttpError(400, str(exc)) from exc
                return (200, {"git": st}, None)

            if path == "/api/git/publish" and method == "POST":
                try:
                    pub = self.git.publish_branch_to_db(adapter)
                    self._ensure_default_workspace(adapter)
                except GitSyncError as exc:
                    raise HttpError(400, str(exc)) from exc
                return (200, {"result": pub, "git": self.git.status()}, None)

            if path == "/api/git/push" and method == "POST":
                msg = str(payload.get("message") or "Update FormSpec workspace").strip()
                try:
                    res = self.git.commit_and_push(adapter, message=msg)
                except GitSyncError as exc:
                    raise HttpError(400, str(exc)) from exc
                return (200, res, None)

            # --- Packaging endpoints ---
            if path == "/api/package/dialects" and method == "GET":
                return (200, {"dialects": list_dialects()}, None)

            if path == "/api/package" and method == "POST":
                app_name = str(payload.get("app_name") or "formspec-app").strip()
                dialect_id = str(payload.get("dialect") or "sqlite").strip().lower()
                include_data = bool(payload.get("include_data", False))
                build_docker = bool(payload.get("build_docker", False))
                try:
                    bundle = self.packager.package(
                        adapter,
                        app_name=app_name,
                        dialect_id=dialect_id,
                        include_data=include_data,
                        build_docker=build_docker,
                    )
                except ValueError as exc:
                    raise HttpError(400, str(exc)) from exc
                return (
                    201,
                    {
                        "bundle_id": bundle.bundle_id,
                        "app_name": bundle.app_name,
                        "image_tag": bundle.image_tag,
                        "dialect": bundle.dialect,
                        "dialect_label": bundle.dialect_label,
                        "init_sql_filename": bundle.init_sql_filename,
                        "init_sql": bundle.init_sql,
                        "bundle_dir": str(bundle.bundle_dir),
                        "download_url": f"/api/package/download/{bundle.bundle_id}",
                        "docker_built": bundle.docker_built,
                        "docker_log": bundle.docker_log,
                    },
                    None,
                )

            if path == "/api/admin/reset-db" and method == "POST":
                if not auth_user["is_superuser"]:
                    raise HttpError(
                        403, "Only superusers can wipe and re-initialize the database."
                    )
                if not isinstance(adapter, SQLiteAdapter):
                    raise HttpError(
                        400, "Database wipe and re-initialization is only supported for SQLite."
                    )
                adapter.reset_database()
                self._ensure_default_workspace(adapter)
                clear_cookie = f"{SessionManager.COOKIE_NAME}=; HttpOnly; Path=/; Max-Age=0"
                return (
                    200,
                    {
                        "reset": True,
                        "initialized": True,
                        "has_superuser": False,
                        "is_sqlite": True,
                        "authenticated": False,
                    },
                    clear_cookie,
                )

            if path == "/api/entities" and method == "GET":
                return (200, {"entities": adapter.list_domain_entities()}, None)

            if path == "/api/entities" and method == "POST":
                name = str(payload.get("name") or "").strip()
                label = str(payload.get("label") or name.replace("_", " ").title()).strip()
                description = payload.get("description")
                raw_attrs = payload.get("attributes") or []
                attrs = [
                    AttributeSpec(
                        name=str(a["name"]),
                        fspec_type=str(a.get("fspec_type", "text")),
                        nullable=bool(a.get("nullable", True)),
                        default=a.get("default"),
                        fk_table=a.get("fk_table"),
                    )
                    for a in raw_attrs
                ]
                try:
                    entity_id = adapter.create_entity(name, label, description, attrs)
                    sa_rec = self._ensure_entity_server_action(adapter, name)
                except ValueError as exc:
                    raise HttpError(400, str(exc)) from exc
                return (
                    201,
                    {
                        "id": entity_id,
                        "entities": adapter.list_domain_entities(),
                        "server_action": asdict(sa_rec),
                    },
                    None,
                )

            if path.startswith("/api/entities/") and path.endswith("/attributes") and method == "POST":
                entity_name = unquote(path[len("/api/entities/") : -len("/attributes")])
                spec = AttributeSpec(
                    name=str(payload.get("name") or ""),
                    fspec_type=str(payload.get("fspec_type", "text")),
                    nullable=bool(payload.get("nullable", True)),
                    default=payload.get("default"),
                )
                try:
                    adapter.add_attribute(entity_name, spec)
                except ValueError as exc:
                    raise HttpError(400, str(exc)) from exc
                if payload.get("create_permissions"):
                    adapter.ensure_permission(f"{entity_name}:read_{spec.name}")
                    adapter.ensure_permission(f"{entity_name}:write_{spec.name}")
                sa_rec = self._ensure_entity_server_action(adapter, entity_name)
                return (
                    201,
                    {
                        "entities": adapter.list_domain_entities(),
                        "server_action": asdict(sa_rec),
                    },
                    None,
                )

            if path.startswith("/api/entities/") and path.endswith("/foreign-keys") and method == "POST":
                entity_name = unquote(path[len("/api/entities/") : -len("/foreign-keys")])
                target_entity = str(payload.get("target_entity") or "").strip()
                if not target_entity:
                    raise HttpError(400, "Missing 'target_entity'.")
                col_name = str(payload.get("column_name") or f"{target_entity}_id").strip()
                spec = AttributeSpec(
                    name=col_name,
                    fspec_type="int",
                    nullable=bool(payload.get("nullable", True)),
                    default=payload.get("default"),
                    fk_table=target_entity,
                )
                try:
                    adapter.add_attribute(entity_name, spec)
                except ValueError as exc:
                    raise HttpError(400, str(exc)) from exc
                sa_rec = self._ensure_entity_server_action(adapter, entity_name)
                return (
                    201,
                    {
                        "entities": adapter.list_domain_entities(),
                        "server_action": asdict(sa_rec),
                    },
                    None,
                )

            # --- RBAC panel endpoints ---
            if path == "/api/rbac" and method == "GET":
                snap = adapter.list_rbac_snapshot()
                return (200, asdict(snap), None)

            if path == "/api/rbac/roles" and method == "POST":
                try:
                    adapter.create_role(str(payload.get("name") or ""))
                except ValueError as exc:
                    raise HttpError(400, str(exc)) from exc
                return (201, asdict(adapter.list_rbac_snapshot()), None)

            if path == "/api/rbac/permissions" and method == "POST":
                try:
                    adapter.ensure_permission(str(payload.get("name") or ""))
                except ValueError as exc:
                    raise HttpError(400, str(exc)) from exc
                return (201, asdict(adapter.list_rbac_snapshot()), None)

            if path == "/api/rbac/assign" and method == "POST":
                role = str(payload.get("role") or "")
                perm = str(payload.get("permission") or "")
                granted = bool(payload.get("granted", True))
                try:
                    adapter.set_role_permission(role, perm, granted)
                except ValueError as exc:
                    raise HttpError(400, str(exc)) from exc

                synced_forms: list[dict[str, Any]] = []
                if ":" in perm:
                    entity_name = perm.split(":", 1)[0]
                    domain_names = {e["name"] for e in adapter.list_domain_entities()}
                    if entity_name in domain_names:
                        schema = introspect_entity(adapter, entity_name)
                        for form_rec in adapter.list_forms():
                            if form_rec.entity_name != entity_name:
                                continue
                            synced_src = sync_fspec_permissions(
                                form_rec.fspec_source, schema
                            )
                            if synced_src != form_rec.fspec_source:
                                updated = adapter.update_form(
                                    form_rec.id, fspec_source=synced_src
                                )
                                synced_forms.append(asdict(updated))

                resp = asdict(adapter.list_rbac_snapshot())
                resp["synced_forms"] = synced_forms
                return (200, resp, None)

            # --- SQL Explorer & Query Panel endpoints ---
            if path == "/api/sql/tables" and method == "GET":
                return (200, {"tables": adapter.list_all_tables_schema()}, None)

            if path == "/api/sql/query" and method == "POST":
                sql_text = str(payload.get("sql") or "")
                try:
                    result = adapter.execute_sql(sql_text)
                except ValueError as exc:
                    raise HttpError(400, str(exc)) from exc
                result["tables"] = adapter.list_all_tables_schema()
                return (200, result, None)

            # --- Forms CRUD & Entity-to-Form generation ---
            if path == "/api/forms/map" and method == "GET":
                return (200, _build_form_map(adapter), None)

            if path == "/api/forms" and method == "GET":
                return (200, {"forms": [asdict(f) for f in adapter.list_forms()]}, None)

            if path == "/api/forms" and method == "POST":
                try:
                    rec = adapter.create_form(
                        name=str(payload.get("name") or ""),
                        label=str(payload.get("label") or ""),
                        fspec_source=str(payload.get("fspec_source") or ""),
                        entity_name=payload.get("entity_name"),
                        description=payload.get("description"),
                        formatter=str(payload.get("formatter") or "html5"),
                        theme=str(payload.get("theme") or "default"),
                        form_type=str(payload.get("form_type") or "detail"),
                    )
                    ca_rec = self._create_form_client_action(adapter, rec)
                except Exception as exc:
                    raise HttpError(400, str(exc)) from exc
                return (
                    201,
                    {"form": asdict(rec), "client_action": asdict(ca_rec)},
                    None,
                )

            if path == "/api/forms/from-entity" and method == "POST":
                entity_name = str(payload.get("entity") or "").strip()
                if not entity_name:
                    raise HttpError(400, "Missing 'entity' name.")
                layout = str(payload.get("layout") or "single")
                form_type = str(payload.get("form_type") or "detail")
                if form_type not in ("detail", "list"):
                    raise HttpError(400, "Invalid form_type; expected 'detail' or 'list'.")
                try:
                    schema = introspect_entity(adapter, entity_name)
                    existing_forms = adapter.list_forms()
                    detail_form_name: str | None = None
                    if form_type == "list":
                        matching_detail = next(
                            (
                                f
                                for f in existing_forms
                                if f.entity_name == entity_name and f.form_type == "detail"
                            ),
                            None,
                        )
                        detail_form_name = (
                            matching_detail.name
                            if matching_detail
                            else f"{entity_name}_form"
                        )
                    default_slug = (
                        f"{entity_name}_list" if form_type == "list" else f"{entity_name}_form"
                    )
                    default_label = (
                        f"{schema.display_name} List"
                        if form_type == "list"
                        else f"{schema.display_name} Form"
                    )
                    form_name = str(payload.get("name") or default_slug).strip()
                    existing_names = {f.name for f in existing_forms}
                    if form_name in existing_names:
                        suffix = 2
                        while f"{form_name}_{suffix}" in existing_names:
                            suffix += 1
                        form_name = f"{form_name}_{suffix}"
                    client_action_name = (
                        f"{form_name}_open" if form_type == "list" else f"{form_name}_submit"
                    )
                    fspec_source = generate_fspec(
                        schema,
                        layout=layout,
                        form_type=form_type,
                        detail_form_name=detail_form_name,
                        client_action_name=client_action_name,
                    )
                    rec = adapter.create_form(
                        name=form_name,
                        label=str(payload.get("label") or default_label),
                        fspec_source=fspec_source,
                        entity_name=entity_name,
                        formatter=str(payload.get("formatter") or "html5"),
                        theme=str(payload.get("theme") or "default"),
                        form_type=form_type,
                    )
                    sa_rec = self._ensure_entity_server_action(
                        adapter, entity_name, sync_existing=False
                    )
                    ca_rec = self._create_form_client_action(
                        adapter, rec, detail_form_name=detail_form_name
                    )
                except Exception as exc:
                    raise HttpError(400, str(exc)) from exc
                resp_body: dict[str, Any] = {
                    "form": asdict(rec),
                    "client_action": asdict(ca_rec),
                    "server_action": asdict(sa_rec),
                }
                return (201, resp_body, None)

            if path.startswith("/api/forms/") and path.endswith("/export") and method == "POST":
                fid_str = path[len("/api/forms/") : -len("/export")]
                rec = adapter.get_form(int(fid_str))
                if rec is None:
                    raise HttpError(404, "Form not found.")
                target = str(payload.get("target") or "both")
                files: list[dict[str, str]] = [
                    {"filename": f"{rec.name}.fspec", "content": rec.fspec_source}
                ]
                if rec.entity_name:
                    schema = introspect_entity(adapter, rec.entity_name)
                    if target in ("python", "both"):
                        files.append(
                            {
                                "filename": f"{rec.entity_name}_action.py",
                                "content": generate_python_server_action(schema),
                            }
                        )
                    if target in ("node", "both"):
                        mod_rec = adapter.get_server_module_by_name(SERVER_MODULE_NAME)
                        files.append(
                            {
                                "filename": f"{SERVER_MODULE_NAME}.mjs",
                                "content": (
                                    mod_rec.code
                                    if mod_rec is not None
                                    else get_node_server_module_code()
                                ),
                            }
                        )
                        files.append(
                            {
                                "filename": f"{rec.entity_name}_action.mjs",
                                "content": generate_node_server_action(schema),
                            }
                        )
                return (200, {"files": files}, None)

            if path.startswith("/api/forms/") and path.endswith("/sync-permissions") and method == "POST":
                fid_str = path[len("/api/forms/") : -len("/sync-permissions")]
                rec = adapter.get_form(int(fid_str))
                if rec is None:
                    raise HttpError(404, "Form not found.")
                if not rec.entity_name:
                    raise HttpError(400, "Form is not linked to a database entity.")
                source = str(payload.get("fspec_source") or rec.fspec_source)
                schema = introspect_entity(adapter, rec.entity_name)
                synced_src = sync_fspec_permissions(source, schema)
                updated = adapter.update_form(rec.id, fspec_source=synced_src)
                return (200, {"form": asdict(updated)}, None)

            if path.startswith("/api/forms/") and path.count("/") == 3:
                fid = int(path.split("/")[-1])
                if method == "PUT":
                    try:
                        rec = adapter.update_form(
                            fid,
                            label=payload.get("label"),
                            description=payload.get("description"),
                            fspec_source=payload.get("fspec_source"),
                            formatter=payload.get("formatter"),
                            theme=payload.get("theme"),
                            form_type=payload.get("form_type"),
                        )
                    except ValueError as exc:
                        raise HttpError(404, str(exc)) from exc
                    return (200, {"form": asdict(rec)}, None)
                if method == "DELETE":
                    adapter.delete_form(fid)
                    return (200, {"deleted": fid}, None)

            # --- Server Actions & Client Actions CRUD ---
            if path == "/api/server-actions" and method == "GET":
                return (
                    200,
                    {"server_actions": [asdict(a) for a in adapter.list_server_actions()]},
                    None,
                )

            if path == "/api/server-actions" and method == "POST":
                try:
                    rec = adapter.save_server_action(
                        name=str(payload.get("name") or ""),
                        label=str(payload.get("label") or ""),
                        code=str(payload.get("code") or ""),
                        entity_name=payload.get("entity_name"),
                        description=payload.get("description"),
                        action_type=str(payload.get("action_type") or "upsert"),
                        target_runtime=str(payload.get("target_runtime") or "node"),
                    )
                except ValueError as exc:
                    raise HttpError(400, str(exc)) from exc
                return (201, {"server_action": asdict(rec)}, None)

            if path.startswith("/api/server-actions/") and path.count("/") == 3:
                aid = int(path.split("/")[-1])
                if method == "PUT":
                    try:
                        rec = adapter.update_server_action(
                            aid,
                            label=payload.get("label"),
                            description=payload.get("description"),
                            code=payload.get("code"),
                            target_runtime=payload.get("target_runtime"),
                        )
                    except ValueError as exc:
                        raise HttpError(404, str(exc)) from exc
                    return (200, {"server_action": asdict(rec)}, None)
                if method == "DELETE":
                    adapter.delete_server_action(aid)
                    return (200, {"deleted": aid}, None)

            if path == "/api/client-actions" and method == "GET":
                return (
                    200,
                    {"client_actions": [asdict(a) for a in adapter.list_client_actions()]},
                    None,
                )

            if path == "/api/client-actions" and method == "POST":
                try:
                    rec = adapter.create_client_action(
                        name=str(payload.get("name") or ""),
                        label=str(payload.get("label") or ""),
                        target_form_name=payload.get("target_form_name"),
                        description=payload.get("description"),
                        action_type=str(payload.get("action_type") or "link"),
                        params_spec=payload.get("params_spec"),
                        code=str(payload.get("code") or ""),
                    )
                except Exception as exc:
                    raise HttpError(400, str(exc)) from exc
                return (201, {"client_action": asdict(rec)}, None)

            if path.startswith("/api/client-actions/") and path.count("/") == 3:
                cid = int(path.split("/")[-1])
                if method == "PUT":
                    try:
                        rec = adapter.update_client_action(
                            cid,
                            label=payload.get("label"),
                            description=payload.get("description"),
                            target_form_name=payload.get("target_form_name"),
                            params_spec=payload.get("params_spec"),
                            code=payload.get("code"),
                        )
                    except ValueError as exc:
                        raise HttpError(404, str(exc)) from exc
                    return (200, {"client_action": asdict(rec)}, None)
                if method == "DELETE":
                    adapter.delete_client_action(cid)
                    return (200, {"deleted": cid}, None)

            # --- Server Modules & Client Modules CRUD ---
            if path == "/api/server-modules" and method == "GET":
                return (
                    200,
                    {"server_modules": [asdict(m) for m in adapter.list_server_modules()]},
                    None,
                )

            if path == "/api/server-modules" and method == "POST":
                try:
                    rec = adapter.save_server_module(
                        name=str(payload.get("name") or ""),
                        label=str(payload.get("label") or ""),
                        code=str(payload.get("code") or ""),
                        description=payload.get("description"),
                        target_runtime=str(payload.get("target_runtime") or "node"),
                    )
                except ValueError as exc:
                    raise HttpError(400, str(exc)) from exc
                return (201, {"server_module": asdict(rec)}, None)

            if path.startswith("/api/server-modules/") and path.count("/") == 3:
                mid = int(path.split("/")[-1])
                if method == "PUT":
                    try:
                        rec = adapter.update_server_module(
                            mid,
                            label=payload.get("label"),
                            description=payload.get("description"),
                            code=payload.get("code"),
                            target_runtime=payload.get("target_runtime"),
                        )
                    except ValueError as exc:
                        raise HttpError(404, str(exc)) from exc
                    return (200, {"server_module": asdict(rec)}, None)
                if method == "DELETE":
                    adapter.delete_server_module(mid)
                    return (200, {"deleted": mid}, None)

            if path == "/api/client-modules" and method == "GET":
                return (
                    200,
                    {"client_modules": [asdict(m) for m in adapter.list_client_modules()]},
                    None,
                )

            if path == "/api/client-modules" and method == "POST":
                try:
                    rec = adapter.save_client_module(
                        name=str(payload.get("name") or ""),
                        label=str(payload.get("label") or ""),
                        code=str(payload.get("code") or ""),
                        description=payload.get("description"),
                    )
                except ValueError as exc:
                    raise HttpError(400, str(exc)) from exc
                return (201, {"client_module": asdict(rec)}, None)

            if path.startswith("/api/client-modules/") and path.count("/") == 3:
                mid = int(path.split("/")[-1])
                if method == "PUT":
                    try:
                        rec = adapter.update_client_module(
                            mid,
                            label=payload.get("label"),
                            description=payload.get("description"),
                            code=payload.get("code"),
                        )
                    except ValueError as exc:
                        raise HttpError(404, str(exc)) from exc
                    return (200, {"client_module": asdict(rec)}, None)
                if method == "DELETE":
                    adapter.delete_client_module(mid)
                    return (200, {"deleted": mid}, None)

            # --- Formatters, Themes & Live Render ---
            if path == "/api/formatters" and method == "GET":
                return (200, {"formatters": self.registry.list_metadata()}, None)

            if path == "/api/themes" and method == "GET":
                return (200, {"themes": self.list_themes()}, None)

            if path == "/api/render" and method == "POST":
                source = str(payload.get("fspec_source") or "")
                formatter_id = str(payload.get("formatter") or "html5")
                role = str(payload.get("role") or "superuser")
                filters = payload.get("filters") if isinstance(payload.get("filters"), dict) else {}
                params = dict(payload.get("params") or {}) if isinstance(payload.get("params"), dict) else {}
                search = payload.get("search")
                record_id = payload.get("record_id")
                entity_name_hint = payload.get("entity_name")
                doc: FormDocument = parse_formspec(source)

                if record_id is not None and "id" not in params:
                    params["id"] = record_id

                options_by_field: dict[str, list[dict[str, Any]]] = {}
                list_rows_by_entity: dict[str, list[dict[str, Any]]] = {}
                for form in doc.forms:
                    for choice in _collect_entity_queries(form):
                        q = choice.entity_query
                        if q and q.target_table:
                            options_by_field[choice.field_id] = adapter.query_entity_options(
                                q.target_table,
                                key_col=q.key_col,
                                display_col=q.display_col,
                            )
                    for list_blk in _collect_list_blocks(form):
                        target = (
                            (list_blk.entity_query.target_table if list_blk.entity_query else None)
                            or list_blk.block_id
                        )
                        if target:
                            order_col = (
                                list_blk.entity_query.order_col
                                if list_blk.entity_query and list_blk.entity_query.order_col
                                else "id"
                            )
                            list_rows_by_entity[target] = adapter.query_entity_records(
                                target,
                                filters=filters,
                                search=search,
                                order_col=order_col,
                            )

                values: dict[str, Any] = dict(payload.get("values") or {})
                for fk, fv in filters.items():
                    if fv not in (None, ""):
                        values[fk] = fv
                if search is not None:
                    values["q"] = search
                effective_id = params.get("id", record_id)
                if effective_id not in (None, ""):
                    target_entity = entity_name_hint or (
                        doc.primary_form.block_id if doc.primary_form else None
                    )
                    if target_entity:
                        loaded = adapter.get_entity_record(target_entity, effective_id)
                        if loaded:
                            values = {**loaded, **values}

                try:
                    formatter = self.registry.get(formatter_id)
                except KeyError as exc:
                    raise HttpError(400, str(exc)) from exc

                ctx = RenderContext(
                    role=role,
                    options_by_field=options_by_field,
                    list_rows_by_entity=list_rows_by_entity,
                    values=values,
                    params=params,
                    diagnostics=doc.diagnostics,
                )
                out = formatter.render(doc, ctx)

                rbac_roles = set(adapter.list_rbac_snapshot().roles)
                for form in doc.forms:
                    rbac_roles.update(collect_declared_roles(form))
                roles_list = ["superuser", *sorted(rbac_roles)]

                client_actions = {
                    ca.name: asdict(ca) for ca in adapter.list_client_actions()
                }
                client_modules = {
                    cm.name: asdict(cm) for cm in adapter.list_client_modules()
                }

                return (
                    200,
                    {
                        "formatter_id": out.formatter_id,
                        "output_kind": out.output_kind,
                        "content": out.content,
                        "diagnostics": [asdict(d) for d in doc.diagnostics],
                        "roles": roles_list,
                        "client_actions": client_actions,
                        "client_modules": client_modules,
                    },
                    None,
                )

            if path == "/api/preview/submit" and method == "POST":
                entity_name = str(payload.get("entity") or "").strip()
                server_action_name = payload.get("server_action")
                role = str(payload.get("role") or "superuser")
                record_id = payload.get("record_id")
                data = payload.get("payload")
                if not isinstance(data, dict) or not data:
                    raise HttpError(400, "Expected non-empty 'payload'.")

                upsert_payload = dict(data)
                if record_id is not None and upsert_payload.get("id") in (None, ""):
                    upsert_payload["id"] = record_id

                sa_rec = None
                if server_action_name:
                    sa_rec = adapter.get_server_action_by_name(str(server_action_name))
                    if sa_rec is None:
                        raise HttpError(
                            404, f"Server action '{server_action_name}' not found."
                        )
                elif entity_name:
                    sa_rec = adapter.get_server_action_by_name(f"{entity_name}_upsert")

                if sa_rec is not None:
                    result = self._execute_stored_server_action(
                        adapter,
                        sa_rec.code,
                        sa_rec.target_runtime,
                        role,
                        auth_user["id"],
                        upsert_payload,
                    )
                    return (200, result, None)

                if not entity_name:
                    raise HttpError(400, "Expected 'entity' or 'server_action'.")

                try:
                    schema = introspect_entity(adapter, entity_name)
                except ValueError as exc:
                    raise HttpError(404, str(exc)) from exc

                effective_id = upsert_payload.pop(schema.pk_column, record_id)
                if effective_id == "":
                    effective_id = None

                col_map = {c.name: c for c in schema.columns}
                for key in upsert_payload:
                    if key not in col_map:
                        raise HttpError(422, f"Unknown attribute '{key}'.")
                    eff = (
                        "write"
                        if role == "superuser"
                        else schema.rbac.effective_role_perm(role, key)
                    )
                    if eff != "write":
                        raise HttpError(
                            403, f"Role '{role}' lacks write permission for '{key}'."
                        )

                keys = list(upsert_payload.keys())
                vals = [
                    (1 if v else 0) if isinstance(v, bool) else v
                    for v in (upsert_payload[k] for k in keys)
                ]
                with adapter.conn:  # type: ignore[attr-defined]
                    if effective_id is None:
                        cols_sql = ", ".join(f'"{k}"' for k in keys)
                        ph = ", ".join("?" for _ in keys)
                        cur = adapter.conn.execute(  # type: ignore[attr-defined]
                            f'INSERT INTO "{schema.table_name}" ({cols_sql}) VALUES ({ph})',
                            vals,
                        )
                        saved_id = cur.lastrowid
                        action = "created"
                    else:
                        sets_sql = ", ".join(f'"{k}" = ?' for k in keys)
                        adapter.conn.execute(  # type: ignore[attr-defined]
                            f'UPDATE "{schema.table_name}" SET {sets_sql} WHERE "{schema.pk_column}" = ?',
                            [*vals, effective_id],
                        )
                        saved_id = effective_id
                        action = "updated"
                return (200, {"status": "ok", "action": action, "id": saved_id}, None)

        raise HttpError(404, f"Unknown endpoint: {method} {path}")


def create_server(app: IDEApp, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    """Create a ThreadingHTTPServer bound to `app`."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

        def _session_cookie(self) -> str | None:
            raw_cookie = self.headers.get("Cookie")
            if not raw_cookie:
                return None
            jar: cookies.SimpleCookie = cookies.SimpleCookie()
            jar.load(raw_cookie)
            morsel = jar.get(SessionManager.COOKIE_NAME)
            return morsel.value if morsel else None

        def _send_bytes(
            self,
            status: int,
            data: bytes,
            content_type: str,
            set_cookie: str | None = None,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            if set_cookie:
                self.send_header("Set-Cookie", set_cookie)
            self.end_headers()
            self.wfile.write(data)

        def _send_json(
            self,
            status: int,
            payload: dict[str, Any],
            set_cookie: str | None = None,
        ) -> None:
            self._send_bytes(
                status,
                json.dumps(payload).encode("utf-8"),
                "application/json; charset=utf-8",
                set_cookie=set_cookie,
            )

        def _handle_request(self, method: str) -> None:
            parsed = urlparse(self.path)
            path = parsed.path

            if path.startswith("/api/package/download/") and method == "GET":
                token = self._session_cookie()
                try:
                    with app.open_adapter() as adp:
                        app._require_auth(adp, token)
                except HttpError as exc:
                    self._send_json(exc.status, {"error": exc.message})
                    return
                bid = path[len("/api/package/download/") :].strip("/")
                bundle = app.packager.get_bundle(bid)
                if bundle is None or not bundle.zip_path.is_file():
                    self._send_json(404, {"error": "Package bundle not found."})
                    return
                data = bundle.zip_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header(
                    "Content-Disposition",
                    f'attachment; filename="{bundle.app_name}-{bundle.dialect}.zip"',
                )
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return

            if path.startswith("/api/"):
                body: dict[str, Any] = {}
                if method in ("POST", "PUT"):
                    length = int(self.headers.get("Content-Length", "0"))
                    raw_body = self.rfile.read(length).decode("utf-8") if length else "{}"
                    try:
                        body = json.loads(raw_body or "{}")
                    except json.JSONDecodeError as exc:
                        self._send_json(400, {"error": f"Invalid JSON: {exc}"})
                        return
                try:
                    status, resp, set_cookie = app.handle_api(
                        method, path, body=body, session_token=self._session_cookie()
                    )
                    self._send_json(status, resp, set_cookie=set_cookie)
                except HttpError as exc:
                    self._send_json(exc.status, {"error": exc.message})
                return

            if method != "GET":
                self._send_json(405, {"error": "Method not allowed"})
                return

            if path.startswith("/themes/") and path.endswith(".css"):
                theme_id = path[len("/themes/") : -4]
                css_text = app.read_theme_css(theme_id)
                if css_text is None:
                    self._send_json(404, {"error": "Theme not found"})
                    return
                self._send_bytes(200, css_text.encode("utf-8"), "text/css; charset=utf-8")
                return

            rel_path = "index.html" if path == "/" else path.lstrip("/")
            asset = (_STATIC_DIR / rel_path).resolve()
            if not str(asset).startswith(str(_STATIC_DIR.resolve())) or not asset.is_file():
                self._send_json(404, {"error": "Asset not found"})
                return
            mime, _ = mimetypes.guess_type(str(asset))
            self._send_bytes(
                200,
                asset.read_bytes(),
                f"{mime or 'application/octet-stream'}; charset=utf-8",
            )

        def do_GET(self) -> None:  # noqa: N802
            self._handle_request("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._handle_request("POST")

        def do_PUT(self) -> None:  # noqa: N802
            self._handle_request("PUT")

        def do_DELETE(self) -> None:  # noqa: N802
            self._handle_request("DELETE")

    return ThreadingHTTPServer((host, port), Handler)
