const state = {
  auth: null,
  entities: [],
  forms: [],
  serverModules: [],
  serverActions: [],
  clientModules: [],
  clientActions: [],
  activeItem: null, // { kind: "form" | "server_module" | "server_action" | "client_module" | "client_action", id: number }
  activeFormId: null,
  dirty: false,
  formatters: [],
  themes: [],
  roles: ["superuser", "admin", "guest", "anon"],
  selectedFormatter: "html5",
  selectedTheme: "default",
  selectedRole: "superuser",
  previewRecordId: null,
  previewParams: {},
  previewFilters: {},
  previewSearch: "",
  breadcrumbs: [],
  git: { configured: false, cloned: false, current_branch: null, branches: [] },
};

const $ = (id) => document.getElementById(id);

async function api(path, options = {}) {
  const res = await fetch(path, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(data.error || `Request failed (${res.status})`);
  }
  return data;
}

function escapeHtml(text) {
  return String(text)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function openModal(title, htmlContent, opts = {}) {
  $("modal-title").textContent = title;
  $("modal-body").innerHTML = htmlContent;
  $("modal-card").classList.toggle("modal-card-wide", Boolean(opts.wide));
  $("modal-backdrop").classList.remove("hidden");
}

function closeModal() {
  $("modal-backdrop").classList.add("hidden");
  $("modal-card").classList.remove("modal-card-wide");
  $("modal-body").innerHTML = "";
}

$("modal-close").addEventListener("click", closeModal);
$("modal-backdrop").addEventListener("click", (e) => {
  if (e.target === $("modal-backdrop")) closeModal();
});

// ---------------------------------------------------------------------------
// Auth & First-Run Superuser Setup
// ---------------------------------------------------------------------------

async function checkAuth() {
  const status = await api("/api/auth/status");
  state.auth = status;
  if (!status.has_superuser) {
    $("auth-title").textContent = "Initialize FormSpec Studio";
    $("auth-subtitle").textContent =
      "Permission tables (admin, guest, anon) are ready. Set the superuser password to begin.";
    $("auth-username").value = "superuser";
    $("auth-submit").textContent = "Create Superuser";
    $("auth-gate").classList.remove("hidden");
    $("workspace").classList.add("hidden");
    return;
  }
  if (!status.authenticated) {
    $("auth-title").textContent = "Sign In to FormSpec Studio";
    $("auth-subtitle").textContent = "Authenticate to manage entities, permissions, and forms.";
    $("auth-username").value = "";
    $("auth-submit").textContent = "Sign In";
    $("auth-gate").classList.remove("hidden");
    $("workspace").classList.add("hidden");
    return;
  }

  $("auth-gate").classList.add("hidden");
  $("workspace").classList.remove("hidden");
  $("active-user-badge").textContent = `${status.user.username} (${status.user.roles.join(", ")})`;
  const adminMenu = $("admin-menu");
  if (adminMenu) {
    adminMenu.open = false;
    adminMenu.classList.toggle("hidden", !(status.is_sqlite && status.user?.is_superuser));
  }
  await loadWorkspace();
}

$("auth-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("auth-error").classList.add("hidden");
  const username = $("auth-username").value.trim();
  const password = $("auth-password").value;
  try {
    const endpoint = state.auth?.has_superuser ? "/api/auth/login" : "/api/auth/setup";
    await api(endpoint, {
      method: "POST",
      body: JSON.stringify({ username, password }),
    });
    $("auth-password").value = "";
    await checkAuth();
  } catch (err) {
    $("auth-error").textContent = err.message;
    $("auth-error").classList.remove("hidden");
  }
});

$("btn-logout").addEventListener("click", async () => {
  await api("/api/auth/logout", { method: "POST" });
  await checkAuth();
});

$("btn-wipe-db").addEventListener("click", () => {
  $("admin-menu").open = false;
  openModal(
    "Wipe & Re-initialize SQLite Database",
    `<form id="form-wipe-db" class="modal-form">
      <div class="error-banner">Warning: This will drop ALL tables, domain entities, forms, actions, modules, roles, permissions, users, and data in the SQLite database and re-initialize an empty workspace.</div>
      <div class="muted">After wiping, you will be prompted to create a new superuser password.</div>
      <div id="wipe-db-err" class="error-banner hidden"></div>
      <div style="display:flex; gap:8px; justify-content:flex-end;">
        <button type="button" id="btn-wipe-cancel" class="btn btn-secondary">Cancel</button>
        <button type="submit" class="btn btn-danger">Wipe &amp; Re-initialize</button>
      </div>
    </form>`
  );
  $("btn-wipe-cancel").addEventListener("click", closeModal);
  $("form-wipe-db").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await api("/api/admin/reset-db", { method: "POST" });
      state.activeItem = null;
      state.activeFormId = null;
      state.dirty = false;
      state.entities = [];
      state.forms = [];
      state.serverModules = [];
      state.serverActions = [];
      state.clientModules = [];
      state.clientActions = [];
      state.breadcrumbs = [];
      $("code-editor").value = "";
      $("editor-form-title").textContent = "No item selected";
      $("editor-dirty").classList.add("hidden");
      $("btn-save-form").disabled = true;
      $("btn-export-form").disabled = true;
      $("btn-delete-form").disabled = true;
      closeModal();
      await checkAuth();
    } catch (err) {
      $("wipe-db-err").textContent = err.message;
      $("wipe-db-err").classList.remove("hidden");
    }
  });
});

document.addEventListener("click", (e) => {
  const menu = $("admin-menu");
  if (menu && menu.open && !menu.contains(e.target)) {
    menu.open = false;
  }
});

// ---------------------------------------------------------------------------
// Workspace Load & Rendering
// ---------------------------------------------------------------------------

async function loadWorkspace() {
  const [entRes, formRes, smRes, saRes, cmRes, caRes, fmtRes, themeRes, rbacRes, gitRes] =
    await Promise.all([
      api("/api/entities"),
      api("/api/forms"),
      api("/api/server-modules"),
      api("/api/server-actions"),
      api("/api/client-modules"),
      api("/api/client-actions"),
      api("/api/formatters"),
      api("/api/themes"),
      api("/api/rbac"),
      api("/api/git/status"),
    ]);
  state.entities = entRes.entities;
  state.forms = formRes.forms;
  state.serverModules = smRes.server_modules;
  state.serverActions = saRes.server_actions;
  state.clientModules = cmRes.client_modules;
  state.clientActions = caRes.client_actions;
  state.formatters = fmtRes.formatters;
  state.themes = themeRes.themes;
  state.roles = ["superuser", ...rbacRes.roles];
  state.git = gitRes.git || { configured: false, cloned: false, branches: [] };

  renderGitControls();
  if (gitRes.git_error) {
    $("diagnostics-bar").classList.add("has-errors");
    $("diagnostics-bar").textContent = `Git sync warning: ${gitRes.git_error}`;
  }
  renderSelects();
  renderEntities();
  renderAllSidebarLists();

  const currentStillExists =
    state.activeItem && findItem(state.activeItem.kind, state.activeItem.id);
  if (!currentStillExists && state.forms.length > 0) {
    const homeForm = state.forms.find((f) => f.name === "home") || state.forms[0];
    selectEditorItem("form", homeForm.id);
  } else if (state.activeItem?.kind === "form") {
    await renderPreview();
  }
}

function renderSelects() {
  $("select-formatter").innerHTML = state.formatters
    .map((f) => `<option value="${escapeHtml(f.id)}">${escapeHtml(f.name)}</option>`)
    .join("");
  $("select-formatter").value = state.selectedFormatter;

  $("select-theme").innerHTML = state.themes
    .map((t) => `<option value="${escapeHtml(t.id)}">${escapeHtml(t.name)}</option>`)
    .join("");
  $("select-theme").value = state.selectedTheme;

  $("select-role").innerHTML = state.roles
    .map((r) => `<option value="${escapeHtml(r)}">${escapeHtml(r)}</option>`)
    .join("");
  if (!state.roles.includes(state.selectedRole)) {
    state.selectedRole = state.roles[0] || "superuser";
  }
  $("select-role").value = state.selectedRole;
}

function renderEntities() {
  const container = $("entities-list");
  if (state.entities.length === 0) {
    container.innerHTML = `<div class="muted">No domain entities yet. Click + Entity to create one.</div>`;
    return;
  }

  container.innerHTML = state.entities
    .map(
      (ent) => `
      <div class="sidebar-item" data-entity-name="${escapeHtml(ent.name)}" data-hover-kind="entity" data-hover-id="${escapeHtml(ent.name)}">
        <span class="sidebar-item-name">${escapeHtml(ent.name)}</span>
      </div>`
    )
    .join("");
}

function itemCollection(kind) {
  switch (kind) {
    case "form":
      return state.forms;
    case "server_module":
      return state.serverModules;
    case "server_action":
      return state.serverActions;
    case "client_module":
      return state.clientModules;
    case "client_action":
      return state.clientActions;
    default:
      return [];
  }
}

function findItem(kind, id) {
  return itemCollection(kind).find((x) => x.id === id) || null;
}

function isItemActive(kind, id) {
  return state.activeItem?.kind === kind && state.activeItem?.id === id;
}

function renderAllSidebarLists() {
  renderForms();
  renderServerActions();
  renderClientActions();
  renderServerModules();
  renderClientModules();
}

function renderForms() {
  const container = $("forms-list");
  if (state.forms.length === 0) {
    container.innerHTML = `<div class="muted">No saved forms yet.</div>`;
    return;
  }
  container.innerHTML = state.forms
    .map(
      (f) => `
      <div class="sidebar-item form-item ${isItemActive("form", f.id) ? "active" : ""}" data-form-id="${f.id}" data-hover-kind="form" data-hover-id="${f.id}">
        <span class="sidebar-item-name">${escapeHtml(f.name)}</span>
      </div>`
    )
    .join("");
}

function renderServerModules() {
  const container = $("server-modules-list");
  if (state.serverModules.length === 0) {
    container.innerHTML = `<div class="muted">No server modules yet.</div>`;
    return;
  }
  container.innerHTML = state.serverModules
    .map(
      (sm) => `
      <div class="sidebar-item action-item ${isItemActive("server_module", sm.id) ? "active" : ""}" data-server-module-id="${sm.id}" data-hover-kind="server_module" data-hover-id="${sm.id}">
        <span class="sidebar-item-name">${escapeHtml(sm.name)}</span>
      </div>`
    )
    .join("");
}

function renderServerActions() {
  const container = $("server-actions-list");
  if (state.serverActions.length === 0) {
    container.innerHTML = `<div class="muted">No server actions yet.</div>`;
    return;
  }
  container.innerHTML = state.serverActions
    .map(
      (sa) => `
      <div class="sidebar-item action-item ${isItemActive("server_action", sa.id) ? "active" : ""}" data-server-action-id="${sa.id}" data-hover-kind="server_action" data-hover-id="${sa.id}">
        <span class="sidebar-item-name">${escapeHtml(sa.name)}</span>
      </div>`
    )
    .join("");
}

function renderClientModules() {
  const container = $("client-modules-list");
  if (state.clientModules.length === 0) {
    container.innerHTML = `<div class="muted">No client modules yet.</div>`;
    return;
  }
  container.innerHTML = state.clientModules
    .map(
      (cm) => `
      <div class="sidebar-item action-item ${isItemActive("client_module", cm.id) ? "active" : ""}" data-client-module-id="${cm.id}" data-hover-kind="client_module" data-hover-id="${cm.id}">
        <span class="sidebar-item-name">${escapeHtml(cm.name)}</span>
      </div>`
    )
    .join("");
}

function renderClientActions() {
  const container = $("client-actions-list");
  if (state.clientActions.length === 0) {
    container.innerHTML = `<div class="muted">No client actions yet.</div>`;
    return;
  }
  container.innerHTML = state.clientActions
    .map(
      (ca) => `
      <div class="sidebar-item action-item ${isItemActive("client_action", ca.id) ? "active" : ""}" data-client-action-id="${ca.id}" data-hover-kind="client_action" data-hover-id="${ca.id}">
        <span class="sidebar-item-name">${escapeHtml(ca.name)}</span>
      </div>`
    )
    .join("");
}

function buildHoverCardHtml(kind, rawId) {
  if (kind === "entity") {
    const ent = state.entities.find((e) => e.name === rawId);
    if (!ent) return null;
    const colsHtml = ent.columns
      .map((c) => {
        const badges = [];
        if (c.is_pk) badges.push("PK");
        if (c.fk_table) badges.push(`<span class="fk-tag">&rarr; ${escapeHtml(c.fk_table)}</span>`);
        if (!c.nullable && !c.is_pk) badges.push("*");
        return `<li><code>${escapeHtml(c.name)}</code> (${escapeHtml(c.fspec_type)}) ${badges.join(" ")}</li>`;
      })
      .join("");
    return `
      <div class="hover-card-header">
        <strong>${escapeHtml(ent.label)}</strong>
        <span class="badge">entity</span>
      </div>
      <div class="hover-card-meta">
        <div>Name: <code>${escapeHtml(ent.name)}</code></div>
        ${ent.description ? `<div>${escapeHtml(ent.description)}</div>` : ""}
      </div>
      <ul class="attr-list">${colsHtml}</ul>
      <div class="entity-actions">
        <button class="btn btn-small btn-secondary" data-action="add-attr" data-entity="${escapeHtml(ent.name)}">+ Attr</button>
        <button class="btn btn-small btn-secondary" data-action="add-fk" data-entity="${escapeHtml(ent.name)}">+ FK</button>
        <button class="btn btn-small btn-primary" data-action="gen-detail" data-entity="${escapeHtml(ent.name)}">+ Detail</button>
        <button class="btn btn-small btn-primary" data-action="gen-list" data-entity="${escapeHtml(ent.name)}">+ List</button>
      </div>`;
  }

  const item = findItem(kind, Number(rawId));
  if (!item) return null;
  if (kind === "form") {
    return `
      <div class="hover-card-header">
        <strong>${escapeHtml(item.label)}</strong>
        <div style="display:flex; gap:4px;">
          ${item.name === "home" ? '<span class="badge">entry</span>' : ""}
          <span class="badge">${escapeHtml(item.form_type || "detail")}</span>
        </div>
      </div>
      <div class="hover-card-meta">
        <div>Name: <code>${escapeHtml(item.name)}</code></div>
        <div>Entity: <code>${escapeHtml(item.entity_name || "none")}</code></div>
        <div>Formatter / Theme: ${escapeHtml(item.formatter || "html5")} / ${escapeHtml(item.theme || "default")}</div>
        ${item.description ? `<div>${escapeHtml(item.description)}</div>` : ""}
      </div>`;
  }
  if (kind === "server_action") {
    return `
      <div class="hover-card-header">
        <strong>${escapeHtml(item.label)}</strong>
        <span class="badge">${escapeHtml(item.action_type)} • ${escapeHtml(item.target_runtime)}</span>
      </div>
      <div class="hover-card-meta">
        <div>Name: <code>${escapeHtml(item.name)}</code></div>
        <div>Entity: <code>${escapeHtml(item.entity_name || "standalone")}</code></div>
        ${item.description ? `<div>${escapeHtml(item.description)}</div>` : ""}
      </div>`;
  }
  if (kind === "client_action") {
    return `
      <div class="hover-card-header">
        <strong>${escapeHtml(item.label)}</strong>
        <span class="badge">${escapeHtml(item.action_type)}</span>
      </div>
      <div class="hover-card-meta">
        <div>Name: <code>${escapeHtml(item.name)}</code></div>
        ${item.target_form_name ? `<div>Target Form: <code>${escapeHtml(item.target_form_name)}</code></div>` : ""}
        ${item.params_spec ? `<div>Params: <code>${escapeHtml(item.params_spec)}</code></div>` : ""}
        ${item.description ? `<div>${escapeHtml(item.description)}</div>` : ""}
      </div>`;
  }
  return `
    <div class="hover-card-header">
      <strong>${escapeHtml(item.label)}</strong>
      <span class="badge">${escapeHtml(item.target_runtime || "browser js")}</span>
    </div>
    <div class="hover-card-meta">
      <div>Name: <code>${escapeHtml(item.name)}</code></div>
      ${item.description ? `<div>${escapeHtml(item.description)}</div>` : ""}
    </div>`;
}

let hoverHideTimer = null;
const hoverCardEl = $("sidebar-hover-card");

function showHoverCardFor(targetEl) {
  clearTimeout(hoverHideTimer);
  const kind = targetEl.dataset.hoverKind;
  const rawId = targetEl.dataset.hoverId;
  const html = buildHoverCardHtml(kind, rawId);
  if (!html) return;
  hoverCardEl.innerHTML = html;
  hoverCardEl.classList.remove("hidden");
  const rect = targetEl.getBoundingClientRect();
  const cardHeight = hoverCardEl.offsetHeight || 160;
  const top = Math.max(54, Math.min(rect.top, window.innerHeight - cardHeight - 12));
  hoverCardEl.style.left = `${Math.min(rect.right + 8, window.innerWidth - 320)}px`;
  hoverCardEl.style.top = `${top}px`;
}

function scheduleHideHoverCard() {
  clearTimeout(hoverHideTimer);
  hoverHideTimer = setTimeout(() => {
    hoverCardEl.classList.add("hidden");
  }, 140);
}

document.querySelector(".sidebar").addEventListener("mouseover", (e) => {
  const row = e.target.closest(".sidebar-item[data-hover-kind]");
  if (row) showHoverCardFor(row);
});

document.querySelector(".sidebar").addEventListener("mouseout", (e) => {
  const row = e.target.closest(".sidebar-item[data-hover-kind]");
  if (row && !row.contains(e.relatedTarget) && !hoverCardEl.contains(e.relatedTarget)) {
    scheduleHideHoverCard();
  }
});

hoverCardEl.addEventListener("mouseenter", () => clearTimeout(hoverHideTimer));
hoverCardEl.addEventListener("mouseleave", scheduleHideHoverCard);

function confirmUnsavedChanges(targetLabel = "another item") {
  if (!state.dirty || !state.activeItem) return Promise.resolve(true);
  const currentTitle = $("editor-form-title").textContent || "Current item";
  return new Promise((resolve) => {
    let settled = false;
    const finish = (choice) => {
      if (settled) return;
      settled = true;
      resolve(choice);
    };
    openModal(
      "Unsaved Changes",
      `<div class="modal-form">
        <p style="margin:0;"><strong>${escapeHtml(currentTitle)}</strong> has unsaved edits before opening <strong>${escapeHtml(targetLabel)}</strong>.</p>
        <div id="unsaved-err" class="error-banner hidden"></div>
        <div style="display:flex; gap:8px; justify-content:flex-end; margin-top:6px;">
          <button type="button" id="btn-unsaved-cancel" class="btn btn-secondary">Cancel</button>
          <button type="button" id="btn-unsaved-discard" class="btn btn-danger">Discard</button>
          <button type="button" id="btn-unsaved-save" class="btn btn-primary">Save</button>
        </div>
      </div>`
    );
    $("btn-unsaved-cancel").addEventListener("click", () => {
      closeModal();
      finish(false);
    });
    $("modal-close").addEventListener("click", () => finish(false), { once: true });
    $("btn-unsaved-discard").addEventListener("click", () => {
      state.dirty = false;
      $("editor-dirty").classList.add("hidden");
      closeModal();
      finish(true);
    });
    $("btn-unsaved-save").addEventListener("click", async () => {
      try {
        await saveActiveItem();
        closeModal();
        finish(true);
      } catch (err) {
        $("unsaved-err").textContent = err.message;
        $("unsaved-err").classList.remove("hidden");
      }
    });
  });
}

async function switchEditorItem(kind, id, opts = {}) {
  const target = findItem(kind, id);
  if (!target) return false;
  if (state.activeItem?.kind === kind && state.activeItem?.id === id) {
    if (opts.keepRecord && kind === "form") selectEditorItem(kind, id, opts);
    return true;
  }
  const ok = await confirmUnsavedChanges(`${target.label} (${target.name})`);
  if (!ok) return false;
  selectEditorItem(kind, id, opts);
  return true;
}

function syncBreadcrumbsForForm(form, opts = {}) {
  const entry = {
    formId: form.id,
    name: form.name,
    label: form.label,
    params: { ...(state.previewParams || {}) },
    recordId: state.previewRecordId ?? null,
  };

  if (typeof opts.fromBreadcrumbIndex === "number" && opts.fromBreadcrumbIndex >= 0) {
    state.breadcrumbs = state.breadcrumbs.slice(0, opts.fromBreadcrumbIndex + 1);
    if (state.breadcrumbs.length > 0) {
      state.breadcrumbs[state.breadcrumbs.length - 1] = entry;
    } else {
      state.breadcrumbs = [entry];
    }
    return;
  }

  if (form.name === "home") {
    state.breadcrumbs = [entry];
    return;
  }

  if (opts.pushBreadcrumb) {
    const existingIdx = state.breadcrumbs.findIndex(
      (b) => b.name === entry.name && (b.recordId ?? null) === (entry.recordId ?? null)
    );
    if (existingIdx !== -1) {
      state.breadcrumbs = state.breadcrumbs.slice(0, existingIdx + 1);
      state.breadcrumbs[existingIdx] = entry;
    } else {
      if (state.breadcrumbs.length === 0) {
        const homeForm = state.forms.find((f) => f.name === "home");
        if (homeForm) {
          state.breadcrumbs.push({
            formId: homeForm.id,
            name: homeForm.name,
            label: homeForm.label,
            params: {},
            recordId: null,
          });
        }
      }
      state.breadcrumbs.push(entry);
    }
    return;
  }

  const homeForm = state.forms.find((f) => f.name === "home");
  state.breadcrumbs = homeForm
    ? [
        {
          formId: homeForm.id,
          name: homeForm.name,
          label: homeForm.label,
          params: {},
          recordId: null,
        },
        entry,
      ]
    : [entry];
}

function selectEditorItem(kind, id, opts = {}) {
  const item = findItem(kind, id);
  if (!item) return;

  state.activeItem = { kind, id: item.id };
  state.dirty = false;
  $("editor-dirty").classList.add("hidden");

  if (kind === "form") {
    state.activeFormId = item.id;
    state.selectedFormatter = item.formatter || "html5";
    state.selectedTheme = item.theme || "default";
    if (!opts.keepRecord) {
      state.previewRecordId = null;
      state.previewParams = {};
      state.previewFilters = {};
      state.previewSearch = "";
    }
    syncBreadcrumbsForForm(item, opts);
    $("editor-form-title").textContent = `${item.label} (${item.name})`;
    $("code-editor").value = item.fspec_source || "";
    $("btn-export-form").disabled = false;
    $("btn-item-meta").classList.add("hidden");
  } else {
    state.activeFormId = null;
    const kindLabels = {
      server_module: "Server Module",
      server_action: "Server Action",
      client_module: "Client Module",
      client_action: "Client Action",
    };
    $("editor-form-title").textContent = `${kindLabels[kind] || kind}: ${item.label} (${item.name})`;
    $("code-editor").value = item.code || "";
    $("btn-export-form").disabled = true;
    $("btn-item-meta").classList.toggle("hidden", kind !== "client_action");
  }

  $("btn-save-form").disabled = false;
  $("btn-delete-form").disabled = false;

  renderSelects();
  renderAllSidebarLists();
  renderPreview();
}

function selectForm(formId, opts = {}) {
  selectEditorItem("form", formId, opts);
}

// ---------------------------------------------------------------------------
// Live Preview & Tab-Aware Editor
// ---------------------------------------------------------------------------

let previewTimer = null;
function schedulePreview() {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(renderPreview, 180);
}

function showCodeContextPreview(kind, item, codeText) {
  $("preview-frame").classList.add("hidden");
  $("preview-code").classList.remove("hidden");
  const lines = [
    `// ${kind}: ${item.name}`,
    `// label: ${item.label}`,
  ];
  if (item.entity_name) lines.push(`// entity: ${item.entity_name}`);
  if (item.target_runtime) lines.push(`// runtime: ${item.target_runtime}`);
  if (item.target_form_name) lines.push(`// target_form: ${item.target_form_name}`);
  if (item.params_spec) lines.push(`// params_spec: ${item.params_spec}`);
  if (item.description) lines.push(`// ${item.description}`);
  lines.push("", codeText);
  $("preview-code").textContent = lines.join("\n");
  const diagBar = $("diagnostics-bar");
  diagBar.classList.remove("has-errors");
  diagBar.textContent = `Editing ${kind} '${item.name}' (Ctrl+S to save)`;
}

async function renderPreview() {
  if (state.activeItem && state.activeItem.kind !== "form") {
    const activeObj = findItem(state.activeItem.kind, state.activeItem.id);
    if (activeObj) {
      showCodeContextPreview(state.activeItem.kind, activeObj, $("code-editor").value);
    }
    return;
  }
  const source = $("code-editor").value;
  const activeForm = state.forms.find((f) => f.id === state.activeFormId);
  try {
    const res = await api("/api/render", {
      method: "POST",
      body: JSON.stringify({
        fspec_source: source,
        formatter: state.selectedFormatter,
        role: state.selectedRole,
        filters: state.previewFilters,
        params: state.previewParams,
        search: state.previewSearch,
        record_id: state.previewRecordId,
        entity_name: activeForm?.entity_name || null,
      }),
    });

    if (res.roles) {
      const merged = Array.from(new Set([...state.roles, ...res.roles]));
      state.roles = merged;
      renderSelects();
    }

    const diagBar = $("diagnostics-bar");
    if (res.diagnostics.length > 0) {
      diagBar.classList.add("has-errors");
      diagBar.textContent = res.diagnostics
        .map((d) => `L${d.line}:${d.column} ${d.message}`)
        .join(" | ");
    } else {
      diagBar.classList.remove("has-errors");
      const recBadge = state.previewRecordId ? ` • record #${state.previewRecordId}` : "";
      diagBar.textContent = `Valid FormSpec (${res.formatter_id} • role: ${state.selectedRole}${recBadge})`;
    }

    if (res.output_kind === "html") {
      $("preview-frame").classList.remove("hidden");
      $("preview-code").classList.add("hidden");
      const themeUrl = `/themes/${encodeURIComponent(state.selectedTheme)}.css`;
      const headerHtml = await renderCustomAppHeader(activeForm);
      const bridgeScript = `<script>
(function () {
  document.addEventListener("click", (e) => {
    const crumb = e.target.closest("[data-breadcrumb-index]");
    if (crumb) {
      e.preventDefault();
      window.parent.postMessage({
        type: "formspec:breadcrumb",
        index: Number(crumb.dataset.breadcrumbIndex),
      }, "*");
    }
  });

  const form = document.querySelector("form.fs-form");
  if (!form) return;
  const formType = form.dataset.formType || "detail";
  let selectedRow = null;

  function collectPayload() {
    const payload = {};
    for (const el of form.querySelectorAll("input[name], select[name], textarea[name]")) {
      if (el.disabled && el.type !== "hidden") continue;
      if (el.type === "checkbox") payload[el.name] = el.checked;
      else if (el.type === "number") payload[el.name] = el.value === "" ? null : Number(el.value);
      else payload[el.name] = el.value === "" ? null : el.value;
    }
    return payload;
  }

  function resolveParamMap(rawJson, rowObj) {
    let spec = {};
    try { spec = rawJson ? JSON.parse(rawJson) : {}; } catch (_) {}
    const resolved = {};
    const formValues = {};
    for (const el of form.querySelectorAll("input[name], select[name], textarea[name]")) {
      formValues[el.name] = el.type === "checkbox" ? el.checked : el.value;
    }
    for (const [k, expr] of Object.entries(spec)) {
      if (typeof expr === "string" && expr.startsWith("$row.")) {
        const col = expr.slice(5);
        resolved[k] = rowObj ? rowObj[col] : null;
      } else if (typeof expr === "string" && expr.startsWith("$selected.")) {
        const col = expr.slice(10);
        resolved[k] = selectedRow ? selectedRow[col] : null;
      } else if (typeof expr === "string" && expr.startsWith("$")) {
        resolved[k] = formValues[expr.slice(1)] ?? null;
      } else {
        resolved[k] = expr;
      }
    }
    return resolved;
  }

  if (formType === "list") {
    let t = null;
    const emitFilter = () => {
      const filters = {};
      let search = "";
      for (const el of form.querySelectorAll("input[name], select[name]")) {
        if (el.closest(".fs-list-wrap")) continue;
        if (el.name === "q") search = el.value;
        else if (el.value !== "") filters[el.name] = el.value;
      }
      window.parent.postMessage({ type: "formspec:filter", filters, search }, "*");
    };
    form.addEventListener("input", (e) => {
      if (e.target.name === "q") { clearTimeout(t); t = setTimeout(emitFilter, 220); }
    });
    form.addEventListener("change", (e) => {
      if (e.target.tagName === "SELECT") emitFilter();
    });
  } else {
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const caBtn = form.querySelector("button[data-client-action]");
      const saBtn = form.querySelector("button[data-server-action]");
      window.parent.postMessage({
        type: "formspec:submit",
        payload: collectPayload(),
        clientAction: caBtn ? caBtn.dataset.clientAction : null,
        serverAction: saBtn ? saBtn.dataset.serverAction : null,
      }, "*");
    });
  }

  form.addEventListener("click", (e) => {
    const tr = e.target.closest("tr.fs-list-row");
    if (tr) {
      for (const other of form.querySelectorAll("tr.fs-list-row.is-selected")) {
        other.classList.remove("is-selected");
      }
      tr.classList.add("is-selected");
      try { selectedRow = JSON.parse(tr.dataset.row || "{}"); } catch (_) { selectedRow = null; }
    }

    const clientTrigger = e.target.closest("[data-client-action]");
    if (clientTrigger) {
      if (clientTrigger.tagName === "BUTTON" && clientTrigger.type === "submit") {
        return; // Let form submit handler run with full payload + clientAction
      }
      e.preventDefault();
      const rowData = tr ? JSON.parse(tr.dataset.row || "{}") : selectedRow;
      const params = resolveParamMap(clientTrigger.dataset.linkParams, rowData);
      window.parent.postMessage({
        type: "formspec:client-action",
        action: clientTrigger.dataset.clientAction,
        params,
        payload: collectPayload(),
      }, "*");
      return;
    }

    const linkTrigger = e.target.closest("[data-link-target]");
    if (linkTrigger) {
      e.preventDefault();
      const target = linkTrigger.dataset.linkTarget;
      const rowData = tr ? JSON.parse(tr.dataset.row || "{}") : selectedRow;
      const params = resolveParamMap(linkTrigger.dataset.linkParams, rowData);
      const recordId = params.id ?? (linkTrigger.dataset.recordId || null);
      if (target) {
        window.parent.postMessage({
          type: "formspec:navigate",
          target,
          recordId,
          params,
        }, "*");
      }
    }
  });
})();
<\/script>`;
      $("preview-frame").srcdoc = `<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8" />
  <link rel="stylesheet" href="${themeUrl}" />
</head>
<body>${headerHtml}${res.content}${bridgeScript}</body>
</html>`;
    } else {
      $("preview-frame").classList.add("hidden");
      $("preview-code").classList.remove("hidden");
      $("preview-code").textContent = res.content;
    }
  } catch (err) {
    $("diagnostics-bar").classList.add("has-errors");
    $("diagnostics-bar").textContent = err.message;
  }
}

async function runPreviewServerAction(serverActionName, payload) {
  const activeForm = state.forms.find((f) => f.id === state.activeFormId);
  const res = await api("/api/preview/submit", {
    method: "POST",
    body: JSON.stringify({
      entity: activeForm?.entity_name || null,
      server_action: serverActionName || null,
      record_id: state.previewRecordId,
      payload,
      role: state.selectedRole,
    }),
  });
  state.previewRecordId = res.id;
  state.previewParams = { ...state.previewParams, id: res.id };
  return res;
}

async function withImportedClientModule(sourceCode, fn) {
  const blobUrls = [];
  const modUrlMap = new Map();
  for (const cm of state.clientModules) {
    const url = URL.createObjectURL(new Blob([cm.code], { type: "text/javascript" }));
    blobUrls.push(url);
    modUrlMap.set(`./${cm.name}.mjs`, url);
    modUrlMap.set(`./${cm.name}.js`, url);
    modUrlMap.set(`./${cm.name}`, url);
  }
  const rewrittenCode = sourceCode.replace(
    /(from\s+["'])(\.\/[^"']+)(["'])/g,
    (full, prefix, spec, suffix) =>
      modUrlMap.has(spec) ? `${prefix}${modUrlMap.get(spec)}${suffix}` : full
  );
  const entryUrl = URL.createObjectURL(
    new Blob([rewrittenCode], { type: "text/javascript" })
  );
  blobUrls.push(entryUrl);
  try {
    const mod = await import(entryUrl);
    return await fn(mod);
  } finally {
    for (const u of blobUrls) URL.revokeObjectURL(u);
  }
}

async function renderCustomAppHeader(activeForm) {
  const headerMod = state.clientModules.find((m) => m.name === "formspec_header");
  const headerCtx = {
    formName: activeForm?.name || "home",
    formLabel: activeForm?.label || activeForm?.name || "Home",
    formType: activeForm?.form_type || "detail",
    breadcrumbs: state.breadcrumbs,
    role: state.selectedRole,
    recordId: state.previewRecordId,
  };
  if (headerMod && headerMod.code && headerMod.code.trim()) {
    try {
      return await withImportedClientModule(headerMod.code, async (mod) => {
        if (typeof mod.renderAppHeader === "function") {
          return String(await mod.renderAppHeader(headerCtx));
        }
        return "";
      });
    } catch (err) {
      return `<header class="fs-app-header"><span style="color:#dc2626;">Header module error: ${escapeHtml(err.message)}</span></header>`;
    }
  }
  return "";
}

async function navigatePreviewForm(targetName, params = {}) {
  const targetForm = state.forms.find((f) => f.name === targetName);
  if (!targetForm) {
    throw new Error(`Linked form '${targetName}' not found. Generate it from the entity first.`);
  }
  const ok = await confirmUnsavedChanges(`${targetForm.label} (${targetForm.name})`);
  if (!ok) return;
  state.previewParams = params || {};
  state.previewRecordId = params?.id ? Number(params.id) : null;
  if (state.previewRecordId !== null && state.previewParams.id === undefined) {
    state.previewParams.id = state.previewRecordId;
  }
  selectEditorItem("form", targetForm.id, { keepRecord: true, pushBreadcrumb: true });
}

async function executeClientAction(actionName, { payload = {}, params = {} } = {}) {
  const ca = state.clientActions.find((c) => c.name === actionName);
  if (!ca) {
    throw new Error(`Client action '${actionName}' not found.`);
  }
  const activeForm = state.forms.find((f) => f.id === state.activeFormId);
  const ctx = {
    payload,
    params,
    selected: params,
    role: state.selectedRole,
    entity: activeForm?.entity_name || null,
    form: activeForm?.name || null,
    serverAction: async (saName, saPayload) => runPreviewServerAction(saName, saPayload),
    navigate: async (targetName, navParams = {}) => await navigatePreviewForm(targetName, navParams),
  };

  if (ca.code && ca.code.trim()) {
    return await withImportedClientModule(ca.code, async (mod) => {
      if (typeof mod.run !== "function") {
        throw new Error(`Client action '${ca.name}' must export 'async function run(ctx)'.`);
      }
      const result = await mod.run(ctx);
      if (result && result.status === "ok" && result.id !== undefined) {
        $("diagnostics-bar").classList.remove("has-errors");
        $("diagnostics-bar").textContent = `Saved (${result.action}) #${result.id} via ${ca.name} as ${state.selectedRole}`;
        await renderPreview();
      } else if (state.activeItem?.kind === "form" && state.activeFormId === activeForm?.id) {
        $("diagnostics-bar").classList.remove("has-errors");
        $("diagnostics-bar").textContent = `Executed client_action '${ca.name}'`;
      }
      return result;
    });
  }

  if (ca.target_form_name) {
    await navigatePreviewForm(ca.target_form_name, params);
    return null;
  }
  $("diagnostics-bar").classList.remove("has-errors");
  $("diagnostics-bar").textContent = `Executed client_action '${ca.name}' with params ${JSON.stringify(params || {})}`;
  return null;
}

window.addEventListener("message", async (e) => {
  const msg = e.data;
  if (!msg || typeof msg !== "object") return;
  if (msg.type === "formspec:filter") {
    state.previewFilters = msg.filters || {};
    state.previewSearch = msg.search || "";
    await renderPreview();
    return;
  }
  if (msg.type === "formspec:breadcrumb") {
    const crumb = state.breadcrumbs[msg.index];
    if (!crumb) return;
    const targetForm = state.forms.find((f) => f.id === crumb.formId || f.name === crumb.name);
    if (!targetForm) return;
    const ok = await confirmUnsavedChanges(`${targetForm.label} (${targetForm.name})`);
    if (!ok) return;
    state.previewParams = { ...(crumb.params || {}) };
    state.previewRecordId = crumb.recordId ?? null;
    state.previewFilters = {};
    state.previewSearch = "";
    selectEditorItem("form", targetForm.id, {
      keepRecord: true,
      fromBreadcrumbIndex: msg.index,
    });
    return;
  }
  if (msg.type === "formspec:client-action") {
    try {
      await executeClientAction(msg.action, {
        payload: msg.payload || {},
        params: msg.params || {},
      });
    } catch (err) {
      $("diagnostics-bar").classList.add("has-errors");
      $("diagnostics-bar").textContent = err.message;
    }
    return;
  }
  if (msg.type === "formspec:navigate") {
    try {
      const navParams = { ...(msg.params || {}) };
      if (msg.recordId && navParams.id === undefined) {
        navParams.id = Number(msg.recordId);
      }
      await navigatePreviewForm(msg.target, navParams);
    } catch (err) {
      $("diagnostics-bar").classList.add("has-errors");
      $("diagnostics-bar").textContent = err.message;
    }
    return;
  }
  if (msg.type === "formspec:submit") {
    const activeForm = state.forms.find((f) => f.id === state.activeFormId);
    if (!msg.clientAction && !activeForm?.entity_name && !msg.serverAction) return;
    try {
      if (msg.clientAction) {
        await executeClientAction(msg.clientAction, {
          payload: msg.payload || {},
          params: state.previewParams || {},
        });
      } else {
        const res = await runPreviewServerAction(msg.serverAction, msg.payload);
        $("diagnostics-bar").classList.remove("has-errors");
        $("diagnostics-bar").textContent = `Saved (${res.action}) #${res.id} via ${msg.serverAction || "upsert"} as ${state.selectedRole}`;
        await renderPreview();
      }
    } catch (err) {
      $("diagnostics-bar").classList.add("has-errors");
      $("diagnostics-bar").textContent = err.message;
    }
  }
});

const editorEl = $("code-editor");
editorEl.addEventListener("input", () => {
  state.dirty = true;
  $("editor-dirty").classList.remove("hidden");
  if (state.activeItem?.kind === "form") {
    schedulePreview();
  }
});

editorEl.addEventListener("keydown", (e) => {
  if (e.key === "Tab") {
    e.preventDefault();
    const start = editorEl.selectionStart;
    const end = editorEl.selectionEnd;
    const val = editorEl.value;
    if (!e.shiftKey) {
      editorEl.value = val.slice(0, start) + "\t" + val.slice(end);
      editorEl.selectionStart = editorEl.selectionEnd = start + 1;
    } else if (start > 0 && val[start - 1] === "\t") {
      editorEl.value = val.slice(0, start - 1) + val.slice(end);
      editorEl.selectionStart = editorEl.selectionEnd = start - 1;
    }
    state.dirty = true;
    $("editor-dirty").classList.remove("hidden");
    if (state.activeItem?.kind === "form") {
      schedulePreview();
    }
  } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
    e.preventDefault();
    if (!$("btn-save-form").disabled) saveActiveItem();
  }
});

$("select-formatter").addEventListener("change", (e) => {
  state.selectedFormatter = e.target.value;
  if (state.activeItem?.kind === "form") renderPreview();
});
$("select-theme").addEventListener("change", (e) => {
  state.selectedTheme = e.target.value;
  if (state.activeItem?.kind === "form") renderPreview();
});
$("select-role").addEventListener("change", (e) => {
  state.selectedRole = e.target.value;
  if (state.activeItem?.kind === "form") renderPreview();
});

// ---------------------------------------------------------------------------
// Unified Editor Save / Delete / Export & Sidebar Selection
// ---------------------------------------------------------------------------

const KIND_ENDPOINT = {
  form: "/api/forms",
  server_module: "/api/server-modules",
  server_action: "/api/server-actions",
  client_module: "/api/client-modules",
  client_action: "/api/client-actions",
};

async function saveActiveItem() {
  if (!state.activeItem) return;
  const { kind, id } = state.activeItem;
  const text = $("code-editor").value;

  if (kind === "form") {
    const res = await api(`/api/forms/${id}`, {
      method: "PUT",
      body: JSON.stringify({
        fspec_source: text,
        formatter: state.selectedFormatter,
        theme: state.selectedTheme,
      }),
    });
    const idx = state.forms.findIndex((f) => f.id === id);
    if (idx !== -1) state.forms[idx] = res.form;
  } else {
    const res = await api(`${KIND_ENDPOINT[kind]}/${id}`, {
      method: "PUT",
      body: JSON.stringify({ code: text }),
    });
    const updated = res[kind];
    if (updated) {
      const col = itemCollection(kind);
      const idx = col.findIndex((x) => x.id === id);
      if (idx !== -1) col[idx] = updated;
      showCodeContextPreview(kind, updated, text);
    }
  }

  state.dirty = false;
  $("editor-dirty").classList.add("hidden");
  $("diagnostics-bar").classList.remove("has-errors");
  $("diagnostics-bar").textContent = `Saved ${kind} #${id}`;
  renderAllSidebarLists();
}

$("btn-save-form").addEventListener("click", saveActiveItem);

$("btn-delete-form").addEventListener("click", async () => {
  if (!state.activeItem) return;
  const { kind, id } = state.activeItem;
  await api(`${KIND_ENDPOINT[kind]}/${id}`, { method: "DELETE" });
  state.activeItem = null;
  state.activeFormId = null;
  state.dirty = false;
  $("code-editor").value = "";
  $("editor-form-title").textContent = "No item selected";
  $("editor-dirty").classList.add("hidden");
  $("btn-save-form").disabled = true;
  $("btn-export-form").disabled = true;
  $("btn-delete-form").disabled = true;
  $("btn-item-meta").classList.add("hidden");
  await loadWorkspace();
});

$("btn-export-form").addEventListener("click", async () => {
  if (!state.activeFormId) return;
  const res = await api(`/api/forms/${state.activeFormId}/export`, {
    method: "POST",
    body: JSON.stringify({ target: "both" }),
  });
  const blocks = res.files
    .map(
      (f) => `
      <div>
        <strong>${escapeHtml(f.filename)}</strong>
        <pre class="code-export">${escapeHtml(f.content)}</pre>
      </div>`
    )
    .join("");
  openModal("Exported FormSpec, Modules & Server Actions", blocks);
});

$("forms-list").addEventListener("click", async (e) => {
  const item = e.target.closest(".form-item");
  if (!item) return;
  await switchEditorItem("form", Number(item.dataset.formId));
});

$("btn-new-form").addEventListener("click", async () => {
  if (!(await confirmUnsavedChanges("new blank form"))) return;
  openModal(
    "Create Blank Form",
    `<form id="form-new-blank" class="modal-form">
      <label class="field-label"><span>Form Name (snake_case)</span><input name="name" placeholder="contact_request" required /></label>
      <label class="field-label"><span>Display Label</span><input name="label" placeholder="Contact Request" required /></label>
      <label class="field-label"><span>Form Type</span>
        <select name="form_type">
          <option value="detail">detail</option>
          <option value="list">list</option>
        </select>
      </label>
      <button type="submit" class="btn btn-primary">Create Form</button>
    </form>`
  );
  $("form-new-blank").addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const name = fd.get("name").trim();
    const label = fd.get("label").trim();
    const formType = fd.get("form_type");
    const starter =
      formType === "list"
        ? `::: form ${name}(type: list) "${label}" @(admin:write, *:none)\n\t[Search](q:text) | [[+ New]](create -> ${name}_detail) @(admin:write, *:none) | [3:1]\n\n\t::: list ${name}\n\t\t[ID](id:hidden)\n\t\t[Name](name:text -> ${name}_detail(id = $row.id))\n`
        : `::: form ${name} "${label}" @(admin:write, *:none)\n\t::: section "Details"\n\t\t[ID](id:hidden)\n\t\t[*Name](name:text)\n\n\t[[Submit]](submit:submit -> client_action:${name}_submit) @(admin:write, *:none)\n`;
    const res = await api("/api/forms", {
      method: "POST",
      body: JSON.stringify({ name, label, form_type: formType, fspec_source: starter }),
    });
    closeModal();
    await loadWorkspace();
    selectForm(res.form.id);
  });
});

// ---------------------------------------------------------------------------
// Entities, Attributes, Foreign Keys & Entity -> Form
// ---------------------------------------------------------------------------

$("btn-new-entity").addEventListener("click", () => {
  openModal(
    "Create New Entity",
    `<form id="form-new-entity" class="modal-form">
      <label class="field-label"><span>Singular Entity Name (snake_case)</span><input name="name" placeholder="department" required /></label>
      <label class="field-label"><span>Display Label</span><input name="label" placeholder="Department" required /></label>
      <label class="field-label"><span>Description</span><input name="description" placeholder="Organizational departments" /></label>
      <label class="field-label"><input type="checkbox" name="include_name" checked style="width:auto;" /> Include required <code>name</code> attribute (recommended for _id lookups)</label>
      <div class="muted">Automatically creates <code>id</code> primary key, <code>&lt;entity&gt;:read</code> / <code>&lt;entity&gt;:write</code> permissions, and an <code>&lt;entity&gt;_upsert</code> server action.</div>
      <div id="entity-modal-err" class="error-banner hidden"></div>
      <button type="submit" class="btn btn-primary">Create Entity</button>
    </form>`
  );
  $("form-new-entity").addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const attributes = fd.get("include_name")
      ? [{ name: "name", fspec_type: "text", nullable: false }]
      : [];
    try {
      await api("/api/entities", {
        method: "POST",
        body: JSON.stringify({
          name: fd.get("name").trim(),
          label: fd.get("label").trim(),
          description: fd.get("description").trim() || null,
          attributes,
        }),
      });
      closeModal();
      await loadWorkspace();
    } catch (err) {
      $("entity-modal-err").textContent = err.message;
      $("entity-modal-err").classList.remove("hidden");
    }
  });
});

async function handleEntityAction(action, entityName) {
  if (action === "gen-detail" || action === "gen-list") {
    const formType = action === "gen-list" ? "list" : "detail";
    if (!(await confirmUnsavedChanges(`new ${entityName} ${formType} form`))) return;
    const res = await api("/api/forms/from-entity", {
      method: "POST",
      body: JSON.stringify({ entity: entityName, form_type: formType }),
    });
    await loadWorkspace();
    selectEditorItem("form", res.form.id);
    return;
  }

  if (action === "add-attr") {
    openModal(
      `Add Attribute to "${entityName}"`,
      `<form id="form-add-attr" class="modal-form">
        <label class="field-label"><span>Attribute Name (snake_case)</span><input name="name" placeholder="email" required /></label>
        <label class="field-label"><span>Type</span>
          <select name="fspec_type">
            <option value="text">text</option>
            <option value="email">email</option>
            <option value="int">int</option>
            <option value="decimal">decimal</option>
            <option value="boolean">boolean</option>
            <option value="date">date</option>
            <option value="datetime">datetime</option>
            <option value="file">file</option>
          </select>
        </label>
        <label class="field-label"><span>Default Value (optional; required if NOT NULL)</span><input name="default" placeholder="" /></label>
        <label><input type="checkbox" name="nullable" checked /> Nullable (optional)</label>
        <label><input type="checkbox" name="create_permissions" /> Create granular <code>${escapeHtml(entityName)}:read_&lt;attr&gt;</code> &amp; <code>write_&lt;attr&gt;</code> permissions</label>
        <div id="attr-err" class="error-banner hidden"></div>
        <button type="submit" class="btn btn-primary">Add Attribute</button>
      </form>`
    );
    $("form-add-attr").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const fd = new FormData(ev.target);
      try {
        await api(`/api/entities/${encodeURIComponent(entityName)}/attributes`, {
          method: "POST",
          body: JSON.stringify({
            name: fd.get("name").trim(),
            fspec_type: fd.get("fspec_type"),
            nullable: Boolean(fd.get("nullable")),
            default: fd.get("default").trim() || null,
            create_permissions: Boolean(fd.get("create_permissions")),
          }),
        });
        closeModal();
        await loadWorkspace();
      } catch (err) {
        $("attr-err").textContent = err.message;
        $("attr-err").classList.remove("hidden");
      }
    });
    return;
  }

  if (action === "add-fk") {
    const targetOptions = state.entities
      .filter((ent) => ent.name !== entityName)
      .map((ent) => `<option value="${escapeHtml(ent.name)}">${escapeHtml(ent.label)} (${escapeHtml(ent.name)})</option>`)
      .join("");
    if (!targetOptions) {
      openModal("Add Foreign Key", `<p class="muted">Create at least one other entity first to link via foreign key.</p>`);
      return;
    }
    openModal(
      `Add Foreign Key to "${entityName}"`,
      `<form id="form-add-fk" class="modal-form">
        <label class="field-label"><span>Target Entity</span>
          <select name="target_entity" id="fk-target-select">${targetOptions}</select>
        </label>
        <label class="field-label"><span>Column Name (auto-named &lt;entity&gt;_id)</span>
          <input name="column_name" id="fk-col-input" required />
        </label>
        <label><input type="checkbox" name="nullable" checked /> Nullable</label>
        <label class="field-label"><span>Default ID (optional; required if NOT NULL)</span><input name="default" /></label>
        <div id="fk-err" class="error-banner hidden"></div>
        <button type="submit" class="btn btn-primary">Add Foreign Key</button>
      </form>`
    );
    const syncFkName = () => {
      $("fk-col-input").value = `${$("fk-target-select").value}_id`;
    };
    syncFkName();
    $("fk-target-select").addEventListener("change", syncFkName);
    $("form-add-fk").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const fd = new FormData(ev.target);
      try {
        await api(`/api/entities/${encodeURIComponent(entityName)}/foreign-keys`, {
          method: "POST",
          body: JSON.stringify({
            target_entity: fd.get("target_entity"),
            column_name: fd.get("column_name").trim(),
            nullable: Boolean(fd.get("nullable")),
            default: fd.get("default").trim() || null,
          }),
        });
        closeModal();
        await loadWorkspace();
      } catch (err) {
        $("fk-err").textContent = err.message;
        $("fk-err").classList.remove("hidden");
      }
    });
  }
}

$("entities-list").addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-action]");
  if (btn) {
    await handleEntityAction(btn.dataset.action, btn.dataset.entity);
    return;
  }
  const row = e.target.closest(".sidebar-item[data-entity-name]");
  if (row) {
    showHoverCardFor(row);
  }
});

hoverCardEl.addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-action][data-entity]");
  if (!btn) return;
  hoverCardEl.classList.add("hidden");
  await handleEntityAction(btn.dataset.action, btn.dataset.entity);
});

// ---------------------------------------------------------------------------
// Lightweight RBAC Permissions Panel
// ---------------------------------------------------------------------------

async function openRbacModal() {
  const snap = await api("/api/rbac");
  const headerCols = snap.roles.map((r) => `<th>${escapeHtml(r)}</th>`).join("");
  const rowsHtml = snap.permissions
    .map((perm) => {
      const cells = snap.roles
        .map((role) => {
          const checked = (snap.grants[role] || []).includes(perm) ? "checked" : "";
          return `<td><input type="checkbox" data-role="${escapeHtml(role)}" data-perm="${escapeHtml(perm)}" ${checked} /></td>`;
        })
        .join("");
      return `<tr><td><code>${escapeHtml(perm)}</code></td>${cells}</tr>`;
    })
    .join("");

  openModal(
    "Roles & Permissions",
    `<div style="display:flex; gap:8px; flex-wrap:wrap;">
      <form id="form-add-role" style="display:flex; gap:6px;">
        <input name="name" placeholder="new_role" required style="padding:5px 8px; background:var(--bg); color:var(--text); border:1px solid var(--border); border-radius:6px;" />
        <button type="submit" class="btn btn-small btn-secondary">+ Role</button>
      </form>
      <form id="form-add-perm" style="display:flex; gap:6px;">
        <input name="name" placeholder="entity:write_attr" required style="padding:5px 8px; background:var(--bg); color:var(--text); border:1px solid var(--border); border-radius:6px;" />
        <button type="submit" class="btn btn-small btn-secondary">+ Permission</button>
      </form>
    </div>
    <table class="rbac-table">
      <thead><tr><th>Permission</th>${headerCols}</tr></thead>
      <tbody>${rowsHtml || '<tr><td colspan="4" class="muted">No permissions defined yet.</td></tr>'}</tbody>
    </table>`
  );

  $("form-add-role").addEventListener("submit", async (e) => {
    e.preventDefault();
    const name = new FormData(e.target).get("name").trim();
    await api("/api/rbac/roles", { method: "POST", body: JSON.stringify({ name }) });
    await openRbacModal();
    await loadWorkspace();
  });

  $("form-add-perm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const name = new FormData(e.target).get("name").trim();
    await api("/api/rbac/permissions", { method: "POST", body: JSON.stringify({ name }) });
    await openRbacModal();
  });

  const rbacTable = $("modal-body").querySelector(".rbac-table");
  rbacTable.addEventListener("change", async (e) => {
    const cb = e.target.closest("input[data-role][data-perm]");
    if (!cb) return;
    const res = await api("/api/rbac/assign", {
      method: "POST",
      body: JSON.stringify({
        role: cb.dataset.role,
        permission: cb.dataset.perm,
        granted: cb.checked,
      }),
    });
    if (Array.isArray(res.synced_forms) && res.synced_forms.length > 0) {
      for (const updated of res.synced_forms) {
        const idx = state.forms.findIndex((f) => f.id === updated.id);
        if (idx !== -1) state.forms[idx] = updated;
        if (updated.id === state.activeFormId && state.activeItem?.kind === "form") {
          if (!state.dirty) {
            $("code-editor").value = updated.fspec_source;
            await renderPreview();
          } else {
            $("diagnostics-bar").textContent =
              "DB permissions updated — save or re-select form to sync @(...) sigils into unsaved edits.";
          }
        }
      }
      renderForms();
    }
  });
}

$("btn-open-rbac").addEventListener("click", openRbacModal);

// ---------------------------------------------------------------------------
// SQL Schema Explorer & Query Panel
// ---------------------------------------------------------------------------

function renderSqlSchemaList(tables) {
  return tables
    .map((t) => {
      const cols = t.columns
        .map((c) => {
          const tags = [];
          if (c.is_pk) tags.push("PK");
          if (c.fk_table) {
            tags.push(
              `<span class="fk-tag">&rarr; ${escapeHtml(c.fk_table)}(${escapeHtml(c.fk_column || "id")})</span>`
            );
          }
          return `<li><code>${escapeHtml(c.name)}</code> <span class="muted">${escapeHtml(c.sql_type)}</span> ${tags.join(" ")}</li>`;
        })
        .join("");

      return `
        <div class="sql-table-card">
          <div class="sql-table-header" data-sql-table="${escapeHtml(t.name)}" title="Click to query ${escapeHtml(t.name)}">
            <strong>${escapeHtml(t.name)}</strong>
            <span class="badge">${t.is_system ? "rbac/sys" : "entity"} • ${t.row_count}</span>
          </div>
          <ul class="attr-list">${cols}</ul>
        </div>
      `;
    })
    .join("");
}

async function openSqlModal() {
  const initial = await api("/api/sql/tables");
  const defaultSql =
    "SELECT r.name AS role, p.name AS permission\n" +
    "FROM role_permissions rp\n" +
    "JOIN roles r ON r.id = rp.role_id\n" +
    "JOIN permissions p ON p.id = rp.permission_id\n" +
    "ORDER BY r.name, p.name;";

  openModal(
    "SQL Explorer & Query Runner",
    `<div class="sql-layout">
      <div class="sql-schema-pane">
        <div class="panel-header"><h2>All Tables &amp; Relations</h2></div>
        <div id="sql-schema-list">${renderSqlSchemaList(initial.tables)}</div>
      </div>
      <div class="sql-query-pane">
        <div class="sql-toolbar">
          <div class="sql-presets">
            <button type="button" class="btn btn-small btn-secondary" data-preset="roles_perms">Role Permissions</button>
            <button type="button" class="btn btn-small btn-secondary" data-preset="user_roles">User Roles</button>
            <button type="button" class="btn btn-small btn-secondary" data-preset="entities">Entities</button>
            <button type="button" class="btn btn-small btn-secondary" data-preset="forms">Forms</button>
          </div>
          <button type="button" id="btn-run-sql" class="btn btn-primary">Run Query (Ctrl+Enter)</button>
        </div>
        <textarea id="sql-editor" class="sql-editor" spellcheck="false">${escapeHtml(defaultSql)}</textarea>
        <div id="sql-error" class="error-banner hidden"></div>
        <div id="sql-meta" class="muted"></div>
        <div id="sql-results" class="sql-results-wrap"></div>
      </div>
    </div>`,
    { wide: true }
  );

  const presets = {
    roles_perms: defaultSql,
    user_roles:
      "SELECT u.id, u.username, u.is_superuser, r.name AS role\n" +
      "FROM users u\n" +
      "LEFT JOIN user_roles ur ON ur.user_id = u.id\n" +
      "LEFT JOIN roles r ON r.id = ur.role_id\n" +
      "ORDER BY u.id;",
    entities: "SELECT id, name, label, description FROM entity ORDER BY name;",
    forms: "SELECT id, name, label, form_type, entity_id, formatter, theme FROM forms ORDER BY name;",
  };

  async function runQuery() {
    $("sql-error").classList.add("hidden");
    const sql = $("sql-editor").value;
    try {
      const res = await api("/api/sql/query", {
        method: "POST",
        body: JSON.stringify({ sql }),
      });
      if (res.tables) {
        $("sql-schema-list").innerHTML = renderSqlSchemaList(res.tables);
      }
      if (res.columns && res.columns.length > 0) {
        const thead = `<tr>${res.columns.map((c) => `<th>${escapeHtml(c)}</th>`).join("")}</tr>`;
        const tbody = res.rows
          .map(
            (row) =>
              `<tr>${row
                .map((cell) =>
                  cell === null
                    ? '<td><span class="sql-null">NULL</span></td>'
                    : `<td>${escapeHtml(cell)}</td>`
                )
                .join("")}</tr>`
          )
          .join("");
        $("sql-meta").textContent = `${res.row_count} row(s)${res.truncated ? " (truncated at 500)" : ""}`;
        $("sql-results").innerHTML = `<table class="rbac-table"><thead>${thead}</thead><tbody>${tbody}</tbody></table>`;
      } else {
        $("sql-meta").textContent = res.message || "Query executed.";
        $("sql-results").innerHTML = `<div class="muted" style="padding:10px;">${escapeHtml(res.message || "OK")}</div>`;
        await loadWorkspace();
      }
    } catch (err) {
      $("sql-error").textContent = err.message;
      $("sql-error").classList.remove("hidden");
    }
  }

  $("btn-run-sql").addEventListener("click", runQuery);
  $("sql-editor").addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
      e.preventDefault();
      runQuery();
    }
  });
  $("modal-body").querySelector(".sql-layout").addEventListener("click", (e) => {
    const tbl = e.target.closest("[data-sql-table]");
    if (tbl) {
      const name = tbl.dataset.sqlTable.replaceAll('"', '""');
      $("sql-editor").value = `SELECT * FROM "${name}" LIMIT 100;`;
      runQuery();
      return;
    }
    const presetBtn = e.target.closest("[data-preset]");
    if (presetBtn) {
      $("sql-editor").value = presets[presetBtn.dataset.preset] || defaultSql;
      runQuery();
    }
  });

  await runQuery();
}

$("btn-open-sql").addEventListener("click", openSqlModal);

// Prevent + buttons inside collapsible <summary> headers from toggling <details>
for (const btn of document.querySelectorAll("summary.panel-header button")) {
  btn.addEventListener("click", (e) => e.stopPropagation());
}

// ---------------------------------------------------------------------------
// Server Modules, Server Actions, Client Modules & Client Actions (Unified Editor)
// ---------------------------------------------------------------------------

function openCreateServerModuleModal() {
  openModal(
    "Create Server Module",
    `<form id="form-new-sm" class="modal-form">
      <label class="field-label"><span>Module Name (snake_case)</span><input name="name" placeholder="custom_validators" required /></label>
      <label class="field-label"><span>Display Label</span><input name="label" placeholder="Custom Validators" required /></label>
      <div id="sm-err" class="error-banner hidden"></div>
      <button type="submit" class="btn btn-primary">Create Server Module</button>
    </form>`
  );
  $("form-new-sm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const name = fd.get("name").trim();
    const label = fd.get("label").trim();
    try {
      const res = await api("/api/server-modules", {
        method: "POST",
        body: JSON.stringify({
          name,
          label,
          target_runtime: "node",
          code: `/**\n * Shared server module \`${name}\`.\n */\n\nexport function assertValid(condition, message) {\n  if (!condition) throw new Error(message);\n}\n`,
        }),
      });
      closeModal();
      await loadWorkspace();
      selectEditorItem("server_module", res.server_module.id);
    } catch (err) {
      $("sm-err").textContent = err.message;
      $("sm-err").classList.remove("hidden");
    }
  });
}

function openCreateServerActionModal() {
  const entOptions = ['<option value="">(None)</option>']
    .concat(
      state.entities.map(
        (e) => `<option value="${escapeHtml(e.name)}">${escapeHtml(e.name)}</option>`
      )
    )
    .join("");
  openModal(
    "Create Server Action",
    `<form id="form-new-sa" class="modal-form">
      <label class="field-label"><span>Action Name (snake_case)</span><input name="name" placeholder="custom_action" required /></label>
      <label class="field-label"><span>Display Label</span><input name="label" placeholder="Custom Action" required /></label>
      <label class="field-label"><span>Entity (optional)</span><select name="entity_name">${entOptions}</select></label>
      <div id="sa-err" class="error-banner hidden"></div>
      <button type="submit" class="btn btn-primary">Create Server Action</button>
    </form>`
  );
  $("form-new-sa").addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const name = fd.get("name").trim();
    const label = fd.get("label").trim();
    try {
      const res = await api("/api/server-actions", {
        method: "POST",
        body: JSON.stringify({
          name,
          label,
          entity_name: fd.get("entity_name") || null,
          action_type: "custom",
          target_runtime: "node",
          code:
            `/**\n * Server action \`${name}\`.\n */\n` +
            `import { ValidationError } from "./formspec_server.mjs";\n\n` +
            `export async function upsertRecord(db, userId, payload) {\n` +
            `  if (!payload) throw new ValidationError("Missing payload.");\n` +
            `  return { status: "ok", action: "custom", id: payload.id ?? null };\n` +
            `}\n`,
        }),
      });
      closeModal();
      await loadWorkspace();
      selectEditorItem("server_action", res.server_action.id);
    } catch (err) {
      $("sa-err").textContent = err.message;
      $("sa-err").classList.remove("hidden");
    }
  });
}

function openCreateClientModuleModal() {
  openModal(
    "Create Client Module",
    `<form id="form-new-cm" class="modal-form">
      <label class="field-label"><span>Module Name (snake_case)</span><input name="name" placeholder="ui_validators" required /></label>
      <label class="field-label"><span>Display Label</span><input name="label" placeholder="UI Validators" required /></label>
      <div id="cm-err" class="error-banner hidden"></div>
      <button type="submit" class="btn btn-primary">Create Client Module</button>
    </form>`
  );
  $("form-new-cm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const name = fd.get("name").trim();
    const label = fd.get("label").trim();
    try {
      const res = await api("/api/client-modules", {
        method: "POST",
        body: JSON.stringify({
          name,
          label,
          code: `/**\n * Shared client module \`${name}\`.\n */\n\nexport function assertMinLength(val, min, label = "Field") {\n  if (String(val ?? "").trim().length < min) {\n    throw new Error(\`${label} must be at least ${min} characters.\`);\n  }\n}\n`,
        }),
      });
      closeModal();
      await loadWorkspace();
      selectEditorItem("client_module", res.client_module.id);
    } catch (err) {
      $("cm-err").textContent = err.message;
      $("cm-err").classList.remove("hidden");
    }
  });
}

function openCreateClientActionModal() {
  const formOptions = ['<option value="">(None)</option>']
    .concat(
      state.forms.map(
        (f) => `<option value="${escapeHtml(f.name)}">${escapeHtml(f.label)} (${escapeHtml(f.name)})</option>`
      )
    )
    .join("");
  const saOptions = ['<option value="">(None)</option>']
    .concat(
      state.serverActions.map(
        (sa) => `<option value="${escapeHtml(sa.name)}">${escapeHtml(sa.label)} (${escapeHtml(sa.name)})</option>`
      )
    )
    .join("");

  openModal(
    "Create Client Action",
    `<form id="form-new-ca" class="modal-form">
      <label class="field-label"><span>Action Name (snake_case)</span><input name="name" placeholder="validate_and_submit" required /></label>
      <label class="field-label"><span>Display Label</span><input name="label" placeholder="Validate & Submit" required /></label>
      <label class="field-label"><span>Server Action to Initiate (optional)</span><select name="server_action_name">${saOptions}</select></label>
      <label class="field-label"><span>Target Form (optional navigation)</span><select name="target_form_name">${formOptions}</select></label>
      <div id="ca-err" class="error-banner hidden"></div>
      <button type="submit" class="btn btn-primary">Create Client Action</button>
    </form>`
  );
  $("form-new-ca").addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const name = fd.get("name").trim();
    const label = fd.get("label").trim();
    const saName = fd.get("server_action_name");
    const targetForm = fd.get("target_form_name") || null;
    const starterCode = saName
      ? `import { invokeServerAction } from "./formspec_client.mjs";\n\nexport async function run(ctx) {\n  // Perform client-side checks here\n  return await invokeServerAction(ctx, ${JSON.stringify(saName)}, ctx.payload || {});\n}\n`
      : `import { navigateToForm } from "./formspec_client.mjs";\n\nexport async function run(ctx) {\n  return navigateToForm(ctx, ${JSON.stringify(targetForm || "")}, ctx.params || {});\n}\n`;
    try {
      const res = await api("/api/client-actions", {
        method: "POST",
        body: JSON.stringify({
          name,
          label,
          action_type: saName ? "submit" : "link",
          target_form_name: targetForm,
          params_spec: targetForm ? '{"id": "$selected.id"}' : null,
          code: starterCode,
        }),
      });
      closeModal();
      await loadWorkspace();
      selectEditorItem("client_action", res.client_action.id);
    } catch (err) {
      $("ca-err").textContent = err.message;
      $("ca-err").classList.remove("hidden");
    }
  });
}

function openClientActionMetaModal(ca) {
  const formOptions = ['<option value="">(None)</option>']
    .concat(
      state.forms.map(
        (f) =>
          `<option value="${escapeHtml(f.name)}" ${ca.target_form_name === f.name ? "selected" : ""}>${escapeHtml(f.label)} (${escapeHtml(f.name)})</option>`
      )
    )
    .join("");
  openModal(
    `Client Action Settings: ${ca.name}`,
    `<form id="form-ca-meta" class="modal-form">
      <label class="field-label"><span>Display Label</span><input name="label" value="${escapeHtml(ca.label || "")}" required /></label>
      <label class="field-label"><span>Target Form</span><select name="target_form_name">${formOptions}</select></label>
      <label class="field-label"><span>Parameter Mapping (JSON)</span><input name="params_spec" value="${escapeHtml(ca.params_spec || '{"id": "$selected.id"}')}" /></label>
      <div id="ca-meta-err" class="error-banner hidden"></div>
      <button type="submit" class="btn btn-primary">Update Settings</button>
    </form>`
  );
  $("form-ca-meta").addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    try {
      const res = await api(`/api/client-actions/${ca.id}`, {
        method: "PUT",
        body: JSON.stringify({
          label: fd.get("label").trim(),
          target_form_name: fd.get("target_form_name") || null,
          params_spec: fd.get("params_spec").trim() || null,
        }),
      });
      closeModal();
      await loadWorkspace();
      selectEditorItem("client_action", res.client_action.id);
    } catch (err) {
      $("ca-meta-err").textContent = err.message;
      $("ca-meta-err").classList.remove("hidden");
    }
  });
}

$("btn-new-server-module").addEventListener("click", async () => {
  if (await confirmUnsavedChanges("new server module")) openCreateServerModuleModal();
});
$("btn-new-server-action").addEventListener("click", async () => {
  if (await confirmUnsavedChanges("new server action")) openCreateServerActionModal();
});
$("btn-new-client-module").addEventListener("click", async () => {
  if (await confirmUnsavedChanges("new client module")) openCreateClientModuleModal();
});
$("btn-new-client-action").addEventListener("click", async () => {
  if (await confirmUnsavedChanges("new client action")) openCreateClientActionModal();
});

$("btn-item-meta").addEventListener("click", () => {
  if (state.activeItem?.kind !== "client_action") return;
  const ca = findItem("client_action", state.activeItem.id);
  if (ca) openClientActionMetaModal(ca);
});

$("server-modules-list").addEventListener("click", async (e) => {
  const item = e.target.closest("[data-server-module-id]");
  if (item) await switchEditorItem("server_module", Number(item.dataset.serverModuleId));
});

$("server-actions-list").addEventListener("click", async (e) => {
  const item = e.target.closest("[data-server-action-id]");
  if (item) await switchEditorItem("server_action", Number(item.dataset.serverActionId));
});

$("client-modules-list").addEventListener("click", async (e) => {
  const item = e.target.closest("[data-client-module-id]");
  if (item) await switchEditorItem("client_module", Number(item.dataset.clientModuleId));
});

$("client-actions-list").addEventListener("click", async (e) => {
  const item = e.target.closest("[data-client-action-id]");
  if (item) await switchEditorItem("client_action", Number(item.dataset.clientActionId));
});

// ---------------------------------------------------------------------------
// Interactive Mermaid Form Navigation Map
// ---------------------------------------------------------------------------

function buildMapSvg(graph) {
  const nodes = graph.nodes || [];
  const edges = graph.edges || [];
  if (nodes.length === 0) {
    return `<div class="muted">No forms defined yet.</div>`;
  }

  const adj = new Map(nodes.map((n) => [n.name, []]));
  for (const e of edges) {
    if (adj.has(e.from) && adj.has(e.to)) adj.get(e.from).push(e.to);
  }

  // Topological BFS layers anchored at `home`
  const rank = new Map();
  const queue = [];
  if (nodes.some((n) => n.name === "home")) {
    rank.set("home", 0);
    queue.push("home");
  } else {
    rank.set(nodes[0].name, 0);
    queue.push(nodes[0].name);
  }
  while (queue.length > 0) {
    const cur = queue.shift();
    const d = rank.get(cur);
    for (const nxt of adj.get(cur) || []) {
      if (!rank.has(nxt)) {
        rank.set(nxt, d + 1);
        queue.push(nxt);
      }
    }
  }
  let maxRank = Math.max(0, ...rank.values());
  const orphanStart = maxRank + 1;
  let placedOrphan = false;
  for (const n of nodes) {
    if (!rank.has(n.name)) {
      rank.set(n.name, orphanStart);
      placedOrphan = true;
    }
  }
  if (placedOrphan) maxRank = orphanStart;

  const layers = new Map();
  for (const n of nodes) {
    const r = rank.get(n.name) ?? 0;
    if (!layers.has(r)) layers.set(r, []);
    layers.get(r).push(n);
  }

  const nodeW = 200;
  const nodeH = 56;
  const colGap = 100;
  const rowGap = 42;
  const padX = 36;
  const padY = 36;
  const maxRows = Math.max(1, ...Array.from(layers.values()).map((arr) => arr.length));
  const svgW = Math.max(540, padX * 2 + (maxRank + 1) * nodeW + maxRank * colGap);
  const svgH = Math.max(240, padY * 2 + maxRows * nodeH + (maxRows - 1) * rowGap);

  const pos = new Map();
  for (let r = 0; r <= maxRank; r++) {
    const colNodes = layers.get(r) || [];
    const totalColH = colNodes.length * nodeH + Math.max(0, colNodes.length - 1) * rowGap;
    const startY = Math.max(padY, (svgH - totalColH) / 2);
    colNodes.forEach((n, idx) => {
      const x = padX + r * (nodeW + colGap);
      const y = startY + idx * (nodeH + rowGap);
      pos.set(n.name, { x, y, w: nodeW, h: nodeH, node: n });
    });
  }

  const edgeSvg = edges
    .map((e, i) => {
      const src = pos.get(e.from);
      const dst = pos.get(e.to);
      if (!src || !dst) return "";
      let d;
      let lx;
      let ly;
      if (src === dst) {
        const cx = src.x + src.w / 2;
        const topY = src.y;
        d = `M ${cx - 24} ${topY} C ${cx - 24} ${topY - 38}, ${cx + 24} ${topY - 38}, ${cx + 24} ${topY}`;
        lx = cx;
        ly = topY - 32;
      } else if (src.x <= dst.x) {
        const x1 = src.x + src.w;
        const y1 = src.y + src.h / 2;
        const x2 = dst.x;
        const y2 = dst.y + dst.h / 2;
        const dx = Math.max(40, (x2 - x1) * 0.5);
        d = `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`;
        lx = (x1 + x2) / 2;
        ly = (y1 + y2) / 2 - 8 + ((i % 2 === 0 ? -1 : 1) * 8);
      } else {
        const x1 = src.x;
        const y1 = src.y + src.h / 2;
        const x2 = dst.x + dst.w;
        const y2 = dst.y + dst.h / 2;
        const bend = 40 + (i % 3) * 18;
        const midY = Math.max(y1, y2) + bend;
        d = `M ${x1} ${y1} C ${x1 - 40} ${midY}, ${x2 + 40} ${midY}, ${x2} ${y2}`;
        lx = (x1 + x2) / 2;
        ly = midY - 4;
      }
      const dash = e.kind === "embed" ? ' stroke-dasharray="5,4"' : "";
      const shortLbl = e.label.length > 26 ? `${e.label.slice(0, 23)}...` : e.label;
      return `
        <g class="map-edge">
          <path d="${d}" fill="none" stroke="#60a5fa" stroke-width="1.8"${dash} marker-end="url(#map-arrow)" />
          <text x="${lx}" y="${ly}" text-anchor="middle" fill="#cbd5e1" font-size="11">${escapeHtml(shortLbl)}</text>
        </g>`;
    })
    .join("");

  const nodeSvg = nodes
    .map((n) => {
      const p = pos.get(n.name);
      if (!p) return "";
      const isCurrent = state.activeItem?.kind === "form" && state.activeItem?.id === n.id;
      const rx = n.is_home ? 24 : 8;
      const stroke = isCurrent ? "#f59e0b" : n.is_home ? "#22c55e" : "#3b82f6";
      const fill = n.is_home ? "#0f2a24" : "#1e293b";
      const badge = n.is_home ? "entry" : n.form_type || "detail";
      return `
        <g class="map-node" data-map-form="${escapeHtml(n.name)}">
          <rect x="${p.x}" y="${p.y}" width="${p.w}" height="${p.h}" rx="${rx}" fill="${fill}" stroke="${stroke}" stroke-width="${isCurrent ? 2.5 : 1.6}" />
          <text x="${p.x + p.w / 2}" y="${p.y + 23}" text-anchor="middle" fill="#f8fafc" font-size="13" font-weight="600">${escapeHtml(n.label)}</text>
          <text x="${p.x + p.w / 2}" y="${p.y + 42}" text-anchor="middle" fill="#94a3b8" font-size="11">${escapeHtml(n.name)} • ${escapeHtml(badge)}</text>
        </g>`;
    })
    .join("");

  return `
    <svg width="${svgW}" height="${svgH}" viewBox="0 0 ${svgW} ${svgH}" xmlns="http://www.w3.org/2000/svg">
      <defs>
        <marker id="map-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
          <path d="M 0 1 L 10 5 L 0 9 z" fill="#60a5fa" />
        </marker>
      </defs>
      ${edgeSvg}
      ${nodeSvg}
    </svg>`;
}

async function openFormMapModal() {
  const graph = await api("/api/forms/map");
  openModal(
    "Form Navigation Map (Mermaid)",
    `<div class="map-toolbar">
      <span class="muted">Click any form node to open it in the editor. Links include buttons, list name links, embeds, and client actions.</span>
      <button type="button" id="btn-copy-mermaid" class="btn btn-small btn-secondary">Copy Mermaid</button>
    </div>
    <div id="map-svg-container" class="map-svg-wrap">${buildMapSvg(graph)}</div>
    <details>
      <summary class="muted" style="cursor:pointer;">Mermaid Flowchart Source (<code>flowchart LR</code>)</summary>
      <pre id="map-mermaid-source" class="map-mermaid-src">${escapeHtml(graph.mermaid || "")}</pre>
    </details>`,
    { wide: true }
  );

  $("btn-copy-mermaid").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(graph.mermaid || "");
      $("btn-copy-mermaid").textContent = "Copied!";
    } catch (_) {
      $("btn-copy-mermaid").textContent = "Copy ready below";
    }
  });

  $("map-svg-container").addEventListener("click", async (e) => {
    const nodeEl = e.target.closest("[data-map-form]");
    if (!nodeEl) return;
    const formName = nodeEl.dataset.mapForm;
    const targetForm = state.forms.find((f) => f.name === formName);
    if (!targetForm) return;
    closeModal();
    await switchEditorItem("form", targetForm.id);
  });
}

$("btn-open-map").addEventListener("click", openFormMapModal);

// ---------------------------------------------------------------------------
// Git Branch Controller, Publish to DB & Commit/Push
// ---------------------------------------------------------------------------

function renderGitControls() {
  const sel = $("git-branch-select");
  const btnPub = $("btn-git-publish");
  const btnPush = $("btn-git-push");
  const git = state.git || {};

  if (!git.configured) {
    sel.innerHTML = `<option value="">Git: unconfigured</option>`;
    sel.disabled = true;
    btnPub.disabled = true;
    btnPush.disabled = true;
    return;
  }

  const branches = git.branches?.length
    ? git.branches
    : git.current_branch
    ? [git.current_branch]
    : ["main"];
  sel.innerHTML = branches
    .map((b) => `<option value="${escapeHtml(b)}">${escapeHtml(b)}</option>`)
    .join("");
  if (git.current_branch && branches.includes(git.current_branch)) {
    sel.value = git.current_branch;
  }
  sel.disabled = !git.cloned;
  btnPub.disabled = !git.cloned;
  btnPush.disabled = !git.cloned;
}

$("git-branch-select").addEventListener("change", async (e) => {
  const branch = e.target.value;
  if (!branch) return;
  try {
    const res = await api("/api/git/checkout", {
      method: "POST",
      body: JSON.stringify({ branch }),
    });
    state.git = res.git;
    renderGitControls();
    $("diagnostics-bar").classList.remove("has-errors");
    $("diagnostics-bar").textContent = `Switched Git workspace to branch '${branch}' (click Publish Branch to apply to DB)`;
  } catch (err) {
    renderGitControls();
    $("diagnostics-bar").classList.add("has-errors");
    $("diagnostics-bar").textContent = err.message;
  }
});

$("btn-git-publish").addEventListener("click", async () => {
  if (!(await confirmUnsavedChanges("published branch contents"))) return;
  const branch = state.git?.current_branch || "current branch";
  try {
    const res = await api("/api/git/publish", { method: "POST", body: "{}" });
    state.git = res.git || state.git;
    renderGitControls();
    await loadWorkspace();
    const counts = res.result?.published || {};
    $("diagnostics-bar").classList.remove("has-errors");
    $("diagnostics-bar").textContent =
      `Published branch '${branch}' to DB (${counts.forms ?? 0} forms, ${counts.server_actions ?? 0} server actions, ${counts.client_actions ?? 0} client actions)`;
  } catch (err) {
    $("diagnostics-bar").classList.add("has-errors");
    $("diagnostics-bar").textContent = err.message;
  }
});

$("btn-git-push").addEventListener("click", async () => {
  if (!(await confirmUnsavedChanges("Git commit & push"))) return;
  const branch = state.git?.current_branch || "main";
  openModal(
    `Commit & Push Branch (${branch})`,
    `<form id="form-git-push" class="modal-form">
      <div class="muted">Exports entities, RBAC, forms, actions, and modules from the database to branch <code>${escapeHtml(branch)}</code> and pushes to origin.</div>
      <label class="field-label"><span>Commit Message</span><input name="message" value="Update FormSpec workspace" required /></label>
      <div id="git-push-err" class="error-banner hidden"></div>
      <button type="submit" class="btn btn-primary">Commit &amp; Push</button>
    </form>`
  );
  $("form-git-push").addEventListener("submit", async (e) => {
    e.preventDefault();
    const message = new FormData(e.target).get("message").trim();
    try {
      const res = await api("/api/git/push", {
        method: "POST",
        body: JSON.stringify({ message }),
      });
      state.git = res.status || state.git;
      renderGitControls();
      closeModal();
      $("diagnostics-bar").classList.remove("has-errors");
      $("diagnostics-bar").textContent = `Pushed branch '${res.branch}' to origin`;
    } catch (err) {
      $("git-push-err").textContent = err.message;
      $("git-push-err").classList.remove("hidden");
    }
  });
});

// ---------------------------------------------------------------------------
// Package Standalone Web App & Multi-RDBMS Init Script
// ---------------------------------------------------------------------------

async function openPackageModal() {
  const { dialects } = await api("/api/package/dialects");
  const dialectOptions = dialects
    .map((d) => `<option value="${escapeHtml(d.id)}">${escapeHtml(d.label)} — ${escapeHtml(d.description)}</option>`)
    .join("");

  openModal(
    "Package Standalone Web App",
    `<form id="form-package-app" class="modal-form">
      <div class="muted">Creates a Docker container bundle of the standalone end-user web app (not the IDE) with a launch-time empty-DB auto-init script. Defaults to SQLite unless <code>DATABASE_CONNECTION_STRING</code> is provided.</div>
      <label class="field-label"><span>Target Database Dialect for Init Script</span>
        <select name="dialect">${dialectOptions}</select>
      </label>
      <label class="field-label"><span>Application / Image Name</span>
        <input name="app_name" value="formspec-app" required />
      </label>
      <label><input type="checkbox" name="include_data" /> Include current domain entity rows in seed script</label>
      <label><input type="checkbox" name="build_docker" /> Run <code>docker build</code> on server if Docker daemon is available</label>
      <div id="pkg-err" class="error-banner hidden"></div>
      <button type="submit" class="btn btn-primary">Generate Package &amp; Init Script</button>
    </form>
    <div id="pkg-result" class="hidden" style="display:flex; flex-direction:column; gap:10px;"></div>`,
    { wide: true }
  );

  $("form-package-app").addEventListener("submit", async (e) => {
    e.preventDefault();
    $("pkg-err").classList.add("hidden");
    const fd = new FormData(e.target);
    try {
      const res = await api("/api/package", {
        method: "POST",
        body: JSON.stringify({
          dialect: fd.get("dialect"),
          app_name: fd.get("app_name").trim(),
          include_data: Boolean(fd.get("include_data")),
          build_docker: Boolean(fd.get("build_docker")),
        }),
      });
      const resultEl = $("pkg-result");
      resultEl.classList.remove("hidden");
      resultEl.innerHTML = `
        <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:8px;">
          <div>
            <strong>${escapeHtml(res.image_tag)}</strong> • ${escapeHtml(res.dialect_label)}
            <span class="badge">${escapeHtml(res.init_sql_filename)}</span>
          </div>
          <a href="${escapeHtml(res.download_url)}" class="btn btn-primary" download>Download Docker Bundle (.zip)</a>
        </div>
        <div class="muted">Docker status: ${escapeHtml(res.docker_log)}</div>
        <details open>
          <summary class="muted" style="cursor:pointer;">Generated <code>${escapeHtml(res.init_sql_filename)}</code></summary>
          <pre class="code-export">${escapeHtml(res.init_sql)}</pre>
        </details>
      `;
    } catch (err) {
      $("pkg-err").textContent = err.message;
      $("pkg-err").classList.remove("hidden");
    }
  });
}

$("btn-open-package").addEventListener("click", openPackageModal);

checkAuth();
