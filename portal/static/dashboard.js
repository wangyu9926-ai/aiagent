(() => {
  "use strict";

  const state = {
    me: null,
    csrf: "",
    tenants: [],
    users: [],
    sessions: [],
    projects: [],
    events: [],
    lastEventId: 0,
    es: null,
    filters: { tenant: "", type: "", q: "" },
    activeTab: "live",
    currentSession: null,
    lastToken: "",
    toastTimer: null,
    searchTimer: null,
    statsTimer: null,
  };

  const TYPE_META = {
    "llm.pre": { label: "用户输入", cls: "t-llm" },
    "llm.post": { label: "助手回复", cls: "t-llm" },
    "api.pre": { label: "模型请求", cls: "t-api" },
    "api.post": { label: "模型返回", cls: "t-api" },
    "tool.pre": { label: "工具调用", cls: "t-tool" },
    "tool.post": { label: "工具结果", cls: "t-tool" },
    "session.start": { label: "会话开始", cls: "t-session" },
    "session.end": { label: "会话结束", cls: "t-session" },
  };

  const ROLE_LABEL = { admin: "管理员", member: "成员", viewer: "只读" };

  const $ = (id) => document.getElementById(id);

  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined && text !== null) element.textContent = String(text);
    return element;
  }

  function clear(element) {
    while (element.firstChild) element.removeChild(element.firstChild);
  }

  function typeMeta(type) {
    return TYPE_META[type] || { label: type || "事件", cls: "" };
  }

  function formatTime(ts) {
    if (!ts) return "—";
    return new Date(Number(ts) * 1000).toLocaleString("zh-CN", {
      month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
    });
  }

  function shortId(value, size = 22) {
    const text = String(value || "");
    if (text.length <= size) return text;
    return text.slice(0, Math.max(6, size - 8)) + "…" + text.slice(-6);
  }

  function errorText(data, status) {
    if (typeof data?.detail === "string") return data.detail;
    if (Array.isArray(data?.detail)) return data.detail.map((item) => item.msg || "参数错误").join("；");
    if (typeof data?.message === "string") return data.message;
    return `请求失败（HTTP ${status}）`;
  }

  async function api(path, options = {}) {
    const method = (options.method || "GET").toUpperCase();
    const headers = Object.assign({ Accept: "application/json" }, options.headers || {});
    let body = options.body;
    if (body !== undefined && typeof body !== "string") {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(body);
    }
    if (method !== "GET" && state.csrf) headers["X-Portal-CSRF"] = state.csrf;
    const response = await fetch(path, { ...options, method, headers, body, credentials: "same-origin" });
    if (response.status === 401) {
      window.location.href = "/login";
      throw new Error("未登录");
    }
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(errorText(data, response.status));
    return data;
  }

  function showToast(message, kind = "ok") {
    const toast = $("toast");
    toast.textContent = message;
    toast.className = `toast ${kind}`;
    toast.hidden = false;
    if (state.toastTimer) window.clearTimeout(state.toastTimer);
    state.toastTimer = window.setTimeout(() => { toast.hidden = true; }, 3200);
  }

  function setSSE(mode) {
    const dot = $("sse-dot");
    dot.className = "live-dot" + (mode === "on" ? " on" : mode === "off" ? " off" : "");
    $("sse-label").textContent = {
      on: "已连接", connecting: "连接中", off: "已断开", paused: "搜索中暂停",
    }[mode] || "连接中";
  }

  // ---------------------------------------------------------------- navigation

  function switchTab(tab) {
    state.activeTab = tab;
    document.querySelectorAll(".nav-item").forEach((item) => {
      item.classList.toggle("active", item.dataset.tab === tab);
    });
    ["live", "sessions", "projects", "tenants", "users", "approvals", "assets"].forEach((name) => {
      $(`tab-${name}`).hidden = name !== tab;
    });
    if (tab === "live") {
      renderFeed();
      renderLiveSessions();
    }
    if (tab === "sessions") loadSessions();
    if (tab === "projects") loadProjects();
    if (tab === "tenants") loadTenants();
    if (tab === "users") loadUsers();
    if (tab === "approvals") loadApprovals();
    if (tab === "assets") loadAssets();
  }

  // ---------------------------------------------------------------- data loading

  async function loadStats() {
    try {
      const data = await api("/api/stats");
      $("stat-active").textContent = data.active_sessions ?? "—";
      $("stat-events").textContent = data.events_today ?? "—";
      $("stat-tenants").textContent = data.tenants ?? "—";
      const gaps = $("stat-gaps");
      gaps.textContent = data.gaps_today ?? "—";
      gaps.classList.toggle("warn", Number(data.gaps_today) > 0);
    } catch (error) {
      console.warn(error);
    }
  }

  async function loadTenants() {
    try {
      const data = await api("/api/tenants");
      state.tenants = data.tenants || [];
      renderTenantTable();
      renderTenantFilter();
      syncTenantSelectors();
    } catch (error) {
      showToast(error.message, "err");
    }
  }

  function renderTenantFilter() {
    const select = $("filter-tenant");
    const current = select.value;
    clear(select);
    select.append(new Option("全部租户", ""));
    for (const tenant of state.tenants) {
      if (tenant.disabled) continue;
      select.append(new Option(`${tenant.name}（${tenant.id}）`, tenant.id));
    }
    if ([...select.options].some((option) => option.value === current)) select.value = current;
  }

  function fillTenantSelect(select, placeholder) {
    const current = select.value;
    clear(select);
    if (placeholder !== null) select.append(new Option(placeholder, ""));
    for (const tenant of state.tenants) {
      if (tenant.disabled) continue;
      select.append(new Option(`${tenant.name}（${tenant.id}）`, tenant.id));
    }
    if ([...select.options].some((option) => option.value === current)) select.value = current;
  }

  function syncTenantSelectors() {
    fillTenantSelect($("runtime-tenant"), null);
    fillTenantSelect($("user-tenant"), "不授权");
    fillTenantSelect($("grant-tenant"), null);
    fillTenantSelect($("project-tenant"), null);
    const runtimeSelect = $("runtime-tenant");
    if (!runtimeSelect.value && runtimeSelect.options.length) runtimeSelect.selectedIndex = 0;
    if (runtimeSelect.value) loadRuntimeConfig(runtimeSelect.value);
  }

  function renderTenantTable() {
    const tbody = $("tenants-body");
    clear(tbody);
    $("tenants-empty").hidden = state.tenants.length > 0;
    for (const tenant of state.tenants) {
      const row = node("tr", tenant.disabled ? "" : "clickable");
      row.append(node("td", "mono", tenant.id));
      row.append(node("td", "", tenant.name));
      row.append(node("td", "", tenant.event_count ?? 0));
      row.append(node("td", "", tenant.sessions ?? 0));
      row.append(node("td", "", formatTime(tenant.last_ts)));
      const runtimeCell = node("td");
      if (tenant.runtime_base_url) {
        runtimeCell.append(node("span", "badge", "已配置"));
        const detail = tenant.runtime_error
          ? "测试失败"
          : tenant.runtime_ok_at
            ? `测试通过 · ${formatTime(tenant.runtime_ok_at)}`
            : "未测试";
        runtimeCell.append(node("div", "small muted", detail));
        runtimeCell.title = tenant.runtime_base_url;
      } else {
        runtimeCell.append(node("span", "badge danger", "未配置"));
      }
      row.append(runtimeCell);
      const status = node("td");
      status.append(node("span", "badge" + (tenant.disabled ? " danger" : ""), tenant.disabled ? "已停用" : "启用"));
      row.append(status);
      row.addEventListener("click", () => {
        if (tenant.disabled) return;
        $("filter-tenant").value = tenant.id;
        state.filters.tenant = tenant.id;
        switchTab("live");
        applyFilters();
      });
      tbody.append(row);
    }
  }

  async function loadUsers() {
    try {
      const data = await api("/api/users");
      state.users = data.users || [];
      renderUserTable();
      fillGrantUserSelect();
    } catch (error) {
      showToast(error.message, "err");
    }
  }

  function renderUserTable() {
    const tbody = $("users-body");
    clear(tbody);
    $("users-empty").hidden = state.users.length > 0;
    for (const user of state.users) {
      const row = node("tr");
      row.append(node("td", "", user.id));
      row.append(node("td", "mono", user.username));
      row.append(node("td", "", ROLE_LABEL[user.role] || user.role));
      const status = node("td");
      status.append(node("span", "badge" + (user.disabled ? " danger" : ""), user.disabled ? "已停用" : "正常"));
      row.append(status);
      const actions = node("td");
      const button = node("button", "btn btn-sm" + (user.disabled ? "" : " btn-danger"), user.disabled ? "启用" : "停用");
      button.type = "button";
      button.disabled = state.me && user.id === state.me.user.id;
      button.addEventListener("click", async () => {
        button.disabled = true;
        try {
          await api(`/api/users/${user.id}/disable`, { method: "POST", body: { disabled: !user.disabled } });
          showToast(user.disabled ? "用户已启用" : "用户已停用");
          await loadUsers();
        } catch (error) {
          showToast(error.message, "err");
          button.disabled = false;
        }
      });
      actions.append(button);
      row.append(actions);
      tbody.append(row);
    }
  }

  function fillGrantUserSelect() {
    const select = $("grant-user");
    const current = select.value;
    clear(select);
    for (const user of state.users) {
      select.append(new Option(`${user.username}（${ROLE_LABEL[user.role] || user.role}）`, String(user.id)));
    }
    if ([...select.options].some((option) => option.value === current)) select.value = current;
    if (!select.value && select.options.length) select.selectedIndex = 0;
    if (select.value) loadGrants(select.value);
    else $("grant-list").textContent = "";
  }

  async function loadSessions() {
    try {
      const params = new URLSearchParams({ limit: "100" });
      if (state.filters.tenant) params.set("tenant", state.filters.tenant);
      const data = await api(`/api/sessions?${params.toString()}`);
      state.sessions = data.sessions || [];
      renderSessionTable();
      renderLiveSessions();
    } catch (error) {
      showToast(error.message, "err");
    }
  }

  function renderSessionTable() {
    const tbody = $("sessions-body");
    clear(tbody);
    $("sessions-empty").hidden = state.sessions.length > 0;
    for (const session of state.sessions) {
      const meta = typeMeta(session.last_type);
      const row = node("tr", "clickable");
      row.dataset.tenant = session.tenant;
      row.dataset.session = session.session_id;
      row.append(node("td", "mono", session.tenant));
      row.append(node("td", "mono", shortId(session.session_id, 26)));
      row.append(node("td", "", session.event_count));
      row.append(node("td", "", formatTime(session.last_ts)));
      const typeCell = node("td");
      typeCell.append(node("span", "badge " + meta.cls, meta.label));
      row.append(typeCell);
      const summaryCell = node("td", "cell-clip");
      summaryCell.textContent = session.last_summary || "";
      summaryCell.title = session.last_summary || "";
      row.append(summaryCell);
      row.addEventListener("click", () => openSession(session.tenant, session.session_id));
      tbody.append(row);
    }
    $("live-session-count").textContent = String(state.sessions.length);
  }

  function renderLiveSessions() {
    const list = $("live-sessions");
    clear(list);
    if (!state.sessions.length) {
      list.append(node("div", "empty", "暂无会话"));
      return;
    }
    for (const session of state.sessions.slice(0, 14)) {
      const meta = typeMeta(session.last_type);
      const item = node("div", "session-item");
      const top = node("div", "session-top");
      top.append(node("span", "session-title", shortId(session.session_id, 20)));
      top.append(node("span", "badge " + meta.cls, meta.label));
      const sub = node("div", "session-sub", `${session.tenant} · ${session.event_count} 条事件 · ${formatTime(session.last_ts)}`);
      const summary = node("div", "session-sub", session.last_summary || "");
      item.append(top, sub, summary);
      item.addEventListener("click", () => openSession(session.tenant, session.session_id));
      list.append(item);
    }
  }

  // ---------------------------------------------------------------- feed

  function eventRow(event, timeline = false) {
    const meta = typeMeta(event.type);
    const row = node("div", "event");
    row.tabIndex = 0;
    row.setAttribute("role", "button");
    row.dataset.id = event.id;
    row.append(node("div", "event-rail " + meta.cls));

    const main = node("div", "event-main");
    const metaRow = node("div", "event-meta");
    metaRow.append(node("span", "badge " + meta.cls, meta.label));
    metaRow.append(node("span", "mono", event.tenant));
    metaRow.append(node("span", "mono", shortId(event.session_id, 20)));
    metaRow.append(node("span", "muted", `#${event.seq}`));
    main.append(metaRow);
    const summary = node("div", "event-summary", event.summary || "");
    main.append(summary);
    row.append(main);
    row.append(node("div", "event-time", formatTime(event.ts)));

    row.addEventListener("click", () => openEvent(event.id));
    row.addEventListener("keydown", (keyEvent) => {
      if (keyEvent.key === "Enter" || keyEvent.key === " ") {
        keyEvent.preventDefault();
        openEvent(event.id);
      }
    });
    return row;
  }

  function matchesFilters(event) {
    if (state.filters.tenant && event.tenant !== state.filters.tenant) return false;
    if (state.filters.type && event.type !== state.filters.type) return false;
    return true;
  }

  function renderFeed() {
    const feed = $("feed");
    clear(feed);
    const visible = state.events.filter(matchesFilters);
    if (!state.events.length) {
      const empty = node("div", "empty");
      empty.append(node("strong", "", "还没有事件"));
      empty.append(node("span", "", "接入审计插件后，这里会实时滚动。也可以先运行 python -m portal.cli seed-demo。"));
      feed.append(empty);
    } else if (!visible.length) {
      const empty = node("div", "empty");
      empty.append(node("strong", "", "没有匹配的事件"));
      empty.append(node("span", "", "换一个筛选条件，或清除搜索。"));
      feed.append(empty);
    } else {
      for (const event of visible) feed.append(eventRow(event));
    }
    updateFeedCount();
  }

  function updateFeedCount() {
    $("feed-count").textContent = `${$("feed").querySelectorAll(".event").length} 条`;
  }

  async function loadFeed() {
    try {
      const params = new URLSearchParams({ limit: "100" });
      if (state.filters.tenant) params.set("tenant", state.filters.tenant);
      if (state.filters.type) params.set("type", state.filters.type);
      if (state.filters.q) params.set("q", state.filters.q);
      const data = await api(`/api/audit/events?${params.toString()}`);
      state.events = data.events || [];
      if (!state.filters.q) {
        const maxId = state.events.reduce((max, event) => Math.max(max, Number(event.id) || 0), 0);
        state.lastEventId = Math.max(state.lastEventId, maxId);
      }
      renderFeed();
    } catch (error) {
      showToast(error.message, "err");
    }
  }

  function updateFilterBanner() {
    const banner = $("filter-banner");
    const text = $("filter-banner-text");
    if (state.filters.q) {
      text.textContent = `正在搜索「${state.filters.q}」，实时流已暂停。`;
      banner.hidden = false;
    } else if (state.filters.tenant || state.filters.type) {
      const tenant = state.tenants.find((item) => item.id === state.filters.tenant);
      const type = state.filters.type ? typeMeta(state.filters.type).label : "";
      text.textContent = `筛选：${[tenant ? tenant.name : "", type].filter(Boolean).join(" · ")}`;
      banner.hidden = false;
    } else {
      banner.hidden = true;
    }
  }

  function applyFilters() {
    state.filters.tenant = $("filter-tenant").value;
    state.filters.type = $("filter-type").value;
    state.filters.q = $("search-input").value.trim();
    updateFilterBanner();
    loadFeed();
    loadSessions();
    if (state.filters.q) {
      disconnectSSE();
      setSSE("paused");
    } else {
      connectSSE();
    }
  }

  function clearFilters() {
    $("filter-tenant").value = "";
    $("filter-type").value = "";
    $("search-input").value = "";
    state.filters = { tenant: "", type: "", q: "" };
    updateFilterBanner();
    loadFeed();
    loadSessions();
    connectSSE();
  }

  // ---------------------------------------------------------------- live stream

  function connectSSE() {
    disconnectSSE();
    if (state.filters.q) return;
    const params = new URLSearchParams({ after_id: String(state.lastEventId) });
    if (state.filters.tenant) params.set("tenant", state.filters.tenant);
    const source = new EventSource(`/api/audit/stream?${params.toString()}`);
    state.es = source;
    setSSE("connecting");
    source.onopen = () => setSSE("on");
    source.onerror = () => setSSE("off");
    source.onmessage = (message) => {
      try {
        handleIncoming(JSON.parse(message.data));
      } catch (error) {
        console.warn("bad event", error);
      }
    };
  }

  function disconnectSSE() {
    if (state.es) {
      state.es.close();
      state.es = null;
    }
  }

  function handleIncoming(event) {
    if (typeof event.id === "number") {
      state.lastEventId = Math.max(state.lastEventId, event.id);
    }
    if (!matchesFilters(event)) return;
    state.events.unshift(event);
    if (state.events.length > 300) state.events.pop();
    const feed = $("feed");
    if (state.activeTab === "live") {
      const empty = feed.querySelector(".empty");
      if (empty) empty.remove();
      feed.prepend(eventRow(event));
      while (feed.querySelectorAll(".event").length > 300) {
        const last = feed.querySelector(".event:last-of-type");
        if (!last) break;
        last.remove();
      }
      updateFeedCount();
    }
    mergeSession(event);
    if (state.statsTimer) window.clearTimeout(state.statsTimer);
    state.statsTimer = window.setTimeout(loadStats, 700);
  }

  function mergeSession(event) {
    let session = state.sessions.find(
      (item) => item.tenant === event.tenant && item.session_id === event.session_id
    );
    if (!session) {
      session = {
        tenant: event.tenant,
        session_id: event.session_id,
        first_ts: event.ts,
        last_ts: event.ts,
        event_count: 0,
        last_type: event.type,
        last_summary: event.summary,
      };
      state.sessions.unshift(session);
    }
    session.last_ts = event.ts;
    session.event_count = Number(session.event_count || 0) + 1;
    session.last_type = event.type;
    session.last_summary = event.summary;
    state.sessions.sort((a, b) => Number(b.last_ts) - Number(a.last_ts));
    if (state.activeTab === "live") renderLiveSessions();
    $("live-session-count").textContent = String(state.sessions.length);
  }

  // ---------------------------------------------------------------- event detail

  async function openEvent(id) {
    try {
      const event = await api(`/api/audit/events/${id}`);
      const meta = typeMeta(event.type);
      $("event-modal-title").textContent = `事件详情 · ${meta.label}`;
      const body = $("event-modal-body");
      clear(body);
      const kv = node("dl", "kv");
      const pairs = [
        ["租户", event.tenant],
        ["会话", event.session_id],
        ["序号", `#${event.seq}`],
        ["时间", formatTime(event.ts)],
        ["类型", event.type],
        ["哈希", event.hash],
      ];
      for (const [key, value] of pairs) {
        kv.append(node("dt", "", key));
        kv.append(node("dd", key === "哈希" || key === "会话" ? "mono" : "", value));
      }
      body.append(kv);
      const pre = node("pre", "");
      pre.textContent = JSON.stringify(event.payload, null, 2);
      body.append(pre);
      $("event-modal").hidden = false;
      $("event-modal-close").focus();
    } catch (error) {
      showToast(error.message, "err");
    }
  }

  function closeEventModal() {
    $("event-modal").hidden = true;
  }

  // ---------------------------------------------------------------- session detail

  async function openSession(tenant, sessionId) {
    state.currentSession = { tenant, sessionId };
    switchTab("sessions");
    $("session-detail").hidden = false;
    $("session-detail-title").textContent = `${tenant} / ${sessionId}`;
    $("verify-result").textContent = "";
    const timeline = $("session-timeline");
    clear(timeline);
    timeline.append(node("div", "empty", "加载中…"));
    try {
      const params = new URLSearchParams({ tenant, session_id: sessionId, limit: "200" });
      const data = await api(`/api/audit/events?${params.toString()}`);
      const events = (data.events || []).slice().reverse();
      clear(timeline);
      if (!events.length) {
        timeline.append(node("div", "empty", "这个会话还没有事件"));
        return;
      }
      for (const event of events) timeline.append(eventRow(event, true));
    } catch (error) {
      clear(timeline);
      timeline.append(node("div", "empty", error.message));
    }
  }

  async function verifyCurrentSession() {
    if (!state.currentSession) return;
    const result = $("verify-result");
    result.textContent = "校验中…";
    try {
      const params = new URLSearchParams({
        tenant: state.currentSession.tenant,
        session_id: state.currentSession.sessionId,
      });
      const data = await api(`/api/audit/verify?${params.toString()}`);
      if (data.ok) {
        result.textContent = `✓ 哈希链完整，共 ${data.events} 条事件。`;
        result.className = "panel-body";
      } else {
        result.textContent = `✗ 第 #${data.first_bad_seq} 条开始断链：${data.reason}`;
        result.className = "panel-body";
      }
    } catch (error) {
      result.textContent = error.message;
    }
  }

  // ---------------------------------------------------------------- create forms

  async function submitTenant(event) {
    event.preventDefault();
    const name = $("tenant-name").value.trim();
    const id = $("tenant-id").value.trim();
    if (!name) return;
    const button = event.target.querySelector("button[type=submit]");
    button.disabled = true;
    try {
      const data = await api("/api/tenants", { method: "POST", body: { name, id: id || null } });
      state.lastToken = data.token;
      $("tenant-token").hidden = false;
      $("tenant-token-id").textContent = data.tenant.id;
      $("tenant-token-value").textContent = data.token;
      event.target.reset();
      showToast(`租户 ${data.tenant.id} 已创建，令牌只显示这一次`);
      await loadTenants();
      $("tenant-token").scrollIntoView({ behavior: "smooth", block: "nearest" });
    } catch (error) {
      showToast(error.message, "err");
    } finally {
      button.disabled = false;
    }
  }

  async function submitUser(event) {
    event.preventDefault();
    const username = $("user-name").value.trim();
    const password = $("user-password").value;
    const role = $("user-role").value;
    const tenantId = $("user-tenant").value || null;
    if (!username || password.length < 8) {
      showToast("用户名不能为空，密码至少 8 位", "err");
      return;
    }
    const button = event.target.querySelector("button[type=submit]");
    button.disabled = true;
    try {
      await api("/api/users", { method: "POST", body: { username, password, role, tenant_id: tenantId } });
      event.target.reset();
      showToast(`用户 ${username} 已创建`);
      await loadUsers();
    } catch (error) {
      showToast(error.message, "err");
    } finally {
      button.disabled = false;
    }
  }

  async function loadRuntimeConfig(tenantId) {
    const result = $("runtime-result");
    $("runtime-key").value = "";
    if (!tenantId) {
      $("runtime-base").value = "";
      $("runtime-model").value = "hermes-agent";
      result.textContent = "还没有租户";
      return;
    }
    try {
      const data = await api(`/api/tenants/${encodeURIComponent(tenantId)}/runtime`);
      if (data.configured) {
        $("runtime-base").value = data.base_url || "";
        $("runtime-model").value = data.model || "hermes-agent";
        result.textContent = data.last_error
          ? `已保存 · 最近测试失败：${data.last_error}`
          : data.last_ok_at
            ? `已保存 · 最近测试通过：${formatTime(data.last_ok_at)}`
            : "已保存 · 尚未测试";
      } else {
        $("runtime-base").value = "";
        $("runtime-model").value = "hermes-agent";
        result.textContent = "未配置运行环境";
      }
    } catch (error) {
      result.textContent = error.message;
    }
  }

  async function saveRuntime(event) {
    event.preventDefault();
    const tenantId = $("runtime-tenant").value;
    const baseUrl = $("runtime-base").value.trim();
    const apiKey = $("runtime-key").value;
    const model = $("runtime-model").value.trim() || "hermes-agent";
    if (!tenantId) {
      showToast("先选择一个租户", "err");
      return;
    }
    const button = event.target.querySelector("button[type=submit]");
    button.disabled = true;
    try {
      await api(`/api/tenants/${encodeURIComponent(tenantId)}/runtime`, {
        method: "PUT",
        body: { base_url: baseUrl, api_key: apiKey, model },
      });
      showToast("运行环境已保存");
      await loadTenants();
      await loadRuntimeConfig(tenantId);
    } catch (error) {
      showToast(error.message, "err");
    } finally {
      button.disabled = false;
    }
  }

  async function testRuntime() {
    const tenantId = $("runtime-tenant").value;
    if (!tenantId) {
      showToast("先选择一个租户", "err");
      return;
    }
    const button = $("runtime-test");
    button.disabled = true;
    $("runtime-result").textContent = "连接中…";
    try {
      const data = await api(`/api/tenants/${encodeURIComponent(tenantId)}/runtime/test`, { method: "POST" });
      $("runtime-result").textContent = data.ok ? "连接成功：/v1/models 可用" : `连接失败：${data.error || "未知错误"}`;
      showToast(data.ok ? "运行环境连接成功" : "运行环境连接失败", data.ok ? "ok" : "err");
      await loadTenants();
    } catch (error) {
      $("runtime-result").textContent = error.message;
      showToast(error.message, "err");
    } finally {
      button.disabled = false;
    }
  }

  async function loadGrants(userId) {
    const list = $("grant-list");
    if (!userId) {
      list.textContent = "";
      return;
    }
    try {
      const data = await api(`/api/users/${encodeURIComponent(userId)}/tenants`);
      clear(list);
      if (!data.grants.length) {
        list.textContent = "该用户还没有租户授权";
        return;
      }
      for (const grant of data.grants) {
        const row = node("div", "grant-row");
        row.append(node("span", "small", `${grant.name}（${grant.tenant_id}） · ${grant.access_role}`));
        const button = node("button", "btn btn-sm btn-danger", "撤销");
        button.type = "button";
        button.addEventListener("click", async () => {
          button.disabled = true;
          try {
            await api(
              `/api/users/${encodeURIComponent(userId)}/tenants/${encodeURIComponent(grant.tenant_id)}`,
              { method: "DELETE" }
            );
            showToast("授权已撤销");
            await loadGrants(userId);
          } catch (error) {
            showToast(error.message, "err");
            button.disabled = false;
          }
        });
        row.append(button);
        list.append(row);
      }
    } catch (error) {
      list.textContent = error.message;
    }
  }

  async function grantUser(event) {
    event.preventDefault();
    const userId = $("grant-user").value;
    const tenantId = $("grant-tenant").value;
    const accessRole = $("grant-role").value;
    if (!userId || !tenantId) {
      showToast("先选择用户和租户", "err");
      return;
    }
    const button = event.target.querySelector("button[type=submit]");
    button.disabled = true;
    try {
      await api(`/api/users/${encodeURIComponent(userId)}/tenants`, {
        method: "POST",
        body: { tenant_id: tenantId, access_role: accessRole },
      });
      showToast("授权已保存");
      await loadGrants(userId);
    } catch (error) {
      showToast(error.message, "err");
    } finally {
      button.disabled = false;
    }
  }

  // ---------------------------------------------------------------- projects

  async function loadProjects() {
    try {
      const data = await api("/api/projects");
      state.projects = data.projects || [];
      renderProjects();
    } catch (error) {
      showToast(error.message, "err");
    }
  }

  function renderProjects() {
    const list = $("project-list");
    clear(list);
    if (!state.projects.length) {
      list.append(node("div", "empty", "还没有项目。右侧填写目标和已知条件，创建一个。"));
      return;
    }
    for (const project of state.projects) {
      const card = node("div", "approval-card");
      const main = node("div", "approval-main");
      const meta = node("div", "approval-meta");
      meta.append(node("span", "badge", project.tenant_name || project.tenant_id));
      meta.append(node("span", "", project.access_role === "viewer" ? "只读" : "可接手"));
      meta.append(node("span", "", formatTime(project.updated_at)));
      meta.append(
        node(
          "span",
          project.report_exists ? "badge" : "badge danger",
          project.report_exists ? "报告已生成" : "待生成报告"
        )
      );
      main.append(meta);
      main.append(node("strong", "", project.name));
      if (project.goal) main.append(node("div", "small muted", project.goal));
      if (project.repo_url) main.append(node("div", "small muted mono", project.repo_url));
      main.append(node("div", "small muted mono", project.workspace));

      const actions = node("div", "approval-actions");
      if (project.report_exists) {
        const reportButton = node("button", "btn btn-sm", "查看报告");
        reportButton.type = "button";
        reportButton.addEventListener("click", () => viewProjectReport(project));
        actions.append(reportButton);
      }
      const button = node("button", "btn btn-sm btn-primary", "开始接手");
      button.type = "button";
      button.disabled = project.access_role === "viewer";
      button.addEventListener("click", () => onboardProject(project.id, button));
      actions.append(button);
      card.append(main, actions);
      list.append(card);
    }
  }

  async function onboardProject(projectId, button) {
    if (button) button.disabled = true;
    try {
      const data = await api(`/api/projects/${encodeURIComponent(projectId)}/onboard`, {
        method: "POST",
      });
      const params = new URLSearchParams({
        tenant: data.conversation.tenant_id,
        conversation: data.conversation.id,
        prompt: data.prompt,
      });
      window.location.href = `/chat?${params.toString()}`;
    } catch (error) {
      showToast(error.message, "err");
      if (button) button.disabled = false;
    }
  }

  async function viewProjectReport(project) {
    try {
      const data = await api(`/api/projects/${encodeURIComponent(project.id)}/report`);
      $("project-modal-title").textContent = `接手报告 · ${data.project?.name || project.name}`;
      $("project-report-body").textContent = data.content || "（空报告）";
      $("project-modal").hidden = false;
      $("project-modal-close").focus();
    } catch (error) {
      showToast(error.message, "err");
    }
  }

  function closeProjectModal() {
    $("project-modal").hidden = true;
  }

  async function submitProject(event) {
    event.preventDefault();
    const name = $("project-name").value.trim();
    const tenantId = $("project-tenant").value;
    const goal = $("project-goal").value.trim();
    if (!name || !tenantId || !goal) {
      showToast("项目名称、租户和目标都要填", "err");
      return;
    }
    const button = event.target.querySelector("button[type=submit]");
    button.disabled = true;
    try {
      await api("/api/projects", {
        method: "POST",
        body: {
          name,
          tenant_id: tenantId,
          goal,
          repo_url: $("project-repo").value.trim(),
          known: $("project-known").value.trim(),
          unknowns: $("project-unknowns").value.trim(),
        },
      });
      event.target.reset();
      showToast("项目已创建，点“开始接手”生成接手会话");
      await loadProjects();
    } catch (error) {
      showToast(error.message, "err");
    } finally {
      button.disabled = false;
    }
  }

  // ---------------------------------------------------------------- skills & accumulation

  async function loadAssets() {
    try {
      const data = await api("/api/assets");
      renderServerSkills(data.server_skills || []);
      renderTenantAssets(data.tenants || []);
    } catch (error) {
      showToast(error.message, "err");
    }
  }

  function renderServerSkills(skills) {
    const list = $("server-skill-list");
    clear(list);
    $("server-skill-count").textContent = `${skills.length} 个可用技能`;
    if (!skills.length) {
      list.append(node("div", "empty", "服务端技能库为空"));
      return;
    }
    for (const skill of skills) {
      const card = node("div", "approval-card");
      const main = node("div", "approval-main");
      const meta = node("div", "approval-meta");
      meta.append(node("span", "badge", skill.category || "技能"));
      meta.append(node("span", "", skill.id));
      main.append(meta);
      main.append(node("strong", "", skill.name));
      if (skill.description) main.append(node("div", "small muted", skill.description));
      const actions = node("div", "approval-actions");
      const button = node("button", "btn btn-sm", "查看说明");
      button.type = "button";
      button.addEventListener("click", () => viewServerSkill(skill));
      actions.append(button);
      card.append(main, actions);
      list.append(card);
    }
  }

  async function viewServerSkill(skill) {
    try {
      const data = await api(`/api/skills/${encodeURIComponent(skill.id)}`);
      $("server-skill-title").textContent = `${data.name} · ${data.id}`;
      $("server-skill-body").textContent = data.content || "（空技能）";
      $("server-skill-detail").hidden = false;
      $("server-skill-detail").scrollIntoView({ behavior: "smooth", block: "nearest" });
    } catch (error) {
      showToast(error.message, "err");
    }
  }

  function renderTenantAssets(tenants) {
    const tbody = $("assets-body");
    clear(tbody);
    $("assets-empty").hidden = tenants.length > 0;
    for (const tenant of tenants) {
      const row = node("tr");
      const name = node("td");
      name.append(node("strong", "", tenant.tenant_name || tenant.tenant_id));
      name.append(node("div", "small muted mono", tenant.tenant_id));
      row.append(name);
      row.append(node("td", "", tenant.skill_count ?? 0));
      row.append(node("td", "", tenant.memory_count ?? 0));
      const recent = node("td", "small muted");
      recent.textContent = (tenant.recent_skills || []).slice(0, 6).join("、") || "—";
      row.append(recent);
      row.append(node("td", "small muted", formatTime(tenant.last_seen)));
      tbody.append(row);
    }
  }

  async function loadApprovals() {
    try {
      const data = await api("/api/approvals?status=pending");
      renderApprovals(data.approvals || []);
    } catch (error) {
      showToast(error.message, "err");
    }
  }

  function renderApprovals(items) {
    const list = $("approval-list");
    clear(list);
    if (!items.length) {
      list.append(node("div", "empty", "当前没有待审批"));
      return;
    }
    for (const approval of items) {
      const card = node("div", "approval-card");
      const main = node("div", "approval-main");
      const meta = node("div", "approval-meta");
      meta.append(node("span", "badge", approval.tenant_id));
      meta.append(node("span", "", approval.username || `用户 ${approval.user_id || "?"}`));
      meta.append(node("span", "", approval.tool || "工具"));
      meta.append(node("span", "", formatTime(approval.created_at)));
      main.append(meta);
      if (approval.command) main.append(node("div", "approval-command", approval.command));
      if (approval.description) main.append(node("div", "small muted", approval.description));
      const actions = node("div", "approval-actions");
      const choices = [
        ["once", "允许一次", ""],
        ["session", "允许本会话", ""],
        ["always", "始终允许", ""],
        ["deny", "拒绝", "btn-danger"],
      ];
      for (const [choice, label, cls] of choices) {
        const button = node("button", `btn btn-sm ${cls}`.trim(), label);
        button.type = "button";
        button.addEventListener("click", () => decideApproval(approval.id, choice));
        actions.append(button);
      }
      card.append(main, actions);
      list.append(card);
    }
  }

  async function decideApproval(approvalId, choice) {
    try {
      await api(`/api/approvals/${encodeURIComponent(approvalId)}`, {
        method: "POST",
        body: { choice },
      });
      showToast(choice === "deny" ? "已拒绝" : "已批准");
      await loadApprovals();
    } catch (error) {
      showToast(error.message, "err");
    }
  }

  async function copyToken() {
    if (!state.lastToken) return;
    try {
      await navigator.clipboard.writeText(state.lastToken);
      showToast("令牌已复制");
    } catch {
      window.prompt("复制令牌", state.lastToken);
    }
  }

  // ---------------------------------------------------------------- bindings

  function bind() {
    document.querySelectorAll(".nav-item").forEach((item) => {
      item.addEventListener("click", () => switchTab(item.dataset.tab));
    });

    $("logout-btn").addEventListener("click", async () => {
      try {
        await api("/api/auth/logout", { method: "POST" });
      } catch (error) {
        console.warn(error);
      }
      window.location.href = "/login";
    });

    $("filter-tenant").addEventListener("change", applyFilters);
    $("filter-type").addEventListener("change", applyFilters);
    $("search-input").addEventListener("input", () => {
      if (state.searchTimer) window.clearTimeout(state.searchTimer);
      state.searchTimer = window.setTimeout(applyFilters, 350);
    });
    $("clear-filter").addEventListener("click", clearFilters);
    $("filter-banner-clear").addEventListener("click", clearFilters);

    $("tenant-form").addEventListener("submit", submitTenant);
    $("project-form").addEventListener("submit", submitProject);
    $("user-form").addEventListener("submit", submitUser);
    $("copy-token").addEventListener("click", copyToken);
    $("runtime-tenant").addEventListener("change", (event) => loadRuntimeConfig(event.target.value));
    $("runtime-form").addEventListener("submit", saveRuntime);
    $("runtime-test").addEventListener("click", testRuntime);
    $("grant-form").addEventListener("submit", grantUser);
    $("grant-user").addEventListener("change", (event) => loadGrants(event.target.value));
    $("refresh-approvals").addEventListener("click", loadApprovals);
    $("close-skill-detail").addEventListener("click", () => {
      $("server-skill-detail").hidden = true;
    });

    $("close-detail").addEventListener("click", () => {
      $("session-detail").hidden = true;
      state.currentSession = null;
    });
    $("verify-btn").addEventListener("click", verifyCurrentSession);

    $("event-modal-close").addEventListener("click", closeEventModal);
    $("event-modal").addEventListener("click", (event) => {
      if (event.target === $("event-modal")) closeEventModal();
    });
    $("project-modal-close").addEventListener("click", closeProjectModal);
    $("project-modal").addEventListener("click", (event) => {
      if (event.target === $("project-modal")) closeProjectModal();
    });
    document.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      if (!$("event-modal").hidden) closeEventModal();
      else if (!$("project-modal").hidden) closeProjectModal();
    });
  }

  // ---------------------------------------------------------------- boot

  async function main() {
    try {
      const me = await api("/api/me");
      if (me.user.role !== "admin") {
        window.location.href = "/login";
        return;
      }
      state.me = me;
      state.csrf = me.csrf || "";
      $("user-chip").textContent = `${me.user.username} · 管理员`;
    } catch {
      return;
    }
    bind();
    await Promise.all([loadStats(), loadTenants(), loadUsers()]);
    await loadSessions();
    await loadFeed();
    connectSSE();
    window.setInterval(loadStats, 10000);
    window.setInterval(loadSessions, 15000);
    window.setInterval(loadApprovals, 10000);
  }

  main();
})();
