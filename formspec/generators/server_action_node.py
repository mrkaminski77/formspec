"""Node.js server action code generator (`--target node`)."""

from __future__ import annotations

import json
import re
from string import Template

from formspec.db.base import EntitySchema
from formspec.generators.common import build_entity_config

_NODE_ACTION_TEMPLATE = Template('''#!/usr/bin/env node
/**
 * Auto-generated FormSpec server action for entity `$entity_slug`.
 * Shared DB, RBAC, and upsert helpers live in Server Module `./formspec_server.mjs`.
 */

import { pathToFileURL } from "node:url";
import {
  ActionError,
  NotFoundError,
  PermissionDeniedError,
  ValidationError,
  listRecords as sharedListRecords,
  loadForm as sharedLoadForm,
  resolvePermissions as sharedResolvePermissions,
  runEntityCli,
  submitForm as sharedSubmitForm,
  upsertRecord as sharedUpsertRecord,
} from "./formspec_server.mjs";

export { ActionError, NotFoundError, PermissionDeniedError, ValidationError };

export const ENTITY_CONFIG = $config_json;

export function resolvePermissions(db, userId) {
  return sharedResolvePermissions(db, userId, ENTITY_CONFIG);
}

export function loadForm(db, userId, recordId = null) {
  return sharedLoadForm(db, userId, ENTITY_CONFIG, recordId);
}

export function listRecords(
  db,
  userId,
  filters = {},
  search = null,
  limit = 100,
  offset = 0
) {
  return sharedListRecords(db, userId, ENTITY_CONFIG, filters, search, limit, offset);
}

export function submitForm(db, userId, payload, recordId = null) {
  return sharedSubmitForm(db, userId, ENTITY_CONFIG, payload, recordId);
}

export function upsertRecord(db, userId, payload) {
  return sharedUpsertRecord(db, userId, ENTITY_CONFIG, payload);
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  runEntityCli(ENTITY_CONFIG, {
    resolvePermissions,
    loadForm,
    listRecords,
    submitForm,
    upsertRecord,
  });
}
''')

_ENTITY_CONFIG_RE = re.compile(
    r"export const ENTITY_CONFIG\s*=\s*\{.*?\n\};",
    re.DOTALL,
)


def generate_node_server_action(schema: EntitySchema) -> str:
    """Generate a modular Node.js server action importing `./formspec_server.mjs`."""
    config = build_entity_config(schema)
    return _NODE_ACTION_TEMPLATE.substitute(
        entity_slug=schema.entity_slug,
        config_json=json.dumps(config, indent=2),
    )


def sync_entity_config_in_node_action(existing_code: str, schema: EntitySchema) -> str:
    """Refresh `ENTITY_CONFIG` in a stored Node server action while preserving custom hooks."""
    config_block = f"export const ENTITY_CONFIG = {json.dumps(build_entity_config(schema), indent=2)};"
    updated, count = _ENTITY_CONFIG_RE.subn(config_block, existing_code, count=1)
    if count == 0:
        return generate_node_server_action(schema)
    return updated
