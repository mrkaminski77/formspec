"""Shared Client Module (`formspec_client`) and form Client Action generators."""

from __future__ import annotations

import json

from formspec.db.base import EntitySchema

CLIENT_MODULE_NAME = "formspec_client"
HEADER_MODULE_NAME = "formspec_header"

_HEADER_MODULE_CODE = """/**
 * Shared FormSpec Header & Breadcrumbs Client Module (`formspec_header.mjs`).
 *
 * Rendered at the top of every page. Customize `renderAppHeader(ctx)` to change
 * navigation breadcrumbs, branding, or header actions across all forms.
 */

function escapeHtml(text) {
  return String(text ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

export function renderAppHeader({
  formName = "home",
  formLabel = "Home",
  formType = "detail",
  breadcrumbs = [],
  role = "superuser",
  recordId = null,
} = {}) {
  const trail = breadcrumbs.length
    ? breadcrumbs
    : [{ name: formName, label: formLabel || formName, recordId }];

  const itemsHtml = trail
    .map((crumb, idx) => {
      const isLast = idx === trail.length - 1;
      const suffix = crumb.recordId ? ` #${crumb.recordId}` : "";
      const text = `${escapeHtml(crumb.label || crumb.name)}${escapeHtml(suffix)}`;
      if (isLast) {
        return `<span class="fs-crumb is-current" aria-current="page">${text}</span>`;
      }
      return `<a href="#" class="fs-crumb-link" data-breadcrumb-index="${idx}">${text}</a>`;
    })
    .join('<span class="fs-crumb-sep" aria-hidden="true">/</span>');

  return `
<header class="fs-app-header" data-form="${escapeHtml(formName)}" data-form-type="${escapeHtml(formType)}">
  <nav class="fs-breadcrumbs" aria-label="Breadcrumb">${itemsHtml}</nav>
  <span class="fs-header-meta">${escapeHtml(role)}</span>
</header>`.trim();
}
"""

_CLIENT_MODULE_CODE = """/**
 * Shared FormSpec Client Module (`formspec_client.mjs`).
 *
 * Reusable client-side validation, normalization, server-action invocation,
 * and navigation helpers imported by generated client actions.
 */

export class ClientValidationError extends Error {
  constructor(message, field = null) {
    super(message);
    this.name = "ClientValidationError";
    this.field = field;
  }
}

export function isBlank(value) {
  return value === null || value === undefined || String(value).trim() === "";
}

export function requireFields(payload, fieldNames, options = {}) {
  const onlyPresent = Boolean(options.onlyPresent);
  const labels = options.labels || {};
  for (const field of fieldNames || []) {
    if (onlyPresent && !Object.prototype.hasOwnProperty.call(payload || {}, field)) {
      continue;
    }
    if (isBlank(payload?.[field])) {
      const label = labels[field] || field.replace(/_/g, " ");
      throw new ClientValidationError(`${label} is required.`, field);
    }
  }
}

export function validateEmail(value, label = "Email") {
  if (isBlank(value)) return;
  if (!/^[^\\s@]+@[^\\s@]+\\.[^\\s@]+$/.test(String(value).trim())) {
    throw new ClientValidationError(`Invalid email format for ${label}.`);
  }
}

export function normalizePayload(payload) {
  const out = {};
  for (const [k, v] of Object.entries(payload || {})) {
    out[k] = typeof v === "string" ? v.trim() : v;
  }
  return out;
}

export async function invokeServerAction(ctx, actionName, payload) {
  if (!ctx || typeof ctx.serverAction !== "function") {
    throw new Error("Client action runtime context is missing serverAction().");
  }
  return await ctx.serverAction(actionName, payload);
}

export function navigateToForm(ctx, formName, params = {}) {
  if (!ctx || typeof ctx.navigate !== "function") {
    throw new Error("Client action runtime context is missing navigate().");
  }
  return ctx.navigate(formName, params);
}
"""


def get_client_module_code() -> str:
    """Return the shared browser client module (`formspec_client.mjs`)."""
    return _CLIENT_MODULE_CODE


def get_header_client_module_code() -> str:
    """Return the customizable app header & breadcrumbs client module (`formspec_header.mjs`)."""
    return _HEADER_MODULE_CODE


def generate_detail_client_action(
    form_name: str,
    *,
    entity_name: str | None = None,
    schema: EntitySchema | None = None,
    server_action_name: str | None = None,
) -> str:
    """Generate a client action script for a detail form that validates and initiates upsert."""
    sa_name = server_action_name or (f"{entity_name}_upsert" if entity_name else f"{form_name}_upsert")
    req_fields: list[str] = []
    email_fields: list[str] = []
    if schema is not None:
        for col in schema.columns:
            if col.is_pk:
                continue
            if not col.nullable and col.default is None:
                req_fields.append(col.name)
            if col.fspec_type == "email":
                email_fields.append(col.name)

    return f'''/**
 * Client action for detail form `{form_name}`.
 * Perform client-side validation or checks here before initiating `{sa_name}`.
 */

import {{
  invokeServerAction,
  normalizePayload,
  requireFields,
  validateEmail,
}} from "./{CLIENT_MODULE_NAME}.mjs";

export async function run(ctx) {{
  const payload = normalizePayload(ctx.payload || {{}});

  // Client-side validation checks
  requireFields(payload, {json.dumps(req_fields)}, {{ onlyPresent: true }});
  for (const field of {json.dumps(email_fields)}) {{
    if (payload[field]) validateEmail(payload[field], field);
  }}

  // Initiate server action
  return await invokeServerAction(ctx, {json.dumps(sa_name)}, payload);
}}
'''


def generate_list_client_action(
    form_name: str,
    *,
    target_form_name: str | None = None,
    entity_name: str | None = None,
) -> str:
    """Generate a client action script for a list form (row selection validation & navigation)."""
    detail_target = target_form_name or (f"{entity_name}_form" if entity_name else f"{form_name}_detail")
    return f'''/**
 * Client action for list form `{form_name}`.
 * Validates selection or checks before opening `{detail_target}`.
 */

import {{
  ClientValidationError,
  navigateToForm,
}} from "./{CLIENT_MODULE_NAME}.mjs";

export async function run(ctx) {{
  const recordId = ctx.params?.id ?? ctx.selected?.id ?? null;
  if (recordId === null || recordId === undefined || recordId === "") {{
    throw new ClientValidationError("Please select a row first.");
  }}
  return navigateToForm(ctx, {json.dumps(detail_target)}, {{ id: recordId }});
}}
'''
