(() => {
  "use strict";

  const state = {
    me: null,
    csrf: "",
    tenants: [],
    tenantId: "",
    conversations: [],
    conversationId: "",
    streaming: false,
    readonly: false,
    toastTimer: null,
    streamStartedAt: 0,
    streamTimer: null,
    phase: "",
    toolNodes: new Map(),
    thinkingNode: null,
    pendingFiles: [],
    watchTimer: null,
    watchStarted: 0,
    localDirHandle: null,
    localNeedsPermission: false,
    localFolder: null,
    localBaseline: new Map(),
    localBusy: false,
    skills: [],
    selectedSkills: new Set(),
    memories: [],
    canManageTeam: false,
    memoryScope: "private",
  };

  // Project onboarding opens /chat with a prefilled prompt. It is deliberately
  // NOT auto-sent: a cross-site link must not be able to make a logged-in user
  // execute an agent task without reviewing it.
  const bootParams = new URLSearchParams(window.location.search);
  const bootTenant = bootParams.get("tenant") || "";
  const bootConversation = bootParams.get("conversation") || "";
  const bootPrompt = bootParams.get("prompt") || "";

  const LOCAL_MAX_FILES = 500;
  const LOCAL_MAX_FILE_BYTES = 50 * 1024 * 1024;
  const LOCAL_MAX_BYTES = 200 * 1024 * 1024;
  const LOCAL_BATCH_FILES = 20;
  const LOCAL_SKIP_DIRS = new Set([
    ".git", ".svn", ".hg", "node_modules", ".venv", "venv", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".next", ".cache",
  ]);
  const LOCAL_SKIP_FILES = new Set([
    ".env", ".env.local", ".env.production", ".env.development",
    "id_rsa", "id_ed25519", "credentials.json", "auth.json",
  ]);
  const LOCAL_SKIP_SUFFIXES = [".pem", ".key", ".pfx", ".p12"];

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

  // IndexedDB stores FileSystemDirectoryHandle objects across reloads.
  function idbOpen() {
    return new Promise((resolve, reject) => {
      const request = indexedDB.open("hermes-local-bridge", 1);
      request.onupgradeneeded = () => {
        const db = request.result;
        if (!db.objectStoreNames.contains("kv")) db.createObjectStore("kv");
      };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
  }

  async function idbGet(key) {
    const db = await idbOpen();
    return new Promise((resolve, reject) => {
      const tx = db.transaction("kv", "readonly");
      const request = tx.objectStore("kv").get(key);
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
  }

  async function idbSet(key, value) {
    const db = await idbOpen();
    return new Promise((resolve, reject) => {
      const tx = db.transaction("kv", "readwrite");
      tx.objectStore("kv").put(value, key);
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error);
    });
  }

  async function idbDel(key) {
    const db = await idbOpen();
    return new Promise((resolve, reject) => {
      const tx = db.transaction("kv", "readwrite");
      tx.objectStore("kv").delete(key);
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error);
    });
  }

  function localKey(prefix) {
    return `${prefix}:${state.conversationId}`;
  }

  function formatBytes(bytes) {
    const value = Number(bytes) || 0;
    if (value < 1024) return `${value} B`;
    if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
    return `${(value / 1024 / 1024).toFixed(1)} MB`;
  }

  function renderPendingFiles() {
    const box = $("pending-attachments");
    clear(box);
    if (!state.pendingFiles.length) {
      box.hidden = true;
      return;
    }
    box.hidden = false;
    state.pendingFiles.forEach((file, index) => {
      const chip = node("span", "file-chip");
      chip.append(node("span", "", file.name));
      chip.append(node("span", "file-size", formatBytes(file.size)));
      const remove = node("button", "file-remove", "×");
      remove.type = "button";
      remove.title = "移除";
      remove.addEventListener("click", () => {
        state.pendingFiles.splice(index, 1);
        renderPendingFiles();
      });
      chip.append(remove);
      box.append(chip);
    });
  }

  function addFiles(fileList) {
    const incoming = Array.from(fileList || []);
    if (!incoming.length) return;
    const room = 5 - state.pendingFiles.length;
    if (room <= 0) {
      showToast("一次最多 5 个文件", "err");
      return;
    }
    for (const file of incoming.slice(0, room)) {
      if (file.size > 25 * 1024 * 1024) {
        showToast(`${file.name} 超过 25 MB`, "err");
        continue;
      }
      state.pendingFiles.push(file);
    }
    renderPendingFiles();
    if (incoming.length > room) showToast("最多 5 个文件，多余的已忽略", "err");
  }

  function clearPendingFiles() {
    state.pendingFiles = [];
    renderPendingFiles();
    const input = $("file-input");
    if (input) input.value = "";
  }

  function formatTime(ts) {
    if (!ts) return "";
    return new Date(Number(ts) * 1000).toLocaleString("zh-CN", {
      month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
    });
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

  function showError(message) {
    const box = $("chat-error");
    box.textContent = message;
    box.hidden = !message;
  }

  function currentTenant() {
    return state.tenants.find((item) => item.id === state.tenantId) || null;
  }

  function currentConversation() {
    return state.conversations.find((item) => item.id === state.conversationId) || null;
  }

  function setStatus(text, kind = "") {
    const status = $("chat-status");
    status.textContent = text;
    status.className = "badge" + (kind ? ` ${kind}` : "");
  }

  function setComposerEnabled(enabled) {
    const input = $("chat-input");
    const button = $("send-btn");
    input.disabled = !enabled;
    button.disabled = !enabled;
    const attach = $("attach-btn");
    if (attach) attach.disabled = !enabled;
    input.placeholder = enabled
      ? "输入消息。Enter 发送，Shift+Enter 换行。"
      : state.streaming
        ? "Agent 正在回复，请稍候…"
        : state.readonly
          ? "只读账号不能发送消息。"
          : "选择或新建一个会话后开始。";
  }

  // ---------------------------------------------------------------- tenants

  function tenantDisplayName(tenant) {
    return tenant.runtime_ready ? tenant.name : `${tenant.name}（未配置）`;
  }

  function updateTenantPicker() {
    const trigger = $("tenant-trigger");
    const label = $("tenant-label");
    if (!trigger || !label) return;
    const tenant = currentTenant();
    trigger.disabled = !state.tenants.length;
    label.textContent = tenant ? tenantDisplayName(tenant) : "没有可用租户";
    document.querySelectorAll("#tenant-menu .select-option").forEach((option) => {
      option.setAttribute("aria-selected", option.dataset.tenantId === state.tenantId ? "true" : "false");
    });
  }

  function closeTenantMenu() {
    const menu = $("tenant-menu");
    const trigger = $("tenant-trigger");
    if (menu) menu.hidden = true;
    if (trigger) trigger.setAttribute("aria-expanded", "false");
  }

  function renderTenantMenu() {
    const menu = $("tenant-menu");
    clear(menu);
    for (const tenant of state.tenants) {
      const option = node("button", "select-option");
      option.type = "button";
      option.dataset.tenantId = tenant.id;
      option.setAttribute("role", "option");
      option.setAttribute("aria-selected", tenant.id === state.tenantId ? "true" : "false");
      const label = node("span", "", tenant.name);
      const note = node(
        "span",
        "option-note",
        tenant.runtime_ready ? (tenant.access_role === "viewer" ? "只读" : "") : "未配置运行环境"
      );
      label.append(note);
      option.append(label);
      if (tenant.id === state.tenantId) option.append(node("span", "option-check", "✓"));
      option.addEventListener("click", (event) => {
        event.stopPropagation();
        closeTenantMenu();
        selectTenant(tenant.id).catch((error) => showToast(error.message, "err"));
      });
      menu.append(option);
    }
  }

  async function loadTenants() {
    const data = await api("/api/me/tenants");
    state.tenants = data.tenants || [];
    if (!state.tenants.length) {
      updateTenantPicker();
      $("new-conversation").disabled = true;
      setComposerEnabled(false);
      setStatus("没有租户");
      $("chat-title").textContent = "还没有分配租户";
      $("chat-sub").textContent = "请联系管理员为你的账号分配一个租户。";
      return;
    }
    const preferred =
      (bootTenant && state.tenants.find((item) => item.id === bootTenant)) ||
      state.tenants.find((item) => item.runtime_ready) ||
      state.tenants[0];
    state.tenantId = preferred.id;
    state.readonly = preferred.access_role === "viewer" && state.me.user.role !== "admin";
    updateTenantPicker();
    renderTenantMenu();
    await loadConversations();
  }

  async function loadConversations() {
    if (!state.tenantId) return;
    const data = await api(`/api/chat/conversations?tenant_id=${encodeURIComponent(state.tenantId)}`);
    state.conversations = data.conversations || [];
    renderConversations();
    if (state.conversations.length) {
      const target =
        (bootConversation && state.conversations.find((item) => item.id === bootConversation)) ||
        state.conversations[0];
      await selectConversation(target.id);
    } else {
      state.conversationId = "";
      renderMessages([]);
      $("chat-title").textContent = "新会话";
      $("chat-sub").textContent = "发送第一条消息后会自动创建会话。";
      setStatus("未开始");
      const tenant = currentTenant();
      setComposerEnabled(!!tenant && tenant.runtime_ready && !state.readonly);
    }
  }

  function renderConversations() {
    const list = $("conversation-list");
    clear(list);
    if (!state.conversations.length) {
      list.append(node("div", "empty", "还没有会话"));
      return;
    }
    for (const conversation of state.conversations) {
      const item = node("button", "conversation-item" + (conversation.id === state.conversationId ? " active" : ""));
      item.type = "button";
      item.append(node("span", "conversation-title", conversation.title || "新会话"));
      item.append(node("span", "conversation-time", formatTime(conversation.updated_at)));
      item.addEventListener("click", () => selectConversation(conversation.id));
      list.append(item);
    }
  }

  async function selectTenant(tenantId) {
    stopWatch();
    clearPendingFiles();
    state.localFolder = null;
    state.localDirHandle = null;
    state.localNeedsPermission = false;
    state.localBaseline = new Map();
    state.memories = [];
    state.canManageTeam = false;
    state.memoryScope = "private";
    renderLocalBridge();
    renderMemories();
    state.tenantId = tenantId;
    const tenant = currentTenant();
    state.readonly = !!tenant && tenant.access_role === "viewer" && state.me.user.role !== "admin";
    state.conversationId = "";
    updateTenantPicker();
    await loadConversations();
  }

  const NEW_CONVERSATION_TITLE = "新会话";

  async function createConversation() {
    if (!state.tenantId) return null;
    stopWatch();
    if (state.readonly) {
      showToast("只读账号不能创建会话", "err");
      return null;
    }
    const data = await api("/api/chat/conversations", {
      method: "POST",
      body: { tenant_id: state.tenantId, title: NEW_CONVERSATION_TITLE },
    });
    state.conversations.unshift(data.conversation);
    state.conversationId = data.conversation.id;
    state.localFolder = null;
    state.localDirHandle = null;
    state.localNeedsPermission = false;
    state.localBaseline = new Map();
    state.memories = [];
    state.canManageTeam = false;
    state.memoryScope = "private";
    renderLocalBridge();
    renderMemories();
    renderConversations();
    renderMessages([]);
    $("chat-title").textContent = "新会话";
    $("chat-sub").textContent = "";
    setStatus("未开始");
    setComposerEnabled(true);
    $("chat-input").focus();
    return data.conversation;
  }

  // ---------------------------------------------------------------- messages

  function stopWatch() {
    if (state.watchTimer) {
      window.clearTimeout(state.watchTimer);
      state.watchTimer = null;
    }
  }

  async function watchActiveRun() {
    const cid = state.conversationId;
    if (!cid || state.streaming) return;
    let data;
    try {
      data = await api(`/api/chat/conversations/${encodeURIComponent(cid)}/active`);
    } catch {
      return;
    }
    if (cid !== state.conversationId) return;
    if (data.active) {
      const elapsed = data.started_at
        ? Math.max(0, Math.round(Date.now() / 1000 - Number(data.started_at)))
        : 0;
      setStatus(`后台执行中 · ${elapsed}s`, "");
      setComposerEnabled(false);
      stopWatch();
      state.watchTimer = window.setTimeout(() => {
        watchActiveRun().catch(() => {});
      }, 3000);
      return;
    }
    const wasWatching = !!state.watchTimer;
    stopWatch();
    if (wasWatching) {
      await selectConversation(cid);
      showToast(data.error ? `后台任务失败：${data.error}` : "后台任务已完成", data.error ? "err" : "ok");
    }
  }

  async function selectConversation(conversationId) {
    if (state.streaming || state.localBusy) return;
    stopWatch();
    if (state.conversationId && state.conversationId !== conversationId) clearPendingFiles();
    state.conversationId = conversationId;
    renderConversations();
    try {
      const data = await api(`/api/chat/conversations/${encodeURIComponent(conversationId)}/messages`);
      renderMessages(data.messages || []);
      const conversation = data.conversation;
      $("chat-title").textContent = conversation.title || "会话";
      $("chat-sub").textContent = `${conversation.tenant_name || conversation.tenant_id} · ${conversation.id.slice(0, 8)}`;
      const tenant = currentTenant();
      const ready = !!tenant && tenant.runtime_ready;
      state.readonly = !!tenant && tenant.access_role === "viewer" && state.me.user.role !== "admin";
      setStatus(ready ? "就绪" : "未配置运行环境", ready ? "" : "danger");
      setComposerEnabled(ready && !state.readonly);
      renderSkills();
      await loadMemories();
      await loadLocalFolderState();
      watchActiveRun().catch(() => {});
    } catch (error) {
      showToast(error.message, "err");
    }
  }

  // ---------------------------------------------------------------- server-side skills

  async function loadSkills() {
    try {
      const data = await api("/api/skills");
      state.skills = data.skills || [];
      const available = new Set(state.skills.map((skill) => skill.id));
      for (const skillId of [...state.selectedSkills]) {
        if (!available.has(skillId)) state.selectedSkills.delete(skillId);
      }
      renderSkills();
    } catch (error) {
      console.warn(error);
    }
  }

  function renderSkills() {
    const list = $("skill-list");
    if (!list) return;
    clear(list);
    if (!state.skills.length) {
      list.append(node("span", "small muted", state.readonly ? "只读账号不能使用技能" : "没有可用技能"));
    } else {
      for (const skill of state.skills) {
        const button = node(
          "button",
          "skill-chip" + (state.selectedSkills.has(skill.id) ? " active" : ""),
          skill.name
        );
        button.type = "button";
        button.title = skill.description || skill.name;
        button.disabled = state.readonly;
        button.addEventListener("click", () => {
          if (state.selectedSkills.has(skill.id)) state.selectedSkills.delete(skill.id);
          else state.selectedSkills.add(skill.id);
          renderSkills();
        });
        list.append(button);
      }
    }
    const selected = state.skills.filter((skill) => state.selectedSkills.has(skill.id));
    $("skill-selected-label").textContent = selected.length
      ? selected.map((skill) => skill.name).join("、")
      : "未选择";
    renderSelectedSkills();
  }

  function renderSelectedSkills() {
    const box = $("selected-skills");
    if (!box) return;
    clear(box);
    const selected = state.skills.filter((skill) => state.selectedSkills.has(skill.id));
    if (!selected.length) {
      box.hidden = true;
      return;
    }
    box.hidden = false;
    for (const skill of selected) {
      const chip = node("span", "file-chip");
      chip.append(node("span", "", `技能：${skill.name}`));
      const remove = node("button", "file-remove", "×");
      remove.type = "button";
      remove.title = "取消使用";
      remove.addEventListener("click", () => {
        state.selectedSkills.delete(skill.id);
        renderSkills();
      });
      chip.append(remove);
      box.append(chip);
    }
  }

  // ---------------------------------------------------------------- scoped memories

  async function loadMemories() {
    if (!state.conversationId) {
      state.memories = [];
      state.canManageTeam = false;
      renderMemories();
      return;
    }
    try {
      const data = await api(
        `/api/chat/conversations/${encodeURIComponent(state.conversationId)}/memories`
      );
      state.memories = data.memories || [];
      state.canManageTeam = !!data.can_manage_team;
      if (!state.canManageTeam && state.memoryScope === "team") state.memoryScope = "private";
      renderMemories();
    } catch (error) {
      console.warn(error);
    }
  }

  function renderMemories() {
    const badge = $("memory-badge");
    const hint = $("memory-hint");
    if (!badge || !hint) return;
    const privateCount = state.memories.filter((item) => item.scope === "private").length;
    const teamCount = state.memories.filter((item) => item.scope === "team").length;
    badge.textContent = String(state.memories.length);
    hint.textContent = state.conversationId
      ? `私有 ${privateCount} 条 · 团队 ${teamCount} 条`
      : "私有记忆只对你可见；团队记忆全组可见。";
    renderMemoryList();
  }

  function renderMemoryList() {
    const list = $("memory-list");
    if (!list) return;
    clear(list);
    if (!state.memories.length) {
      list.append(node("div", "empty", "还没有记忆。可以在这里添加，也可以让 Agent 记住。"));
      return;
    }
    for (const item of state.memories) {
      const card = node("div", "memory-item");
      const main = node("div", "memory-item-main");
      const meta = node("div", "memory-item-meta", `${item.scope === "team" ? "团队" : "私有"} · ${formatTime(item.updated_at)}`);
      main.append(meta, node("div", "memory-item-content", item.content));
      card.append(main);
      const canDelete = item.scope === "private" || state.canManageTeam;
      if (canDelete) {
        const remove = node("button", "memory-item-remove", "×");
        remove.type = "button";
        remove.title = "删除记忆";
        remove.addEventListener("click", () => deleteMemory(item.id));
        card.append(remove);
      }
      list.append(card);
    }
  }

  function setMemoryScope(scope) {
    if (scope === "team" && !state.canManageTeam) {
      showToast("团队共享记忆只能由管理员维护", "err");
      return;
    }
    state.memoryScope = scope;
    document.querySelectorAll(".scope-option").forEach((button) => {
      button.classList.toggle("active", button.dataset.scope === scope);
    });
    $("memory-modal-hint").textContent =
      scope === "team"
        ? "团队记忆对整个租户可见，请只写 SOP、项目背景、公共口径。"
        : "私有记忆只对当前用户可见。";
  }

  function openMemoryModal() {
    if (!state.conversationId) {
      showToast("先选择或新建一个会话", "err");
      return;
    }
    setMemoryScope(state.memoryScope);
    $("memory-modal").hidden = false;
    $("memory-input").focus();
  }

  function closeMemoryModal() {
    $("memory-modal").hidden = true;
  }

  async function saveMemory() {
    const input = $("memory-input");
    const content = input.value.trim();
    if (!content) {
      showToast("先写一条记忆", "err");
      return;
    }
    if (state.memoryScope === "team" && !state.canManageTeam) {
      showToast("团队共享记忆只能由管理员添加", "err");
      return;
    }
    try {
      await api(`/api/chat/conversations/${encodeURIComponent(state.conversationId)}/memories`, {
        method: "POST",
        body: { content, scope: state.memoryScope },
      });
      input.value = "";
      showToast("记忆已保存");
      await loadMemories();
    } catch (error) {
      showToast(error.message, "err");
    }
  }

  async function deleteMemory(memoryId) {
    if (!window.confirm("删除这条记忆？")) return;
    try {
      await api(
        `/api/chat/conversations/${encodeURIComponent(state.conversationId)}/memories/${encodeURIComponent(memoryId)}`,
        { method: "DELETE" }
      );
      showToast("记忆已删除");
      await loadMemories();
    } catch (error) {
      showToast(error.message, "err");
    }
  }

  // ---------------------------------------------------------------- local folder bridge

  function setLocalStatus(text) {
    const hint = $("local-hint");
    if (hint) hint.textContent = text;
  }

  function renderLocalBridge() {
    const badge = $("local-badge");
    if (!badge) return;
    const connectBtn = $("local-connect");
    const syncBtn = $("local-sync");
    const disconnectBtn = $("local-disconnect");
    const folder = state.localFolder;
    const handle = state.localDirHandle;
    const busy = state.localBusy;

    badge.className = "badge";
    if (state.readonly) {
      badge.textContent = "只读";
    } else if (busy) {
      badge.textContent = "处理中";
    } else if (folder && handle && !state.localNeedsPermission) {
      badge.textContent = "已授权";
    } else if (folder) {
      badge.textContent = "需重新授权";
      badge.className = "badge danger";
    } else {
      badge.textContent = "未连接";
    }

    connectBtn.disabled = busy || state.readonly || !state.conversationId;
    connectBtn.textContent = state.localNeedsPermission
      ? "重新授权"
      : folder
        ? "重新选择文件夹"
        : "连接文件夹";
    syncBtn.hidden = !(folder && handle && !state.localNeedsPermission) || busy || state.readonly;
    syncBtn.disabled = busy;
    disconnectBtn.hidden = !(folder || handle);
    disconnectBtn.disabled = busy;

    if (busy) return;
    if (folder && handle) {
      setLocalStatus(
        `已授权「${folder.name}」，服务器副本 ${folder.file_count || 0} 个文件。Agent 改完后点“同步回本地”。`
      );
    } else if (folder) {
      setLocalStatus("服务器已有副本，但浏览器需要重新选择/授权这个文件夹，才能同步回本地。");
    } else if (handle && state.localNeedsPermission) {
      setLocalStatus("浏览器需要重新确认这个文件夹的读写权限。");
    } else {
      setLocalStatus("授权一个目录后，Agent 会读写它的服务器副本；改完点“同步回本地”。");
    }
  }

  async function loadLocalFolderState() {
    state.localFolder = null;
    state.localDirHandle = null;
    state.localNeedsPermission = false;
    state.localBaseline = new Map();
    renderLocalBridge();
    const cid = state.conversationId;
    if (!cid) return;
    try {
      const data = await api(`/api/chat/conversations/${encodeURIComponent(cid)}/local-folder`);
      if (cid !== state.conversationId) return;
      state.localFolder = data.connected ? data.folder : null;
    } catch (error) {
      console.warn(error);
    }
    try {
      const handle = await idbGet(localKey("dir"));
      if (handle && typeof handle.queryPermission === "function") {
        const permission = await handle.queryPermission({ mode: "readwrite" });
        if (permission === "granted") {
          state.localDirHandle = handle;
        } else if (permission === "prompt") {
          state.localDirHandle = handle;
          state.localNeedsPermission = true;
        } else {
          await idbDel(localKey("dir"));
        }
      }
    } catch (error) {
      console.warn(error);
    }
    try {
      const baseline = await idbGet(localKey("base"));
      if (baseline && typeof baseline === "object") {
        state.localBaseline = new Map(Object.entries(baseline));
      }
    } catch (error) {
      console.warn(error);
    }
    if (cid === state.conversationId) renderLocalBridge();
  }

  async function collectLocalFiles(rootHandle) {
    const files = [];
    let total = 0;
    const skipped = [];

    async function walk(directory, prefix, depth) {
      if (depth > 16) return;
      for await (const [name, child] of directory.entries()) {
        if (name === "." || name === "..") continue;
        const relative = prefix ? `${prefix}/${name}` : name;
        if (child.kind === "directory") {
          if (LOCAL_SKIP_DIRS.has(name)) continue;
          await walk(child, relative, depth + 1);
          continue;
        }
        const lower = name.toLowerCase();
        if (LOCAL_SKIP_FILES.has(lower) || LOCAL_SKIP_SUFFIXES.some((suffix) => lower.endsWith(suffix))) {
          if (skipped.length < 50) skipped.push(relative);
          continue;
        }
        const file = await child.getFile();
        if (file.size > LOCAL_MAX_FILE_BYTES) {
          if (skipped.length < 50) skipped.push(relative);
          continue;
        }
        if (files.length >= LOCAL_MAX_FILES || total + file.size > LOCAL_MAX_BYTES) {
          if (skipped.length < 50) skipped.push(relative);
          continue;
        }
        files.push({ path: relative, file });
        total += file.size;
      }
    }

    await walk(rootHandle, "", 0);
    return { files, total, skipped };
  }

  async function uploadLocalFolder(handle) {
    setLocalStatus("正在扫描本地文件…");
    const { files, skipped } = await collectLocalFiles(handle);
    if (!files.length) {
      await loadLocalManifestBaseline();
      showToast("文件夹为空，或没有可同步的文件");
      if (skipped.length) showToast("已跳过敏感/超大/超限文件", "err");
      return;
    }
    let done = 0;
    for (let start = 0; start < files.length; start += LOCAL_BATCH_FILES) {
      const batch = files.slice(start, start + LOCAL_BATCH_FILES);
      const form = new FormData();
      for (const item of batch) {
        form.append("paths", item.path);
        form.append("files", item.file, item.file.name);
      }
      const response = await fetch(
        `/api/chat/conversations/${encodeURIComponent(state.conversationId)}/local-folder/upload`,
        {
          method: "POST",
          headers: { "X-Portal-CSRF": state.csrf },
          credentials: "same-origin",
          body: form,
        }
      );
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(errorText(data, response.status));
      done += batch.length;
      setLocalStatus(`上传本地文件 ${done}/${files.length}…`);
    }
    await loadLocalManifestBaseline();
    const note = skipped.length ? `，跳过 ${skipped.length} 个敏感/超大文件` : "";
    showToast(`本地文件夹已连接：${files.length} 个文件${note}`);
  }

  async function loadLocalManifestBaseline() {
    const data = await api(`/api/chat/conversations/${encodeURIComponent(state.conversationId)}/local-folder/files`);
    state.localFolder = data.folder;
    state.localBaseline = new Map((data.files || []).map((item) => [item.path, item.sha256]));
    await idbSet(localKey("base"), Object.fromEntries(state.localBaseline));
  }

  async function connectLocalFolder() {
    if (!state.conversationId) {
      showToast("先选择或新建一个会话", "err");
      return;
    }
    if (state.readonly || state.localBusy) return;
    if (!window.showDirectoryPicker) {
      showToast("当前浏览器不支持文件夹授权，请用 Edge/Chrome", "err");
      return;
    }
    if (state.localDirHandle && state.localNeedsPermission) {
      try {
        const permission = await state.localDirHandle.requestPermission({ mode: "readwrite" });
        if (permission !== "granted") {
          showToast("没有获得文件夹读写权限", "err");
          return;
        }
        state.localNeedsPermission = false;
        await idbSet(localKey("dir"), state.localDirHandle);
        renderLocalBridge();
        showToast("文件夹权限已恢复");
      } catch (error) {
        showToast(error.message, "err");
      }
      return;
    }
    let handle;
    try {
      handle = await window.showDirectoryPicker({ mode: "readwrite", id: "hermes-local" });
    } catch (error) {
      if (error.name !== "AbortError") showToast(error.message, "err");
      return;
    }
    state.localDirHandle = handle;
    state.localNeedsPermission = false;
    await idbSet(localKey("dir"), handle);
    state.localBusy = true;
    renderLocalBridge();
    try {
      const prepared = await api(
        `/api/chat/conversations/${encodeURIComponent(state.conversationId)}/local-folder`,
        { method: "POST", body: { name: handle.name } }
      );
      state.localFolder = prepared.folder;
      const serverCount = Number(prepared.folder?.file_count || 0);
      let upload = true;
      if (serverCount > 0) {
        upload = window.confirm(
          state.localBaseline.size
            ? "服务器已有这个文件夹的副本。要把本地当前文件重新上传覆盖服务器吗？取消则保留服务器副本，稍后可直接点“同步回本地”。"
            : "服务器已有这个文件夹的副本。重新上传会用本地文件覆盖服务器副本；如果只想拉取服务器改动，请取消。"
        );
      }
      if (upload) await uploadLocalFolder(handle);
      else await loadLocalManifestBaseline();
      await loadLocalFolderState();
    } catch (error) {
      showToast(error.message, "err");
      renderLocalBridge();
    } finally {
      state.localBusy = false;
      renderLocalBridge();
    }
  }

  async function localFileHandle(root, relative, create) {
    const parts = String(relative || "").split("/").filter(Boolean);
    if (!parts.length || parts.some((part) => part === "." || part === "..")) {
      throw new Error("文件路径不合法");
    }
    let directory = root;
    for (const part of parts.slice(0, -1)) {
      directory = await directory.getDirectoryHandle(part, { create: Boolean(create) });
    }
    return directory.getFileHandle(parts[parts.length - 1], { create: Boolean(create) });
  }

  async function sha256Hex(buffer) {
    const digest = await crypto.subtle.digest("SHA-256", buffer);
    return Array.from(new Uint8Array(digest))
      .map((byte) => byte.toString(16).padStart(2, "0"))
      .join("");
  }

  async function syncLocalFolder() {
    if (state.readonly || state.localBusy) return;
    if (!state.localFolder) {
      showToast("这个会话还没有授权本地文件夹", "err");
      return;
    }
    if (!state.localDirHandle) {
      showToast("请先重新选择本地文件夹", "err");
      return;
    }
    if (state.localNeedsPermission) {
      const permission = await state.localDirHandle.requestPermission({ mode: "readwrite" });
      if (permission !== "granted") {
        showToast("没有获得文件夹读写权限", "err");
        return;
      }
      state.localNeedsPermission = false;
      await idbSet(localKey("dir"), state.localDirHandle);
    }
    state.localBusy = true;
    renderLocalBridge();
    try {
      const manifest = await api(
        `/api/chat/conversations/${encodeURIComponent(state.conversationId)}/local-folder/files`
      );
      state.localFolder = manifest.folder;
      const baseline = state.localBaseline;
      const changes = [];
      let conflicts = 0;
      for (const file of manifest.files || []) {
        const base = baseline.get(file.path);
        if (base === file.sha256) continue;
        let localHash = null;
        try {
          const handle = await localFileHandle(state.localDirHandle, file.path, false);
          const localFile = await handle.getFile();
          localHash = await sha256Hex(await localFile.arrayBuffer());
        } catch {
          localHash = null;
        }
        if (localHash && base !== undefined && localHash !== base) conflicts += 1;
        if (localHash && base === undefined && localHash !== file.sha256) conflicts += 1;
        changes.push(file);
      }
      if (!changes.length) {
        showToast("没有需要同步回本地的改动");
        return;
      }
      if (conflicts > 0) {
        const go = window.confirm(
          `服务器有 ${changes.length} 个文件改动，其中 ${conflicts} 个本地文件也被修改过。继续会覆盖本地版本，是否继续？`
        );
        if (!go) return;
      } else if (!baseline.size) {
        const go = window.confirm("首次同步会用服务器副本覆盖本地同名文件，是否继续？");
        if (!go) return;
      }
      let done = 0;
      for (const file of changes) {
        const response = await fetch(
          `/api/chat/conversations/${encodeURIComponent(state.conversationId)}/local-folder/download?path=${encodeURIComponent(file.path)}`,
          { credentials: "same-origin" }
        );
        if (!response.ok) {
          const data = await response.json().catch(() => ({}));
          throw new Error(errorText(data, response.status));
        }
        const buffer = await response.arrayBuffer();
        const handle = await localFileHandle(state.localDirHandle, file.path, true);
        const writable = await handle.createWritable();
        await writable.write(buffer);
        await writable.close();
        baseline.set(file.path, file.sha256);
        done += 1;
        setLocalStatus(`写回本地 ${done}/${changes.length}…`);
      }
      state.localBaseline = baseline;
      await idbSet(localKey("base"), Object.fromEntries(baseline));
      showToast(`已同步 ${changes.length} 个文件到本地`);
    } catch (error) {
      showToast(error.message, "err");
    } finally {
      state.localBusy = false;
      renderLocalBridge();
    }
  }

  async function disconnectLocalFolder() {
    if (state.localBusy) return;
    if (!window.confirm("断开并删除服务器上的副本？本地文件不会被删除。")) return;
    state.localBusy = true;
    renderLocalBridge();
    try {
      if (state.conversationId) {
        await api(`/api/chat/conversations/${encodeURIComponent(state.conversationId)}/local-folder`, {
          method: "DELETE",
        });
      }
      await idbDel(localKey("dir"));
      await idbDel(localKey("base"));
      state.localFolder = null;
      state.localDirHandle = null;
      state.localNeedsPermission = false;
      state.localBaseline = new Map();
      showToast("已断开本地文件夹");
    } catch (error) {
      showToast(error.message, "err");
    } finally {
      state.localBusy = false;
      renderLocalBridge();
    }
  }

  function messageNode(role, content, meta) {
    const wrapper = node("div", `message ${role}`);
    const metaRow = node("div", "message-meta", role === "user" ? "你" : role === "assistant" ? "Agent" : role === "error" ? "错误" : "系统");
    const body = node("div", "message-content", content);
    wrapper.append(metaRow, body);
    const attachments = Array.isArray(meta) ? meta : meta?.attachments;
    const skills = Array.isArray(meta) ? [] : meta?.skills;
    if (Array.isArray(attachments) && attachments.length) {
      const files = node("div", "message-files");
      for (const file of attachments) {
        const chip = node("span", "file-chip");
        chip.append(node("span", "", file.name || "file"));
        chip.append(node("span", "file-size", formatBytes(file.size)));
        files.append(chip);
      }
      wrapper.append(files);
    }
    if (Array.isArray(skills) && skills.length) {
      const files = node("div", "message-files");
      for (const skillId of skills) {
        const skill = state.skills.find((item) => item.id === skillId);
        files.append(node("span", "file-chip", `技能：${skill?.name || skillId}`));
      }
      wrapper.append(files);
    }
    const resultFiles = Array.isArray(meta) ? [] : meta?.files;
    if (Array.isArray(resultFiles) && resultFiles.length) {
      const files = node("div", "message-files");
      for (const file of resultFiles) {
        const link = node("a", "file-chip", file.name || "下载文件");
        link.href = file.url || "#";
        link.download = file.name || "";
        if (file.size) link.append(node("span", "file-size", formatBytes(file.size)));
        files.append(link);
      }
      wrapper.append(files);
    }
    return wrapper;
  }

  function renderMessages(messages) {
    const box = $("messages");
    clear(box);
    if (!messages.length) {
      const empty = node("div", "chat-empty");
      empty.append(node("strong", "", "今天想做什么？"));
      empty.append(
        node("span", "", "上传表格让我分析、生成图表，或者让我查资料、整理文档、连接本地文件夹。")
      );
      const actions = node("div", "chat-empty-actions");
      const suggestions = [
        ["分析表格", "帮我分析一份表格，先告诉我需要哪些文件和数据口径。"],
        ["生成图表", "根据我的数据生成一张图表，先问我文件、指标和图表类型。"],
        ["查资料", "帮我查一下："],
        ["整理文档", "帮我整理一份文档："],
      ];
      for (const [label, prompt] of suggestions) {
        const button = node("button", "quick-action", label);
        button.type = "button";
        button.addEventListener("click", () => {
          const input = $("chat-input");
          input.value = prompt;
          input.focus();
        });
        actions.append(button);
      }
      const localButton = node("button", "quick-action", "连接本地文件夹");
      localButton.type = "button";
      localButton.addEventListener("click", () => {
        if (!state.conversationId) {
          showToast("先新建一个会话，再连接本地文件夹", "err");
          return;
        }
        $("local-connect").click();
      });
      actions.append(localButton);
      empty.append(actions);
      empty.append(node("div", "chat-empty-note", "工作过程会留痕，方便之后回溯。"));
      box.append(empty);
      return;
    }
    for (const message of messages) {
      box.append(messageNode(message.role, message.content, message.meta || {}));
    }
    box.scrollTop = box.scrollHeight;
  }

  function appendLiveMessage(role, content, attachments) {
    const empty = $("messages").querySelector(".chat-empty");
    if (empty) empty.remove();
    const nodeElement = messageNode(role, content, attachments);
    $("messages").append(nodeElement);
    $("messages").scrollTop = $("messages").scrollHeight;
    return nodeElement;
  }

  function appendProgressMessage(text) {
    const empty = $("messages").querySelector(".chat-empty");
    if (empty) empty.remove();
    const element = messageNode("system", text);
    element.classList.add("progress");
    $("messages").append(element);
    $("messages").scrollTop = $("messages").scrollHeight;
    return element;
  }

  // ---------------------------------------------------------------- send/stream

  function paintStreamStatus() {
    if (!state.streaming) return;
    const seconds = Math.max(0, Math.floor((Date.now() - state.streamStartedAt) / 1000));
    const status = $("chat-status");
    status.textContent = `${state.phase || "处理中"} · ${seconds}s`;
    status.className = "badge";
  }

  function startStreamProgress() {
    state.streamStartedAt = Date.now();
    state.phase = "思考中";
    state.toolNodes.clear();
    state.thinkingNode = null;
    if (state.streamTimer) window.clearInterval(state.streamTimer);
    state.streamTimer = window.setInterval(paintStreamStatus, 500);
    paintStreamStatus();
  }

  function stopStreamProgress() {
    if (state.streamTimer) window.clearInterval(state.streamTimer);
    state.streamTimer = null;
  }

  function setPhase(phase) {
    state.phase = phase;
    paintStreamStatus();
  }

  function applyChunkFrame(frame, context) {
    let eventName = "";
    const dataLines = [];
    for (const line of frame.split("\n")) {
      if (line.startsWith("event:")) eventName = line.slice(6).trim();
      else if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
    }
    if (!dataLines.length) return;
    const data = dataLines.join("\n").trim();
    if (!data || data === "[DONE]") return;
    let chunk;
    try {
      chunk = JSON.parse(data);
    } catch {
      return;
    }
    if (eventName === "hermes.tool.progress") {
      const tool = chunk.tool || "工具";
      const label = chunk.label || tool;
      const id = chunk.toolCallId || tool;
      if (chunk.status === "running") {
        setPhase(`调用 ${label}`);
        if (!state.toolNodes.has(id)) {
          state.toolNodes.set(id, appendProgressMessage(`正在执行：${label}`));
        }
      } else if (chunk.status === "completed") {
        const nodeElement = state.toolNodes.get(id);
        if (nodeElement) nodeElement.querySelector(".message-content").textContent = `已完成：${tool}`;
        setPhase("生成中");
      }
      return;
    }
    if (eventName === "hermes.status") {
      const message = chunk.message || chunk.status || chunk.text;
      if (typeof message === "string" && message) setPhase(message);
      return;
    }
    if (eventName === "approval.request" || chunk?.type === "approval.request") {
      const description = chunk.command || chunk.description || "高风险操作";
      appendLiveMessage("system", `等待管理员审批（监管台 → 审批）：${description}`);
      setStatus("等待审批", "danger");
      return;
    }
    if (chunk?.error) {
      const message = typeof chunk.error === "string" ? chunk.error : chunk.error.message || "运行失败";
      appendLiveMessage("error", message);
      context.error = message;
      return;
    }
    const choices = chunk.choices || [];
    if (!choices.length) return;
    const delta = choices[0].delta || {};
    const reasoning = typeof delta.reasoning_content === "string" ? delta.reasoning_content : "";
    if (reasoning) {
      if (!state.thinkingNode) {
        state.thinkingNode = appendProgressMessage("思考中…");
      }
      const short = reasoning.replace(/\s+/g, " ").slice(-260);
      state.thinkingNode.querySelector(".message-content").textContent = `思考：${short}`;
      setPhase("思考中");
    }
    const text = typeof delta.content === "string" ? delta.content : "";
    if (!text) return;
    if (state.thinkingNode) {
      state.thinkingNode.querySelector(".message-content").textContent = "思考完成";
    }
    if (!context.assistantNode) {
      context.assistantNode = appendLiveMessage("assistant", "");
    }
    context.text += text;
    setPhase("生成中");
    context.assistantNode.querySelector(".message-content").textContent = context.text;
    $("messages").scrollTop = $("messages").scrollHeight;
  }

  function runSend() {
    if (state.streaming || state.readonly) return;
    sendMessage({ preventDefault() {} }).catch((error) => showToast(error.message, "err"));
  }

  async function sendMessage(event) {
    event.preventDefault();
    if (state.streaming || state.readonly) return;
    stopWatch();
    const input = $("chat-input");
    const content = input.value.trim();
    if (!content) return;
    if (!state.conversationId) {
      const conversation = await createConversation();
      if (!conversation) return;
    }
    showError("");

    const tenant = currentTenant();
    if (!tenant || !tenant.runtime_ready) {
      appendLiveMessage("error", "该租户还没有配置运行环境。");
      return;
    }

    const filesToUpload = state.pendingFiles.slice();
    let uploaded = [];
    if (filesToUpload.length) {
      setStatus("上传附件…", "");
      const formData = new FormData();
      filesToUpload.forEach((file) => formData.append("files", file, file.name));
      const uploadResponse = await fetch(
        `/api/chat/conversations/${encodeURIComponent(state.conversationId)}/attachments`,
        {
          method: "POST",
          headers: { "X-Portal-CSRF": state.csrf },
          credentials: "same-origin",
          body: formData,
        }
      );
      const uploadData = await uploadResponse.json().catch(() => ({}));
      if (!uploadResponse.ok) {
        showError(errorText(uploadData, uploadResponse.status));
        setStatus("上传失败", "danger");
        return;
      }
      uploaded = uploadData.attachments || [];
    }

    input.value = "";
    clearPendingFiles();
    state.streaming = true;
    setComposerEnabled(false);
    startStreamProgress();
    appendLiveMessage("user", content, uploaded);

    const context = { text: "", assistantNode: null, error: "" };
    try {
      const response = await fetch(`/api/chat/conversations/${encodeURIComponent(state.conversationId)}/messages`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Accept: "text/event-stream",
          "X-Portal-CSRF": state.csrf,
        },
        credentials: "same-origin",
        body: JSON.stringify({
          content,
          attachment_ids: uploaded.map((item) => item.id),
          skill_ids: [...state.selectedSkills],
        }),
      });
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw new Error(errorText(data, response.status));
      }
      if (!response.body) throw new Error("浏览器不支持流式响应");
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let index;
        while ((index = buffer.indexOf("\n\n")) >= 0) {
          const frame = buffer.slice(0, index);
          buffer = buffer.slice(index + 2);
          applyChunkFrame(frame, context);
        }
      }
      if (context.error) throw new Error(context.error);
      setStatus("已完成", "");
    } catch (error) {
      showError(error.message);
      appendLiveMessage("error", error.message);
      setStatus("失败", "danger");
    } finally {
      state.streaming = false;
      stopStreamProgress();
      const tenantNow = currentTenant();
      setComposerEnabled(!!tenantNow && tenantNow.runtime_ready && !state.readonly);
      try {
        await selectConversation(state.conversationId);
        await loadConversations();
      } catch {
        /* the next manual action retries */
      }
    }
  }

  // ---------------------------------------------------------------- boot

  function bind() {
    $("tenant-trigger").addEventListener("click", (event) => {
      event.stopPropagation();
      const menu = $("tenant-menu");
      const open = menu.hidden;
      menu.hidden = !open;
      event.currentTarget.setAttribute("aria-expanded", open ? "true" : "false");
    });
    $("tenant-menu").addEventListener("click", (event) => event.stopPropagation());
    document.addEventListener("click", closeTenantMenu);
    document.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      closeTenantMenu();
      if (!$("memory-modal").hidden) closeMemoryModal();
    });
    $("new-conversation").addEventListener("click", () => {
      createConversation().catch((error) => showToast(error.message, "err"));
    });
    $("memory-manage").addEventListener("click", openMemoryModal);
    $("memory-modal-close").addEventListener("click", closeMemoryModal);
    $("memory-modal").addEventListener("click", (event) => {
      if (event.target === $("memory-modal")) closeMemoryModal();
    });
    $("memory-add").addEventListener("click", () => {
      saveMemory().catch((error) => showToast(error.message, "err"));
    });
    document.querySelectorAll(".scope-option").forEach((button) => {
      button.addEventListener("click", () => setMemoryScope(button.dataset.scope));
    });
    $("local-connect").addEventListener("click", () => {
      connectLocalFolder().catch((error) => showToast(error.message, "err"));
    });
    $("local-sync").addEventListener("click", () => {
      syncLocalFolder().catch((error) => showToast(error.message, "err"));
    });
    $("local-disconnect").addEventListener("click", () => {
      disconnectLocalFolder().catch((error) => showToast(error.message, "err"));
    });
    // The composer is a <form> for semantics, but sending never goes through
    // native form submission: a reload would abort the SSE stream mid-run.
    $("composer").addEventListener("submit", (event) => event.preventDefault());
    $("send-btn").addEventListener("click", runSend);
    $("attach-btn").addEventListener("click", () => $("file-input").click());
    $("file-input").addEventListener("change", (event) => {
      addFiles(event.target.files);
      event.target.value = "";
    });
    const composer = $("composer");
    ["dragenter", "dragover"].forEach((type) => {
      composer.addEventListener(type, (event) => {
        event.preventDefault();
        if (!state.readonly && !state.streaming) composer.classList.add("dragover");
      });
    });
    ["dragleave", "drop"].forEach((type) => {
      composer.addEventListener(type, (event) => {
        event.preventDefault();
        composer.classList.remove("dragover");
      });
    });
    composer.addEventListener("drop", (event) => {
      if (state.readonly || state.streaming) return;
      addFiles(event.dataTransfer.files);
    });
    $("chat-input").addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        runSend();
      }
    });
    $("logout-btn").addEventListener("click", async () => {
      try {
        await api("/api/auth/logout", { method: "POST" });
      } catch {
        /* redirect anyway */
      }
      window.location.href = "/login";
    });
  }

  function clearBootPromptParam() {
    const url = new URL(window.location.href);
    url.searchParams.delete("prompt");
    window.history.replaceState({}, "", url);
  }

  function prefillOnboardPrompt() {
    if (!bootPrompt) return;
    if (bootTenant && state.tenantId !== bootTenant) {
      showToast("项目会话的租户不匹配，请从监管台重新进入", "err");
      clearBootPromptParam();
      return;
    }
    if (bootConversation && state.conversationId !== bootConversation) {
      showToast("项目会话没有找到，请从监管台重新进入", "err");
      clearBootPromptParam();
      return;
    }
    if (state.readonly) {
      showToast("只读账号不能发起项目接手", "err");
      clearBootPromptParam();
      return;
    }
    const input = $("chat-input");
    if (input.value.trim()) return;
    input.value = bootPrompt;
    input.focus();
    setStatus("待发送", "");
    showToast("项目接手指令已填好，确认后点发送");
    clearBootPromptParam();
  }

  async function main() {
    try {
      const me = await api("/api/me");
      state.me = me;
      state.csrf = me.csrf || "";
      $("user-chip").textContent = `${me.user.username} · ${me.user.role === "admin" ? "管理员" : me.user.role === "viewer" ? "只读" : "成员"}`;
      if (me.user.role === "admin") $("dashboard-link").hidden = false;
    } catch {
      return;
    }
    bind();
    renderLocalBridge();
    try {
      await Promise.all([loadTenants(), loadSkills()]);
      prefillOnboardPrompt();
    } catch (error) {
      showError(error.message);
      setStatus("加载失败", "danger");
    }
  }

  main();
})();
