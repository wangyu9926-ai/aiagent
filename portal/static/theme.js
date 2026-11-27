(() => {
  "use strict";

  const STORAGE_KEY = "hermes.theme";
  const THEMES = {
    dark: { label: "黑色", color: "#0b1020" },
    light: { label: "白色", color: "#f5f7fb" },
    warm: { label: "暖白", color: "#f5efe4" },
  };
  const root = document.documentElement;

  function readTheme() {
    try {
      const value = localStorage.getItem(STORAGE_KEY);
      if (value && THEMES[value]) return value;
    } catch {
      /* storage may be unavailable */
    }
    return "dark";
  }

  function applyTheme(name, persist = true) {
    const theme = THEMES[name] ? name : "dark";
    root.dataset.theme = theme;
    if (persist) {
      try {
        localStorage.setItem(STORAGE_KEY, theme);
      } catch {
        /* ignore */
      }
    }
    document.querySelectorAll(".theme-option").forEach((option) => {
      const active = option.dataset.theme === theme;
      option.classList.toggle("active", active);
      option.setAttribute("aria-checked", active ? "true" : "false");
    });
    document.querySelectorAll(".theme-current").forEach((dot) => {
      dot.className = "theme-current";
    });
  }

  // Apply before the stylesheet in <head> paints the first frame.
  applyTheme(readTheme(), false);

  function buildSwitch() {
    if (document.querySelector(".theme-switch")) return;
    const host = document.querySelector(".topbar-right");
    const wrap = document.createElement("div");
    wrap.className = "theme-switch" + (host ? "" : " theme-float");
    const trigger = document.createElement("button");
    trigger.type = "button";
    trigger.className = "theme-trigger";
    trigger.title = "页面颜色";
    trigger.setAttribute("aria-label", "切换页面颜色");
    trigger.setAttribute("aria-haspopup", "menu");
    trigger.setAttribute("aria-expanded", "false");
    const current = document.createElement("span");
    current.className = "theme-current";
    trigger.append(current);

    const menu = document.createElement("div");
    menu.className = "theme-menu";
    menu.setAttribute("role", "menu");
    menu.hidden = true;
    for (const [name, meta] of Object.entries(THEMES)) {
      const option = document.createElement("button");
      option.type = "button";
      option.className = "theme-option";
      option.dataset.theme = name;
      option.setAttribute("role", "menuitemradio");
      const dot = document.createElement("span");
      dot.className = `swatch ${name}`;
      const label = document.createElement("span");
      label.textContent = meta.label;
      const check = document.createElement("span");
      check.className = "theme-check";
      check.textContent = "✓";
      option.append(dot, label, check);
      option.addEventListener("click", () => {
        applyTheme(name);
        close();
      });
      menu.append(option);
    }
    wrap.append(trigger, menu);
    if (host) host.prepend(wrap);
    else document.body.append(wrap);

    function close() {
      menu.hidden = true;
      trigger.setAttribute("aria-expanded", "false");
    }

    trigger.addEventListener("click", (event) => {
      event.stopPropagation();
      const open = menu.hidden;
      menu.hidden = !open;
      trigger.setAttribute("aria-expanded", open ? "true" : "false");
    });
    menu.addEventListener("click", (event) => event.stopPropagation());
    document.addEventListener("click", close);
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") close();
    });
    applyTheme(readTheme(), false);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", buildSwitch);
  } else {
    buildSwitch();
  }

  window.hermesTheme = applyTheme;
})();
