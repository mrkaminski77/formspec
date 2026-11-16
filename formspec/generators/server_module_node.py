"""Shared Node.js Server Module generator (`formspec_server.mjs`).

Extracts all entity-agnostic RBAC resolution, query/list filtering,
FK option lookup, upsert persistence, HTTP server, and CLI parsing
into a single reusable module stored in `server_module`.
"""

from __future__ import annotations

SERVER_MODULE_NAME = "formspec_server"

_NODE_SERVER_MODULE_CODE = """/**
 * Shared FormSpec Node.js Server Module (`formspec_server.mjs`).
 *
 * Reusable DB, RBAC permission enforcement, CRUD/upsert, and CLI/HTTP helpers
 * imported by generated entity server actions.
 * Requires Node.js >= 22 (`node:sqlite`).
 */

import http from "node:http";
import { DatabaseSync } from "node:sqlite";
import { parseArgs } from "node:util";

export const ACCESS_ORDER = { none: 0, read: 1, write: 2 };

export class ActionError extends Error {
  constructor(message, statusCode = 400) {
    super(message);
    this.statusCode = statusCode;
  }
}

export class PermissionDeniedError extends ActionError {
  constructor(message) {
    super(message, 403);
  }
}

export class ValidationError extends ActionError {
  constructor(message) {
    super(message, 422);
  }
}

export class NotFoundError extends ActionError {
  constructor(message) {
    super(message, 404);
  }
}

export function quoteIdent(ident) {
  return `"${String(ident).replace(/"/g, '""')}"`;
}

export function maxAccess(a, b) {
  return ACCESS_ORDER[a] >= ACCESS_ORDER[b] ? a : b;
}

export function toSqlValue(val) {
  if (typeof val === "boolean") return val ? 1 : 0;
  return val;
}

function parsePerm(permStr, entitySlug, attrSet) {
  const prefix = `${entitySlug}:`;
  if (!permStr.startsWith(prefix)) return null;
  const spec = permStr.slice(prefix.length);
  if (spec in ACCESS_ORDER) {
    return [null, spec];
  }
  for (const level of ["write", "read", "none"]) {
    const lvlPrefix = `${level}_`;
    if (spec.startsWith(lvlPrefix)) {
      const attr = spec.slice(lvlPrefix.length);
      if (attrSet.has(attr)) {
        return [attr, level];
      }
      return null;
    }
  }
  return null;
}

export function resolvePermissions(db, userId, entityConfig) {
  const { rbac, entity_slug: entitySlug, columns } = entityConfig;
  const colNames = columns.map((c) => c.name);
  const attrSet = new Set(colNames);

  const sql = `
    SELECT p.${quoteIdent(rbac.perm_name_col)} AS perm_name
    FROM ${quoteIdent(rbac.user_roles_table)} ur
    JOIN ${quoteIdent(rbac.roles_table)} r ON r.id = ur.role_id
    JOIN ${quoteIdent(rbac.role_permissions_table)} rp ON rp.role_id = r.id
    JOIN ${quoteIdent(rbac.permissions_table)} p ON p.id = rp.permission_id
    WHERE ur.user_id = ?
  `;
  const rows = db.prepare(sql).all(userId);

  let entityPerm = "none";
  const attrOverrides = {};
  for (const row of rows) {
    const parsed = parsePerm(String(row.perm_name), entitySlug, attrSet);
    if (!parsed) continue;
    const [attrName, level] = parsed;
    if (attrName === null) {
      entityPerm = maxAccess(entityPerm, level);
    } else {
      attrOverrides[attrName] = maxAccess(attrOverrides[attrName] ?? "none", level);
    }
  }

  const attributes = {};
  for (const name of colNames) {
    attributes[name] = maxAccess(entityPerm, attrOverrides[name] ?? "none");
  }
  const canSubmit =
    entityPerm === "write" || Object.values(attributes).some((lvl) => lvl === "write");

  return {
    entity: entityPerm,
    attributes,
    can_submit: canSubmit,
  };
}

export function fetchFilteredRecord(db, recordId, attrPerms, entityConfig) {
  const table = entityConfig.table_name;
  const pk = entityConfig.pk_column;
  const readableCols = entityConfig.columns
    .map((c) => c.name)
    .filter((name) => ACCESS_ORDER[attrPerms[name] ?? "none"] >= ACCESS_ORDER.read || name === pk);

  const selectSql = readableCols.map(quoteIdent).join(", ");
  const row = db
    .prepare(`SELECT ${selectSql} FROM ${quoteIdent(table)} WHERE ${quoteIdent(pk)} = ? LIMIT 1`)
    .get(recordId);

  if (!row) {
    throw new NotFoundError(`${entityConfig.entity_slug} record '${recordId}' not found.`);
  }

  const filtered = {};
  for (const [k, v] of Object.entries(row)) {
    if (ACCESS_ORDER[attrPerms[k] ?? "none"] >= ACCESS_ORDER.read) {
      filtered[k] = v;
    }
  }
  return filtered;
}

export function loadForm(db, userId, entityConfig, recordId = null) {
  const perms = resolvePermissions(db, userId, entityConfig);
  const attrPerms = perms.attributes;
  const hasAnyRead =
    perms.entity !== "none" ||
    Object.values(attrPerms).some((lvl) => ACCESS_ORDER[lvl] >= ACCESS_ORDER.read);

  if (!hasAnyRead) {
    throw new PermissionDeniedError(
      `User '${userId}' lacks read permission for '${entityConfig.entity_slug}'.`
    );
  }

  const options = {};
  for (const col of entityConfig.columns) {
    const link = col.entity_link;
    if (!link) continue;
    if (ACCESS_ORDER[attrPerms[col.name] ?? "none"] < ACCESS_ORDER.read) continue;
    const rows = db
      .prepare(
        `SELECT ${quoteIdent(link.key_col)} AS id, ${quoteIdent(link.display_col)} AS name FROM ${quoteIdent(
          link.table
        )} ORDER BY ${quoteIdent(link.display_col)} ASC`
      )
      .all();
    options[col.name] = rows.map((r) => ({ id: r.id, name: r.name }));
  }

  const record = recordId !== null && recordId !== undefined
    ? fetchFilteredRecord(db, recordId, attrPerms, entityConfig)
    : null;

  return {
    entity: entityConfig.entity_slug,
    permissions: perms,
    options,
    record,
  };
}

export function listRecords(
  db,
  userId,
  entityConfig,
  filters = {},
  search = null,
  limit = 100,
  offset = 0
) {
  const perms = resolvePermissions(db, userId, entityConfig);
  const attrPerms = perms.attributes;
  const hasAnyRead =
    perms.entity !== "none" ||
    Object.values(attrPerms).some((lvl) => ACCESS_ORDER[lvl] >= ACCESS_ORDER.read);
  if (!hasAnyRead) {
    throw new PermissionDeniedError(
      `User '${userId}' lacks read permission for '${entityConfig.entity_slug}'.`
    );
  }

  const table = entityConfig.table_name;
  const pk = entityConfig.pk_column;
  const readableCols = entityConfig.columns
    .map((c) => c.name)
    .filter((name) => ACCESS_ORDER[attrPerms[name] ?? "none"] >= ACCESS_ORDER.read);
  const selectCols = Array.from(new Set([pk, ...readableCols]));
  const readableSet = new Set(readableCols);

  const whereClauses = [];
  const params = [];
  for (const [k, v] of Object.entries(filters || {})) {
    if (v === null || v === undefined || v === "") continue;
    if (!readableSet.has(k)) {
      throw new PermissionDeniedError(
        `User '${userId}' cannot filter by unreadable attribute '${k}'.`
      );
    }
    whereClauses.push(`${quoteIdent(k)} = ?`);
    params.push(toSqlValue(v));
  }

  if (search && String(search).trim()) {
    const term = `%${String(search).trim()}%`;
    const textCols = entityConfig.columns
      .filter(
        (c) =>
          readableSet.has(c.name) &&
          ["text", "textarea", "email", "url", "tel"].includes(c.fspec_type)
      )
      .map((c) => c.name);
    if (textCols.length > 0) {
      whereClauses.push(
        `(${textCols.map((c) => `CAST(${quoteIdent(c)} AS TEXT) LIKE ?`).join(" OR ")})`
      );
      for (let i = 0; i < textCols.length; i++) params.push(term);
    }
  }

  const whereSql = whereClauses.length > 0 ? ` WHERE ${whereClauses.join(" AND ")}` : "";
  const selectSql = selectCols.map(quoteIdent).join(", ");
  const rows = db
    .prepare(
      `SELECT ${selectSql} FROM ${quoteIdent(table)}${whereSql} ORDER BY ${quoteIdent(pk)} ASC LIMIT ? OFFSET ?`
    )
    .all(...params, Number(limit), Number(offset));

  const options = {};
  for (const col of entityConfig.columns) {
    const link = col.entity_link;
    if (!link || !readableSet.has(col.name)) continue;
    const optRows = db
      .prepare(
        `SELECT ${quoteIdent(link.key_col)} AS id, ${quoteIdent(link.display_col)} AS name FROM ${quoteIdent(
          link.table
        )} ORDER BY ${quoteIdent(link.display_col)} ASC`
      )
      .all();
    options[col.name] = optRows.map((r) => ({ id: r.id, name: r.name }));
  }

  const records = rows.map((row) => {
    const out = {};
    for (const k of selectCols) out[k] = row[k];
    return out;
  });

  return {
    entity: entityConfig.entity_slug,
    permissions: perms,
    options,
    records,
  };
}

export function submitForm(db, userId, entityConfig, payload, recordId = null) {
  const perms = resolvePermissions(db, userId, entityConfig);
  const attrPerms = perms.attributes;
  if (!perms.can_submit) {
    throw new PermissionDeniedError(
      `User '${userId}' lacks write permission to submit '${entityConfig.entity_slug}'.`
    );
  }

  if (!payload || typeof payload !== "object" || Array.isArray(payload) || Object.keys(payload).length === 0) {
    throw new ValidationError("Submission payload must be a non-empty JSON object.");
  }

  const colMap = new Map(entityConfig.columns.map((c) => [c.name, c]));
  for (const key of Object.keys(payload)) {
    if (!colMap.has(key)) {
      throw new ValidationError(`Unknown attribute '${key}'.`);
    }
    if ((attrPerms[key] ?? "none") !== "write") {
      throw new PermissionDeniedError(
        `User '${userId}' lacks write permission for attribute '${key}'.`
      );
    }
  }

  if (recordId === null || recordId === undefined) {
    for (const col of entityConfig.columns) {
      if (col.is_pk || !col.required || col.has_default) continue;
      const val = payload[col.name];
      if (val === undefined || val === null || val === "") {
        if ((attrPerms[col.name] ?? "none") !== "write") {
          throw new PermissionDeniedError(
            `Missing required attribute '${col.name}' which user cannot write.`
          );
        }
        throw new ValidationError(`Missing required attribute '${col.name}'.`);
      }
    }
  }

  for (const [key, val] of Object.entries(payload)) {
    const col = colMap.get(key);
    if (col.required && (val === null || val === "")) {
      throw new ValidationError(`Attribute '${key}' cannot be empty.`);
    }
    if (col.entity_link && val !== null && val !== undefined) {
      const found = db
        .prepare(
          `SELECT 1 FROM ${quoteIdent(col.entity_link.table)} WHERE ${quoteIdent(
            col.entity_link.key_col
          )} = ? LIMIT 1`
        )
        .get(toSqlValue(val));
      if (!found) {
        throw new ValidationError(
          `Invalid reference '${val}' for '${key}' in '${col.entity_link.table}'.`
        );
      }
    }
  }

  const table = entityConfig.table_name;
  const pk = entityConfig.pk_column;
  const keys = Object.keys(payload);
  const values = keys.map((k) => toSqlValue(payload[k]));

  let savedId;
  let action;
  db.exec("BEGIN");
  try {
    if (recordId === null || recordId === undefined) {
      const colsSql = keys.map(quoteIdent).join(", ");
      const placeholders = keys.map(() => "?").join(", ");
      const info = db
        .prepare(`INSERT INTO ${quoteIdent(table)} (${colsSql}) VALUES (${placeholders})`)
        .run(...values);
      savedId = payload[pk] ?? Number(info.lastInsertRowid);
      action = "created";
    } else {
      const exists = db
        .prepare(`SELECT 1 FROM ${quoteIdent(table)} WHERE ${quoteIdent(pk)} = ? LIMIT 1`)
        .get(recordId);
      if (!exists) {
        throw new NotFoundError(`${entityConfig.entity_slug} record '${recordId}' not found.`);
      }
      const setsSql = keys.map((k) => `${quoteIdent(k)} = ?`).join(", ");
      db.prepare(`UPDATE ${quoteIdent(table)} SET ${setsSql} WHERE ${quoteIdent(pk)} = ?`).run(
        ...values,
        recordId
      );
      savedId = recordId;
      action = "updated";
    }
    db.exec("COMMIT");
  } catch (err) {
    db.exec("ROLLBACK");
    throw err;
  }

  const savedRecord = fetchFilteredRecord(db, savedId, attrPerms, entityConfig);
  return {
    status: "ok",
    action,
    id: savedId,
    record: savedRecord,
  };
}

export function upsertRecord(db, userId, entityConfig, payload) {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new ValidationError("Upsert payload must be a JSON object.");
  }
  const pk = entityConfig.pk_column;
  const rawId = payload[pk];
  const recordId =
    rawId === undefined || rawId === null || rawId === "" ? null : rawId;
  const cleanPayload = { ...payload };
  delete cleanPayload[pk];
  return submitForm(db, userId, entityConfig, cleanPayload, recordId);
}

export function openDb(dbPath) {
  const path = dbPath.startsWith("sqlite:///") ? dbPath.slice("sqlite:///".length) : dbPath;
  return new DatabaseSync(path);
}

export function serveEntityAction(entityConfig, dbPath, host = "127.0.0.1", port = 8080, handlers = null) {
  const route = `/${entityConfig.entity_slug}`;
  const doLoad = handlers?.loadForm ?? ((db, uid, rid) => loadForm(db, uid, entityConfig, rid));
  const doList = handlers?.listRecords ?? ((db, uid, f, q) => listRecords(db, uid, entityConfig, f, q));
  const doSubmit = handlers?.submitForm ?? ((db, uid, p, rid) => submitForm(db, uid, entityConfig, p, rid));
  const doUpsert = handlers?.upsertRecord ?? ((db, uid, p) => upsertRecord(db, uid, entityConfig, p));

  const server = http.createServer((req, res) => {
    const sendJson = (code, body) => {
      const raw = Buffer.from(JSON.stringify(body), "utf8");
      res.writeHead(code, {
        "Content-Type": "application/json",
        "Content-Length": raw.length,
      });
      res.end(raw);
    };

    const url = new URL(req.url, `http://${req.headers.host || "localhost"}`);
    if (url.pathname !== route) {
      sendJson(404, { error: "Not found" });
      return;
    }

    if (req.method === "GET") {
      const userId = url.searchParams.get("user_id");
      const recordId = url.searchParams.get("record_id");
      const action = url.searchParams.get("action") || "load";
      if (!userId) {
        sendJson(400, { error: "Missing user_id" });
        return;
      }
      const db = openDb(dbPath);
      try {
        if (action === "list") {
          const filters = JSON.parse(url.searchParams.get("filters") || "{}");
          const search = url.searchParams.get("q");
          sendJson(200, doList(db, userId, filters, search));
        } else {
          sendJson(200, doLoad(db, userId, recordId));
        }
      } catch (err) {
        sendJson(err.statusCode || 500, { error: err.message });
      } finally {
        db.close();
      }
      return;
    }

    if (req.method === "POST") {
      const chunks = [];
      req.on("data", (c) => chunks.push(c));
      req.on("end", () => {
        const body = JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}");
        if (body.user_id === undefined || body.user_id === null) {
          sendJson(400, { error: "Missing user_id" });
          return;
        }
        const db = openDb(dbPath);
        try {
          const result =
            body.action === "upsert"
              ? doUpsert(db, body.user_id, body.payload ?? {})
              : doSubmit(db, body.user_id, body.payload ?? {}, body.record_id ?? null);
          sendJson(200, result);
        } catch (err) {
          sendJson(err.statusCode || 500, { error: err.message });
        } finally {
          db.close();
        }
      });
      return;
    }

    sendJson(405, { error: "Method not allowed" });
  });
  server.listen(port, host);
  return server;
}

export function runEntityCli(entityConfig, handlers = null) {
  const { values } = parseArgs({
    options: {
      db: { type: "string" },
      "user-id": { type: "string" },
      action: { type: "string", default: "load" },
      "record-id": { type: "string" },
      payload: { type: "string", default: "{}" },
      filters: { type: "string", default: "{}" },
      search: { type: "string" },
      serve: { type: "boolean", default: false },
      host: { type: "string", default: "127.0.0.1" },
      port: { type: "string", default: "8080" },
    },
  });

  if (!values.db) {
    console.error(JSON.stringify({ error: "Missing --db argument" }));
    process.exit(2);
  }

  if (values.serve) {
    serveEntityAction(entityConfig, values.db, values.host, Number(values.port), handlers);
    return;
  }

  if (!values["user-id"]) {
    console.error(JSON.stringify({ error: "Missing --user-id argument" }));
    process.exit(2);
  }

  const doLoad = handlers?.loadForm ?? ((db, uid, rid) => loadForm(db, uid, entityConfig, rid));
  const doList = handlers?.listRecords ?? ((db, uid, f, q) => listRecords(db, uid, entityConfig, f, q));
  const doSubmit = handlers?.submitForm ?? ((db, uid, p, rid) => submitForm(db, uid, entityConfig, p, rid));
  const doUpsert = handlers?.upsertRecord ?? ((db, uid, p) => upsertRecord(db, uid, entityConfig, p));

  const db = openDb(values.db);
  try {
    const recordId = values["record-id"] ?? null;
    let result;
    if (values.action === "load") {
      result = doLoad(db, values["user-id"], recordId);
    } else if (values.action === "list") {
      result = doList(
        db,
        values["user-id"],
        JSON.parse(values.filters),
        values.search ?? null
      );
    } else if (values.action === "upsert") {
      result = doUpsert(db, values["user-id"], JSON.parse(values.payload));
    } else {
      result = doSubmit(db, values["user-id"], JSON.parse(values.payload), recordId);
    }
    console.log(JSON.stringify(result));
  } catch (err) {
    console.log(
      JSON.stringify({ error: err.message, status_code: err.statusCode ?? 500 })
    );
    process.exit(1);
  } finally {
    db.close();
  }
}
"""


def get_node_server_module_code() -> str:
    """Return the shared Node.js server module source (`formspec_server.mjs`)."""
    return _NODE_SERVER_MODULE_CODE
