(() => {
  const form = document.getElementById("login-form");
  const button = document.getElementById("login-btn");
  const error = document.getElementById("login-error");

  function showError(message) {
    error.textContent = message;
    error.hidden = false;
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    error.hidden = true;
    button.disabled = true;
    button.textContent = "登录中…";
    try {
      const response = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({
          username: document.getElementById("username").value.trim(),
          password: document.getElementById("password").value,
        }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(data.detail || data.message || `登录失败（HTTP ${response.status}）`);
      }
      window.location.href = data.user && data.user.role === "admin" ? "/dashboard" : "/chat";
    } catch (err) {
      showError(err instanceof Error ? err.message : "登录失败");
      button.disabled = false;
      button.textContent = "进入监管台";
    }
  });
})();
