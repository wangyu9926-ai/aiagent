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
  };

  // Project onboarding opens /chat with a prefilled prompt. It is deliberately
  // NOT auto-sent: a cross-site link must not be able to make a logged-in user
  // execute an agent task without reviewing it.
  const bootParams = new URLSearchParams(window.location.search);
  const bootTenant = bootParams.get("tenant") || "";
  const bootConversation = bootParams.get("conversation") || "";
  const bootPrompt = bootParams.get("prompt") || "";

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

  async function loadTenants() {
    const data = await api("/api/me/tenants");
    state.tenants = data.tenants || [];
    const select = $("tenant-select");
    clear(select);
    for (const tenant of state.tenants) {
      const label = tenant.runtime_ready ? tenant.name : `${tenant.name}（未配置运行环境）`;
      select.append(new Option(label, tenant.id));
    }
    if (!state.tenants.length) {
      select.disabled = true;
      $("new-conversation").disabled = true;
      setComposerEnabled(false);
      setStatus("没有租户");
      $("chat-title").textContent = "还没有分配租户";
      $("chat-sub").textContent = "请联系管理员为你的账号分配一个租户。";
      return;
    }
    select.disabled = false;
    const preferred =
      (bootTenant && state.tenants.find((item) => item.id === bootTenant)) ||
      state.tenants.find((item) => item.runtime_ready) ||
      state.tenants[0];
    state.tenantId = preferred.id;
    select.value = state.tenantId;
    state.readonly = preferred.access_role === "viewer" && state.me.user.role !== "admin";
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
    state.tenantId = tenantId;
    const tenant = currentTenant();
    state.readonly = !!tenant && tenant.access_role === "viewer" && state.me.user.role !== "admin";
    state.conversationId = "";
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
    if (state.streaming) return;
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
      watchActiveRun().catch(() => {});
    } catch (error) {
      showToast(error.message, "err");
    }
  }

  function messageNode(role, content, attachments) {
    const wrapper = node("div", `message ${role}`);
    const meta = node("div", "message-meta", role === "user" ? "你" : role === "assistant" ? "Agent" : role === "error" ? "错误" : "系统");
    const body = node("div", "message-content", content);
    wrapper.append(meta, body);
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
    return wrapper;
  }

  function renderMessages(messages) {
    const box = $("messages");
    clear(box);
    if (!messages.length) {
      const empty = node("div", "chat-empty");
      empty.append(node("strong", "", "开始一段受审计的对话"));
      empty.append(node("span", "", "所有消息、模型请求、工具调用都会出现在管理员的监管台。"));
      box.append(empty);
      return;
    }
    for (const message of messages) {
      box.append(messageNode(message.role, message.content, message.meta?.attachments));
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
        body: JSON.stringify({ content, attachment_ids: uploaded.map((item) => item.id) }),
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
    $("tenant-select").addEventListener("change", (event) => {
      selectTenant(event.target.value).catch((error) => showToast(error.message, "err"));
    });
    $("new-conversation").addEventListener("click", () => {
      createConversation().catch((error) => showToast(error.message, "err"));
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
    try {
      await loadTenants();
      prefillOnboardPrompt();
    } catch (error) {
      showError(error.message);
      setStatus("加载失败", "danger");
    }
  }

  main();
})();
