"""Standalone packaged web application runtime HTTP server (distinct from IDE)."""

from __future__ import annotations

from dataclasses import asdict
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any, Mapping
from urllib.parse import urlparse

from formspec.db import get_adapter
from formspec.db.base import WorkspaceAdapter
from formspec.formatters import FormatterRegistry, RenderContext, create_default_registry
from formspec.generators.server_module_node import (
    SERVER_MODULE_NAME,
    get_node_server_module_code,
)
from formspec.ide.auth import SessionManager
from formspec.parser import AstNode, BlockNode, ChoiceNode, FormDocument, RowNode, parse_formspec
from formspec.runtime.auth import AuthProvider, AuthUser, get_auth_provider
from formspec.runtime.db_bootstrap import (
    ensure_runtime_database_initialized,
    resolve_runtime_dsn,
)

_STATIC_DIR = Path(__file__).parent / "static"
_BUILTIN_THEMES_DIR = Path(__file__).resolve().parents[1] / "ide" / "themes"


class RuntimeHttpError(Exception):
    """Structured HTTP error returned as JSON."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _collect_entity_queries(node: AstNode) -> list[ChoiceNode]:
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
    found: list[BlockNode] = []
    if isinstance(node, BlockNode):
        if node.kind == "list":
            found.append(node)
        for child in node.children:
            found.extend(_collect_list_blocks(child))
    return found


class AppRuntime:
    """Controller for the packaged FormSpec end-user web application."""

    def __init__(
        self,
        dsn: str | None = None,
        *,
        init_sql_path: str | Path | None = None,
        app_data_dir: str | Path | None = None,
        auth_provider: str | AuthProvider | None = None,
        formatters_dir: str | Path | None = None,
        styles_dir: str | Path | None = None,
        secret: str | None = None,
    ) -> None:
        self.dsn = resolve_runtime_dsn(dsn)
        self.formatters_dir = formatters_dir or os.environ.get("FORMSPEC_FORMATTERS_DIR")
        self.styles_dir = styles_dir or os.environ.get("FORMSPEC_STYLES_DIR")
        self.registry: FormatterRegistry = create_default_registry(self.formatters_dir)
        self.auth: AuthProvider = (
            auth_provider
            if isinstance(auth_provider, AuthProvider)
            else get_auth_provider(auth_provider, secret=secret)
        )
        self.db_initialized_on_startup = ensure_runtime_database_initialized(
            self.dsn,
            init_sql_path=init_sql_path,
            app_data_dir=app_data_dir,
        )

    def open_adapter(self) -> WorkspaceAdapter:
        adapter = get_adapter(self.dsn)
        if not isinstance(adapter, WorkspaceAdapter):
            adapter.close()
            raise RuntimeError("Configured DatabaseAdapter does not implement WorkspaceAdapter.")
        return adapter

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

    def _execute_server_action(
        self,
        adapter: WorkspaceAdapter,
        sa_code: str,
        runtime: str,
        auth_user: AuthUser,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        db_path = getattr(adapter, "db_path", None)
        if not db_path or db_path == ":memory:":
            raise RuntimeHttpError(400, "Stored script execution requires a file-backed SQLite DB.")

        temp_user_id: int | None = None
        acting_user_id = auth_user.id

        if acting_user_id is None:
            role_name = auth_user.effective_role
            role_row = adapter.conn.execute(  # type: ignore[attr-defined]
                "SELECT id FROM roles WHERE name = ? LIMIT 1", (role_name,)
            ).fetchone()
            if role_row is None:
                raise RuntimeHttpError(403, f"Unknown role '{role_name}'.")
            with adapter.conn:  # type: ignore[attr-defined]
                cur = adapter.conn.execute(  # type: ignore[attr-defined]
                    "INSERT INTO users (username, password_hash, is_superuser) VALUES (?, '', 0)",
                    (f"__runtime_{role_name}_{os.urandom(4).hex()}",),
                )
                temp_user_id = int(cur.lastrowid)
                adapter.conn.execute(  # type: ignore[attr-defined]
                    "INSERT INTO user_roles (user_id, role_id) VALUES (?, ?)",
                    (temp_user_id, int(role_row["id"])),
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
            raise RuntimeHttpError(
                500, f"Server action output error: {proc.stderr or raw_out}"
            ) from exc

        if proc.returncode != 0:
            raise RuntimeHttpError(
                int(parsed.get("status_code") or 500),
                str(parsed.get("error") or proc.stderr or "Server action failed."),
            )
        return parsed

    def handle_api(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        headers: Mapping[str, str] | None = None,
        session_token: str | None = None,
    ) -> tuple[int, dict[str, Any], str | None]:
        payload = body or {}
        req_headers = headers or {}

        with self.open_adapter() as adapter:
            if path == "/auth/login" and method == "POST":
                if not self.auth.supports_login_form:
                    raise RuntimeHttpError(400, f"Login handled externally by {self.auth.id}.")
                try:
                    auth_user, set_cookie = self.auth.login(
                        adapter,
                        str(payload.get("username") or ""),
                        str(payload.get("password") or ""),
                    )
                except ValueError as exc:
                    raise RuntimeHttpError(401, str(exc)) from exc
                return (200, {"user": asdict(auth_user)}, set_cookie)

            if path == "/auth/logout" and method == "POST":
                clear_cookie = f"{SessionManager.COOKIE_NAME}=; HttpOnly; Path=/; Max-Age=0"
                return (200, {"logged_out": True}, clear_cookie)

            auth_user = self.auth.authenticate_request(adapter, req_headers, session_token)

            if path == "/api/app/bootstrap" and method == "GET":
                forms = adapter.list_forms()
                home_form = next((f for f in forms if f.name == "home"), forms[0] if forms else None)
                return (
                    200,
                    {
                        "user": {
                            "id": auth_user.id,
                            "username": auth_user.username,
                            "is_superuser": auth_user.is_superuser,
                            "roles": auth_user.roles,
                            "effective_role": auth_user.effective_role,
                            "authenticated": auth_user.authenticated,
                            "provider": auth_user.provider,
                        },
                        "supports_login": self.auth.supports_login_form,
                        "entry_form": home_form.name if home_form else None,
                        "forms": [
                            {
                                "id": f.id,
                                "name": f.name,
                                "label": f.label,
                                "form_type": f.form_type,
                                "entity_name": f.entity_name,
                                "theme": f.theme,
                                "formatter": f.formatter,
                            }
                            for f in forms
                        ],
                        "client_actions": {
                            ca.name: asdict(ca) for ca in adapter.list_client_actions()
                        },
                        "client_modules": [
                            {"name": cm.name, "label": cm.label, "code": cm.code}
                            for cm in adapter.list_client_modules()
                        ],
                    },
                    None,
                )

            if path == "/api/app/render" and method == "POST":
                form_name = str(payload.get("form_name") or "home").strip()
                form_rec = adapter.get_form_by_name(form_name)
                if form_rec is None:
                    raise RuntimeHttpError(404, f"Form '{form_name}' not found.")

                filters = payload.get("filters") if isinstance(payload.get("filters"), dict) else {}
                params = dict(payload.get("params") or {}) if isinstance(payload.get("params"), dict) else {}
                search = payload.get("search")
                record_id = payload.get("record_id")
                if record_id is not None and "id" not in params:
                    params["id"] = record_id

                doc: FormDocument = parse_formspec(form_rec.fspec_source)
                options_by_field: dict[str, list[dict[str, Any]]] = {}
                list_rows_by_entity: dict[str, list[dict[str, Any]]] = {}
                for form in doc.forms:
                    for choice in _collect_entity_queries(form):
                        q = choice.entity_query
                        if q and q.target_table:
                            options_by_field[choice.field_id] = adapter.query_entity_options(
                                q.target_table, key_col=q.key_col, display_col=q.display_col
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
                                target, filters=filters, search=search, order_col=order_col
                            )

                values: dict[str, Any] = dict(payload.get("values") or {})
                for fk, fv in filters.items():
                    if fv not in (None, ""):
                        values[fk] = fv
                if search is not None:
                    values["q"] = search
                effective_id = params.get("id", record_id)
                if effective_id not in (None, "") and form_rec.entity_name:
                    loaded = adapter.get_entity_record(form_rec.entity_name, effective_id)
                    if loaded:
                        values = {**loaded, **values}

                formatter = self.registry.get(form_rec.formatter or "html5")
                ctx = RenderContext(
                    role=auth_user.effective_role,
                    options_by_field=options_by_field,
                    list_rows_by_entity=list_rows_by_entity,
                    values=values,
                    params=params,
                    diagnostics=doc.diagnostics,
                )
                out = formatter.render(doc, ctx)
                return (
                    200,
                    {
                        "form": {
                            "id": form_rec.id,
                            "name": form_rec.name,
                            "label": form_rec.label,
                            "form_type": form_rec.form_type,
                            "entity_name": form_rec.entity_name,
                            "theme": form_rec.theme,
                        },
                        "content": out.content,
                        "role": auth_user.effective_role,
                    },
                    None,
                )

            if path == "/api/app/submit" and method == "POST":
                sa_name = str(payload.get("server_action") or "").strip()
                entity_name = str(payload.get("entity") or "").strip()
                data = payload.get("payload")
                if not isinstance(data, dict) or not data:
                    raise RuntimeHttpError(400, "Expected non-empty submission payload.")
                upsert_payload = dict(data)
                if payload.get("record_id") is not None and upsert_payload.get("id") in (None, ""):
                    upsert_payload["id"] = payload.get("record_id")

                target_sa = sa_name or (f"{entity_name}_upsert" if entity_name else "")
                sa_rec = adapter.get_server_action_by_name(target_sa)
                if sa_rec is None:
                    raise RuntimeHttpError(404, f"Server action '{target_sa}' not found.")

                result = self._execute_server_action(
                    adapter,
                    sa_rec.code,
                    sa_rec.target_runtime,
                    auth_user,
                    upsert_payload,
                )
                return (200, result, None)

        raise RuntimeHttpError(404, f"Unknown endpoint: {method} {path}")


def create_app_server(
    runtime: AppRuntime,
    host: str = "0.0.0.0",
    port: int = 8080,
) -> ThreadingHTTPServer:
    """Create a ThreadingHTTPServer serving the packaged standalone web app."""

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

        def _handle(self, method: str) -> None:
            path = urlparse(self.path).path
            if path.startswith("/api/") or path.startswith("/auth/"):
                body: dict[str, Any] = {}
                if method in ("POST", "PUT"):
                    length = int(self.headers.get("Content-Length", "0"))
                    raw = self.rfile.read(length).decode("utf-8") if length else "{}"
                    try:
                        body = json.loads(raw or "{}")
                    except json.JSONDecodeError as exc:
                        self._send_json(400, {"error": f"Invalid JSON: {exc}"})
                        return
                try:
                    status, resp, set_cookie = runtime.handle_api(
                        method,
                        path,
                        body=body,
                        headers=dict(self.headers.items()),
                        session_token=self._session_cookie(),
                    )
                    self._send_json(status, resp, set_cookie=set_cookie)
                except RuntimeHttpError as exc:
                    self._send_json(exc.status, {"error": exc.message})
                return

            if method != "GET":
                self._send_json(405, {"error": "Method not allowed"})
                return

            if path.startswith("/themes/") and path.endswith(".css"):
                theme_id = path[len("/themes/") : -4]
                css_text = runtime.read_theme_css(theme_id)
                if css_text is None:
                    self._send_json(404, {"error": "Theme not found"})
                    return
                self._send_bytes(200, css_text.encode("utf-8"), "text/css; charset=utf-8")
                return

            rel = "index.html" if path == "/" else path.lstrip("/")
            asset = (_STATIC_DIR / rel).resolve()
            if not str(asset).startswith(str(_STATIC_DIR.resolve())) or not asset.is_file():
                self._send_json(404, {"error": "Not found"})
                return
            mime, _ = mimetypes.guess_type(str(asset))
            self._send_bytes(
                200, asset.read_bytes(), f"{mime or 'application/octet-stream'}; charset=utf-8"
            )

        def do_GET(self) -> None:  # noqa: N802
            self._handle("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._handle("POST")

    return ThreadingHTTPServer((host, port), Handler)
