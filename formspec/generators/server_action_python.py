"""Python server action code generator (`--target python`)."""

from __future__ import annotations

import pprint
from string import Template

from formspec.db.base import EntitySchema
from formspec.generators.common import build_entity_config

_PYTHON_ACTION_TEMPLATE = Template('''#!/usr/bin/env python3
"""Auto-generated FormSpec server action for entity `$entity_slug`.

Enforces live RBAC permissions (`<entity>:read|write` and
`<entity>:read|write_<attribute>`) and populates `_id` options from target entities.
"""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import sqlite3
from typing import Any
from urllib.parse import parse_qs, urlparse

ENTITY_CONFIG: dict[str, Any] = $config_json

ACCESS_ORDER: dict[str, int] = {"none": 0, "read": 1, "write": 2}


class ActionError(Exception):
    """Base error with HTTP status code for server action handlers."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class PermissionDeniedError(ActionError):
    def __init__(self, message: str) -> None:
        super().__init__(message, status_code=403)


class ValidationError(ActionError):
    def __init__(self, message: str) -> None:
        super().__init__(message, status_code=422)


class NotFoundError(ActionError):
    def __init__(self, message: str) -> None:
        super().__init__(message, status_code=404)


def _q(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def _max_access(a: str, b: str) -> str:
    return a if ACCESS_ORDER[a] >= ACCESS_ORDER[b] else b


def _parse_perm(perm_str: str, entity_slug: str, attr_names: set[str]) -> tuple[str | None, str] | None:
    prefix = f"{entity_slug}:"
    if not perm_str.startswith(prefix):
        return None
    spec = perm_str[len(prefix):]
    if spec in ACCESS_ORDER:
        return (None, spec)
    for level in ("write", "read", "none"):
        lvl_prefix = f"{level}_"
        if spec.startswith(lvl_prefix):
            attr = spec[len(lvl_prefix):]
            if attr in attr_names:
                return (attr, level)
            return None
    return None


def resolve_permissions(conn: sqlite3.Connection, user_id: Any) -> dict[str, Any]:
    """Resolve live entity and per-attribute permissions for `user_id`."""
    rbac = ENTITY_CONFIG["rbac"]
    entity_slug = ENTITY_CONFIG["entity_slug"]
    col_names = [col["name"] for col in ENTITY_CONFIG["columns"]]
    attr_set = set(col_names)

    query = (
        f"SELECT p.{_q(rbac['perm_name_col'])} AS perm_name "
        f"FROM {_q(rbac['user_roles_table'])} ur "
        f"JOIN {_q(rbac['roles_table'])} r ON r.id = ur.role_id "
        f"JOIN {_q(rbac['role_permissions_table'])} rp ON rp.role_id = r.id "
        f"JOIN {_q(rbac['permissions_table'])} p ON p.id = rp.permission_id "
        f"WHERE ur.user_id = ?"
    )
    rows = conn.execute(query, (user_id,)).fetchall()

    entity_perm = "none"
    attr_overrides: dict[str, str] = {}
    for row in rows:
        perm_name = row["perm_name"] if isinstance(row, sqlite3.Row) else row[0]
        parsed = _parse_perm(str(perm_name), entity_slug, attr_set)
        if parsed is None:
            continue
        attr_name, level = parsed
        if attr_name is None:
            entity_perm = _max_access(entity_perm, level)
        else:
            prev = attr_overrides.get(attr_name, "none")
            attr_overrides[attr_name] = _max_access(prev, level)

    attributes = {
        name: _max_access(entity_perm, attr_overrides.get(name, "none"))
        for name in col_names
    }
    can_submit = entity_perm == "write" or any(
        lvl == "write" for lvl in attributes.values()
    )
    return {
        "entity": entity_perm,
        "attributes": attributes,
        "can_submit": can_submit,
    }


def _fetch_filtered_record(
    conn: sqlite3.Connection,
    record_id: Any,
    attr_perms: dict[str, str],
) -> dict[str, Any]:
    table = ENTITY_CONFIG["table_name"]
    pk = ENTITY_CONFIG["pk_column"]
    readable_cols = [
        col["name"]
        for col in ENTITY_CONFIG["columns"]
        if ACCESS_ORDER[attr_perms.get(col["name"], "none")] >= ACCESS_ORDER["read"]
        or col["name"] == pk
    ]
    select_sql = ", ".join(_q(c) for c in readable_cols)
    row = conn.execute(
        f"SELECT {select_sql} FROM {_q(table)} WHERE {_q(pk)} = ? LIMIT 1",
        (record_id,),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"{ENTITY_CONFIG['entity_slug']} record '{record_id}' not found.")

    row_dict = dict(zip(readable_cols, row)) if not isinstance(row, sqlite3.Row) else dict(row)
    return {
        k: v
        for k, v in row_dict.items()
        if ACCESS_ORDER[attr_perms.get(k, "none")] >= ACCESS_ORDER["read"]
    }


def load_form(
    conn: sqlite3.Connection,
    user_id: Any,
    record_id: Any = None,
) -> dict[str, Any]:
    """Load effective permissions, `_id` entity options, and optional record data."""
    perms = resolve_permissions(conn, user_id)
    attr_perms = perms["attributes"]
    has_any_read = perms["entity"] != "none" or any(
        ACCESS_ORDER[lvl] >= ACCESS_ORDER["read"] for lvl in attr_perms.values()
    )
    if not has_any_read:
        raise PermissionDeniedError(
            f"User '{user_id}' lacks read permission for '{ENTITY_CONFIG['entity_slug']}'."
        )

    options: dict[str, list[dict[str, Any]]] = {}
    for col in ENTITY_CONFIG["columns"]:
        link = col.get("entity_link")
        if link is None:
            continue
        if ACCESS_ORDER[attr_perms.get(col["name"], "none")] < ACCESS_ORDER["read"]:
            continue
        opt_rows = conn.execute(
            f"SELECT {_q(link['key_col'])} AS id, {_q(link['display_col'])} AS name "
            f"FROM {_q(link['table'])} ORDER BY {_q(link['display_col'])} ASC"
        ).fetchall()
        options[col["name"]] = [
            {"id": r[0], "name": r[1]} for r in opt_rows
        ]

    record = None
    if record_id is not None:
        record = _fetch_filtered_record(conn, record_id, attr_perms)

    return {
        "entity": ENTITY_CONFIG["entity_slug"],
        "permissions": perms,
        "options": options,
        "record": record,
    }


def list_records(
    conn: sqlite3.Connection,
    user_id: Any,
    filters: dict[str, Any] | None = None,
    search: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """List optionally filtered records, redacting attributes lacking read permission."""
    perms = resolve_permissions(conn, user_id)
    attr_perms = perms["attributes"]
    has_any_read = perms["entity"] != "none" or any(
        ACCESS_ORDER[lvl] >= ACCESS_ORDER["read"] for lvl in attr_perms.values()
    )
    if not has_any_read:
        raise PermissionDeniedError(
            f"User '{user_id}' lacks read permission for '{ENTITY_CONFIG['entity_slug']}'."
        )

    table = ENTITY_CONFIG["table_name"]
    pk = ENTITY_CONFIG["pk_column"]
    readable_cols = [
        col["name"]
        for col in ENTITY_CONFIG["columns"]
        if ACCESS_ORDER[attr_perms.get(col["name"], "none")] >= ACCESS_ORDER["read"]
    ]
    select_cols = list(dict.fromkeys([pk, *readable_cols]))
    readable_set = set(readable_cols)

    where_clauses: list[str] = []
    params: list[Any] = []
    for k, v in (filters or {}).items():
        if v in (None, ""):
            continue
        if k not in readable_set:
            raise PermissionDeniedError(
                f"User '{user_id}' cannot filter by unreadable attribute '{k}'."
            )
        where_clauses.append(f"{_q(k)} = ?")
        params.append(v)

    if search and str(search).strip():
        term = f"%{str(search).strip()}%"
        text_cols = [
            col["name"]
            for col in ENTITY_CONFIG["columns"]
            if col["name"] in readable_set
            and col["fspec_type"] in ("text", "textarea", "email", "url", "tel")
        ]
        if text_cols:
            like_sql = " OR ".join(f"CAST({_q(c)} AS TEXT) LIKE ?" for c in text_cols)
            where_clauses.append(f"({like_sql})")
            params.extend([term] * len(text_cols))

    where_sql = f" WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
    select_sql = ", ".join(_q(c) for c in select_cols)
    rows = conn.execute(
        f"SELECT {select_sql} FROM {_q(table)}{where_sql} ORDER BY {_q(pk)} ASC LIMIT ? OFFSET ?",
        (*params, int(limit), int(offset)),
    ).fetchall()

    options: dict[str, list[dict[str, Any]]] = {}
    for col in ENTITY_CONFIG["columns"]:
        link = col.get("entity_link")
        if link is not None and col["name"] in readable_set:
            opt_rows = conn.execute(
                f"SELECT {_q(link['key_col'])} AS id, {_q(link['display_col'])} AS name "
                f"FROM {_q(link['table'])} ORDER BY {_q(link['display_col'])} ASC"
            ).fetchall()
            options[col["name"]] = [{"id": r[0], "name": r[1]} for r in opt_rows]

    records: list[dict[str, Any]] = []
    for row in rows:
        row_dict = dict(zip(select_cols, row)) if not isinstance(row, sqlite3.Row) else dict(row)
        records.append({k: row_dict[k] for k in select_cols})

    return {
        "entity": ENTITY_CONFIG["entity_slug"],
        "permissions": perms,
        "options": options,
        "records": records,
    }


def submit_form(
    conn: sqlite3.Connection,
    user_id: Any,
    payload: dict[str, Any],
    record_id: Any = None,
) -> dict[str, Any]:
    """Validate permissions, required fields, and `_id` links, then persist."""
    perms = resolve_permissions(conn, user_id)
    attr_perms = perms["attributes"]
    if not perms["can_submit"]:
        raise PermissionDeniedError(
            f"User '{user_id}' lacks write permission to submit '{ENTITY_CONFIG['entity_slug']}'."
        )

    if not isinstance(payload, dict) or not payload:
        raise ValidationError("Submission payload must be a non-empty JSON object.")

    col_map = {col["name"]: col for col in ENTITY_CONFIG["columns"]}
    for key in payload:
        if key not in col_map:
            raise ValidationError(f"Unknown attribute '{key}'.")
        if attr_perms.get(key, "none") != "write":
            raise PermissionDeniedError(
                f"User '{user_id}' lacks write permission for attribute '{key}'."
            )

    if record_id is None:
        for col in ENTITY_CONFIG["columns"]:
            if col["is_pk"] or not col["required"] or col["has_default"]:
                continue
            val = payload.get(col["name"])
            if val is None or val == "":
                if attr_perms.get(col["name"], "none") != "write":
                    raise PermissionDeniedError(
                        f"Missing required attribute '{col['name']}' which user cannot write."
                    )
                raise ValidationError(f"Missing required attribute '{col['name']}'.")

    for key, val in payload.items():
        col = col_map[key]
        if col["required"] and val in (None, ""):
            raise ValidationError(f"Attribute '{key}' cannot be empty.")
        link = col.get("entity_link")
        if link is not None and val is not None:
            found = conn.execute(
                f"SELECT 1 FROM {_q(link['table'])} WHERE {_q(link['key_col'])} = ? LIMIT 1",
                (val,),
            ).fetchone()
            if found is None:
                raise ValidationError(
                    f"Invalid reference '{val}' for '{key}' in '{link['table']}'."
                )

    table = ENTITY_CONFIG["table_name"]
    pk = ENTITY_CONFIG["pk_column"]
    keys = list(payload.keys())
    values = [payload[k] for k in keys]

    with conn:
        if record_id is None:
            cols_sql = ", ".join(_q(k) for k in keys)
            placeholders = ", ".join("?" for _ in keys)
            cur = conn.execute(
                f"INSERT INTO {_q(table)} ({cols_sql}) VALUES ({placeholders})",
                values,
            )
            saved_id = payload.get(pk, cur.lastrowid)
            action = "created"
        else:
            exists = conn.execute(
                f"SELECT 1 FROM {_q(table)} WHERE {_q(pk)} = ? LIMIT 1",
                (record_id,),
            ).fetchone()
            if exists is None:
                raise NotFoundError(f"{ENTITY_CONFIG['entity_slug']} record '{record_id}' not found.")
            sets_sql = ", ".join(f"{_q(k)} = ?" for k in keys)
            conn.execute(
                f"UPDATE {_q(table)} SET {sets_sql} WHERE {_q(pk)} = ?",
                values + [record_id],
            )
            saved_id = record_id
            action = "updated"

    saved_record = _fetch_filtered_record(conn, saved_id, attr_perms)
    return {
        "status": "ok",
        "action": action,
        "id": saved_id,
        "record": saved_record,
    }


def upsert_record(
    conn: sqlite3.Connection,
    user_id: Any,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Insert when `pk` is null/empty or update when `pk` is present, enforcing RBAC."""
    if not isinstance(payload, dict):
        raise ValidationError("Upsert payload must be a JSON object.")
    pk = ENTITY_CONFIG["pk_column"]
    raw_id = payload.get(pk)
    record_id = None if raw_id in (None, "") else raw_id
    clean_payload = {k: v for k, v in payload.items() if k != pk}
    return submit_form(conn, user_id, clean_payload, record_id=record_id)


def _open_db(db_path: str) -> sqlite3.Connection:
    path = db_path[len("sqlite:///"):] if db_path.startswith("sqlite:///") else db_path
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def serve(db_path: str, host: str = "127.0.0.1", port: int = 8080) -> None:
    """Run a lightweight JSON HTTP server exposing GET/POST for this entity."""
    route = f"/{ENTITY_CONFIG['entity_slug']}"

    class Handler(BaseHTTPRequestHandler):
        def _send_json(self, code: int, body: dict[str, Any]) -> None:
            raw = json.dumps(body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path != route:
                self._send_json(404, {"error": "Not found"})
                return
            qs = parse_qs(parsed.query)
            user_id = qs.get("user_id", [None])[0]
            record_id = qs.get("record_id", [None])[0]
            action = qs.get("action", ["load"])[0]
            if user_id is None:
                self._send_json(400, {"error": "Missing user_id"})
                return
            conn = _open_db(db_path)
            try:
                if action == "list":
                    filters = json.loads(qs.get("filters", ["{}"])[0])
                    search = qs.get("q", [None])[0]
                    self._send_json(200, list_records(conn, user_id, filters=filters, search=search))
                else:
                    self._send_json(200, load_form(conn, user_id, record_id))
            except ActionError as exc:
                self._send_json(exc.status_code, {"error": exc.message})
            finally:
                conn.close()

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path != route:
                self._send_json(404, {"error": "Not found"})
                return
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            user_id = body.get("user_id")
            if user_id is None:
                self._send_json(400, {"error": "Missing user_id"})
                return
            conn = _open_db(db_path)
            try:
                if body.get("action") == "upsert":
                    result = upsert_record(conn, user_id, body.get("payload", {}))
                else:
                    result = submit_form(
                        conn,
                        user_id,
                        body.get("payload", {}),
                        body.get("record_id"),
                    )
                self._send_json(200, result)
            except ActionError as exc:
                self._send_json(exc.status_code, {"error": exc.message})
            finally:
                conn.close()

    server = ThreadingHTTPServer((host, port), Handler)
    server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description=f"Server action for {ENTITY_CONFIG['entity_slug']}")
    parser.add_argument("--db", required=True, help="SQLite database path or sqlite:/// URI")
    parser.add_argument("--user-id", help="Acting user ID")
    parser.add_argument("--action", choices=["load", "list", "submit", "upsert"], default="load")
    parser.add_argument("--record-id", default=None)
    parser.add_argument("--payload", default="{}")
    parser.add_argument("--filters", default="{}")
    parser.add_argument("--search", default=None)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    if args.serve:
        serve(args.db, args.host, args.port)
        return

    if args.user_id is None:
        parser.error("--user-id is required when not running with --serve")

    conn = _open_db(args.db)
    try:
        if args.action == "load":
            result = load_form(conn, args.user_id, args.record_id)
        elif args.action == "list":
            filters = json.loads(args.filters)
            result = list_records(conn, args.user_id, filters=filters, search=args.search)
        elif args.action == "upsert":
            payload = json.loads(args.payload)
            result = upsert_record(conn, args.user_id, payload)
        else:
            payload = json.loads(args.payload)
            result = submit_form(conn, args.user_id, payload, args.record_id)
        print(json.dumps(result))
    except ActionError as exc:
        print(json.dumps({"error": exc.message, "status_code": exc.status_code}))
        raise SystemExit(1) from exc
    finally:
        conn.close()


if __name__ == "__main__":
    main()
''')


def generate_python_server_action(schema: EntitySchema) -> str:
    """Generate a standalone Python server-action script for `schema`."""
    config = build_entity_config(schema)
    return _PYTHON_ACTION_TEMPLATE.substitute(
        entity_slug=schema.entity_slug,
        config_json=pprint.pformat(config, indent=2, sort_dicts=False),
    )
