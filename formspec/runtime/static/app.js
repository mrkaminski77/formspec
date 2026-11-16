const appState = {
  user: null,
  supportsLogin: true,
  forms: [],
  clientActions: {},
  clientModules: [],
  activeFormName: "home",
  params: {},
  recordId: null,
  filters: {},
  search: "",
  breadcrumbs: [],
  selectedRow: null,
};

const $ = (id) => document.getElementById(id);

async function api(path, options = {}) {
  const res = await fetch(path, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
  return data;
}

function showBanner(msg, isError = false) {
  const el = $("status-banner");
  if (!msg) {
    el.classList.add("hidden");
    return;
  }
  el.textContent = msg;
  el.classList.toggle("error", isError);
  el.classList.remove("hidden");
}

async function withClientModule(sourceCode, fn) {
  const urls = [];
  const modMap = new Map();
  for (const cm of appState.clientModules) {
    const url = URL.createObjectURL(new Blob([cm.code], { type: "text/javascript" }));
    urls.push(url);
    modMap.set(`./${cm.name}.mjs`, url);
    modMap.set(`./${cm.name}.js`, url);
    modMap.set(`./${cm.name}`, url);
  }
  const rewritten = sourceCode.replace(
    /(from\s+["'])(\.\/[^"']+)(["'])/g,
    (full, pre, spec, suf) => (modMap.has(spec) ? `${pre}${modMap.get(spec)}${suf}` : full)
  );
  const entry = URL.createObjectURL(new Blob([rewritten], { type: "text/javascript" }));
  urls.push(entry);
  try {
    return await fn(await import(entry));
  } finally {
    for (const u of urls) URL.revokeObjectURL(u);
  }
}

async function renderHeader(formInfo) {
  const modRec = appState.clientModules.find((m) => m.name === "formspec_header");
  if (!modRec?.code) {
    $("app-header-slot").innerHTML = "";
    return;
  }
  const html = await withClientModule(modRec.code, async (mod) => {
    if (typeof mod.renderAppHeader !== "function") return "";
    return String(
      await mod.renderAppHeader({
        formName: formInfo.name,
        formLabel: formInfo.label,
        formType: formInfo.form_type,
        breadcrumbs: appState.breadcrumbs,
        role: appState.user?.effective_role || "anon",
        recordId: appState.recordId,
      })
    );
  });
  $("app-header-slot").innerHTML = html;
}

function updateBreadcrumbs(formInfo, opts = {}) {
  const entry = {
    name: formInfo.name,
    label: formInfo.label,
    params: { ...(appState.params || {}) },
    recordId: appState.recordId ?? null,
  };
  if (typeof opts.fromBreadcrumbIndex === "number") {
    appState.breadcrumbs = appState.breadcrumbs.slice(0, opts.fromBreadcrumbIndex + 1);
    appState.breadcrumbs[appState.breadcrumbs.length - 1] = entry;
    return;
  }
  if (formInfo.name === "home") {
    appState.breadcrumbs = [entry];
    return;
  }
  const existing = appState.breadcrumbs.findIndex(
    (b) => b.name === entry.name && (b.recordId ?? null) === (entry.recordId ?? null)
  );
  if (existing !== -1) {
    appState.breadcrumbs = appState.breadcrumbs.slice(0, existing + 1);
    appState.breadcrumbs[existing] = entry;
  } else {
    if (appState.breadcrumbs.length === 0) {
      const home = appState.forms.find((f) => f.name === "home");
      if (home) appState.breadcrumbs.push({ name: "home", label: home.label, params: {}, recordId: null });
    }
    appState.breadcrumbs.push(entry);
  }
}

function collectPayload(form) {
  const payload = {};
  for (const el of form.querySelectorAll("input[name], select[name], textarea[name]")) {
    if (el.disabled && el.type !== "hidden") continue;
    if (el.type === "checkbox") payload[el.name] = el.checked;
    else if (el.type === "number") payload[el.name] = el.value === "" ? null : Number(el.value);
    else payload[el.name] = el.value === "" ? null : el.value;
  }
  return payload;
}

function resolveParamMap(form, rawJson, rowObj) {
  let spec = {};
  try { spec = rawJson ? JSON.parse(rawJson) : {}; } catch (_) {}
  const resolved = {};
  const formValues = {};
  for (const el of form.querySelectorAll("input[name], select[name], textarea[name]")) {
    formValues[el.name] = el.type === "checkbox" ? el.checked : el.value;
  }
  for (const [k, expr] of Object.entries(spec)) {
    if (typeof expr === "string" && expr.startsWith("$row.")) resolved[k] = rowObj ? rowObj[expr.slice(5)] : null;
    else if (typeof expr === "string" && expr.startsWith("$selected.")) resolved[k] = appState.selectedRow ? appState.selectedRow[expr.slice(10)] : null;
    else if (typeof expr === "string" && expr.startsWith("$")) resolved[k] = formValues[expr.slice(1)] ?? null;
    else resolved[k] = expr;
  }
  return resolved;
}

async function openForm(formName, params = {}, opts = {}) {
  appState.activeFormName = formName;
  appState.params = params || {};
  appState.recordId = params?.id ? Number(params.id) : null;
  if (!opts.keepFilters) {
    appState.filters = {};
    appState.search = "";
  }
  appState.selectedRow = null;
  await renderCurrentForm(opts);
}

async function runClientAction(actionName, payload = {}, params = {}, formMeta = {}) {
  const ca = appState.clientActions[actionName];
  if (!ca) throw new Error(`Client action '${actionName}' not found.`);
  const ctx = {
    payload,
    params,
    selected: params,
    role: appState.user?.effective_role || "anon",
    entity: formMeta.entity_name || null,
    form: formMeta.name || null,
    serverAction: async (saName, saPayload) => {
      const res = await api("/api/app/submit", {
        method: "POST",
        body: JSON.stringify({
          server_action: saName,
          entity: formMeta.entity_name || null,
          record_id: appState.recordId,
          payload: saPayload,
        }),
      });
      appState.recordId = res.id;
      appState.params = { ...appState.params, id: res.id };
      return res;
    },
    navigate: async (target, navParams = {}) => openForm(target, navParams),
  };

  if (ca.code && ca.code.trim()) {
    const res = await withClientModule(ca.code, async (mod) => mod.run(ctx));
    if (res && res.status === "ok" && res.id !== undefined) {
      showBanner(`Saved (${res.action}) #${res.id}`, false);
      await renderCurrentForm({ keepFilters: true });
    }
    return res;
  }
  if (ca.target_form_name) {
    await openForm(ca.target_form_name, params);
  }
  return null;
}

async function renderCurrentForm(opts = {}) {
  try {
    const res = await api("/api/app/render", {
      method: "POST",
      body: JSON.stringify({
        form_name: appState.activeFormName,
        params: appState.params,
        record_id: appState.recordId,
        filters: appState.filters,
        search: appState.search,
      }),
    });
    $("theme-css").href = `/themes/${encodeURIComponent(res.form.theme || "default")}.css`;
    updateBreadcrumbs(res.form, opts);
    await renderHeader(res.form);
    $("app-form-slot").innerHTML = res.content;

    const form = $("app-form-slot").querySelector("form.fs-form");
    if (!form) return;
    const formType = form.dataset.formType || "detail";

    if (formType === "list") {
      let timer = null;
      const emitFilter = () => {
        const filters = {};
        let search = "";
        for (const el of form.querySelectorAll("input[name], select[name]")) {
          if (el.closest(".fs-list-wrap")) continue;
          if (el.name === "q") search = el.value;
          else if (el.value !== "") filters[el.name] = el.value;
        }
        appState.filters = filters;
        appState.search = search;
        renderCurrentForm({ keepFilters: true });
      };
      form.addEventListener("input", (e) => {
        if (e.target.name === "q") { clearTimeout(timer); timer = setTimeout(emitFilter, 220); }
      });
      form.addEventListener("change", (e) => {
        if (e.target.tagName === "SELECT") emitFilter();
      });
    } else {
      form.addEventListener("submit", async (e) => {
        e.preventDefault();
        try {
          const payload = collectPayload(form);
          const caBtn = form.querySelector("button[data-client-action]");
          const saBtn = form.querySelector("button[data-server-action]");
          if (caBtn) {
            await runClientAction(caBtn.dataset.clientAction, payload, appState.params, res.form);
          } else {
            const sub = await api("/api/app/submit", {
              method: "POST",
              body: JSON.stringify({
                server_action: saBtn ? saBtn.dataset.serverAction : null,
                entity: res.form.entity_name,
                record_id: appState.recordId,
                payload,
              }),
            });
            appState.recordId = sub.id;
            showBanner(`Saved (${sub.action}) #${sub.id}`, false);
            await renderCurrentForm({ keepFilters: true });
          }
        } catch (err) {
          showBanner(err.message, true);
        }
      });
    }

    form.addEventListener("click", async (e) => {
      const tr = e.target.closest("tr.fs-list-row");
      if (tr) {
        for (const o of form.querySelectorAll("tr.fs-list-row.is-selected")) o.classList.remove("is-selected");
        tr.classList.add("is-selected");
        try { appState.selectedRow = JSON.parse(tr.dataset.row || "{}"); } catch (_) {}
      }
      const caEl = e.target.closest("[data-client-action]");
      if (caEl) {
        if (caEl.tagName === "BUTTON" && caEl.type === "submit") return;
        e.preventDefault();
        const rowObj = tr ? JSON.parse(tr.dataset.row || "{}") : appState.selectedRow;
        const params = resolveParamMap(form, caEl.dataset.linkParams, rowObj);
        try {
          await runClientAction(caEl.dataset.clientAction, collectPayload(form), params, res.form);
        } catch (err) {
          showBanner(err.message, true);
        }
        return;
      }
      const linkEl = e.target.closest("[data-link-target]");
      if (linkEl) {
        e.preventDefault();
        const rowObj = tr ? JSON.parse(tr.dataset.row || "{}") : appState.selectedRow;
        const params = resolveParamMap(form, linkEl.dataset.linkParams, rowObj);
        if (params.id === undefined && linkEl.dataset.recordId) params.id = Number(linkEl.dataset.recordId);
        await openForm(linkEl.dataset.linkTarget, params);
      }
    });
  } catch (err) {
    showBanner(err.message, true);
  }
}

$("app-header-slot").addEventListener("click", async (e) => {
  const crumbEl = e.target.closest("[data-breadcrumb-index]");
  if (!crumbEl) return;
  e.preventDefault();
  const idx = Number(crumbEl.dataset.breadcrumbIndex);
  const crumb = appState.breadcrumbs[idx];
  if (!crumb) return;
  await openForm(crumb.name, crumb.params || {}, { fromBreadcrumbIndex: idx });
});

async function bootstrap() {
  const data = await api("/api/app/bootstrap");
  appState.user = data.user;
  appState.supportsLogin = data.supports_login;
  appState.forms = data.forms;
  appState.clientActions = data.client_actions || {};
  appState.clientModules = data.client_modules || [];

  $("user-badge").textContent = `${data.user.username} (${data.user.effective_role})`;
  const authBtn = $("btn-auth");
  authBtn.classList.toggle("hidden", !data.supports_login);
  authBtn.textContent = data.user.authenticated ? "Sign Out" : "Sign In";

  await openForm(data.entry_form || "home");
}

$("btn-auth").addEventListener("click", async () => {
  if (appState.user?.authenticated) {
    await api("/auth/logout", { method: "POST" });
    appState.breadcrumbs = [];
    await bootstrap();
    return;
  }
  $("login-err").classList.add("hidden");
  $("login-modal").classList.remove("hidden");
});

$("login-cancel").addEventListener("click", () => $("login-modal").classList.add("hidden"));
$("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("login-err").classList.add("hidden");
  try {
    await api("/auth/login", {
      method: "POST",
      body: JSON.stringify({
        username: $("login-username").value.trim(),
        password: $("login-password").value,
      }),
    });
    $("login-password").value = "";
    $("login-modal").classList.add("hidden");
    appState.breadcrumbs = [];
    await bootstrap();
  } catch (err) {
    $("login-err").textContent = err.message;
    $("login-err").classList.remove("hidden");
  }
});

bootstrap();
