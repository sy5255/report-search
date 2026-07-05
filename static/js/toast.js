/* =========================================================================
   Toast / Confirm — Report-Search
   Replaces all native alert()/confirm() with a consistent, accessible UI.
   Exposed as window.Toast: { success, error, info, warn, confirm }.
   ========================================================================= */
(function () {
  if (window.Toast) return; // singleton

  function ensureContainer() {
    let host = document.getElementById("toast-stack");
    if (!host) {
      host = document.createElement("div");
      host.id = "toast-stack";
      host.setAttribute("aria-live", "polite");
      host.setAttribute("aria-atomic", "false");
      document.body.appendChild(host);
    }
    return host;
  }

  function injectStyles() {
    if (document.getElementById("toast-style")) return;
    const css = document.createElement("style");
    css.id = "toast-style";
    css.textContent = `
      #toast-stack {
        position: fixed;
        right: 20px;
        bottom: 20px;
        z-index: 9000;
        display: flex;
        flex-direction: column;
        gap: 8px;
        max-width: min(420px, calc(100vw - 40px));
        pointer-events: none;
      }
      .toast {
        pointer-events: auto;
        background: var(--color-surface);
        color: var(--color-on-surface);
        border: 1px solid var(--color-outline-variant);
        border-left: 4px solid var(--accent-rag);
        border-radius: 12px;
        padding: 12px 14px 12px 14px;
        box-shadow: var(--shadow-card-hover);
        font-size: 13px;
        line-height: 1.45;
        opacity: 0;
        transform: translateY(10px) scale(0.98);
        transition: opacity .22s var(--ease-out-soft, ease-out),
                    transform .22s var(--ease-out-soft, ease-out);
        display: flex;
        align-items: flex-start;
        gap: 10px;
        min-width: 260px;
      }
      .toast.show { opacity: 1; transform: none; }
      .toast.success { border-left-color: var(--accent-rag); }
      .toast.error   { border-left-color: var(--color-error); }
      .toast.info    { border-left-color: var(--accent-db); }
      .toast.warn    { border-left-color: #d97706; }

      .toast-ico {
        flex: 0 0 22px;
        height: 22px;
        border-radius: 999px;
        display: inline-flex;
        align-items: center;
        justify-content: center;
        background: var(--color-surface-container-low);
        color: var(--color-on-surface-soft);
        margin-top: 1px;
      }
      .toast.success .toast-ico { background: var(--accent-rag-soft); color: var(--accent-rag-strong); }
      .toast.error   .toast-ico { background: rgba(181, 43, 43, 0.12); color: var(--color-error); }
      .toast.info    .toast-ico { background: var(--accent-db-soft); color: var(--accent-db-strong); }
      .toast.warn    .toast-ico { background: rgba(217, 119, 6, 0.14); color: #b45309; }
      .toast-ico .material-symbols-outlined { font-size: 16px; }

      .toast-body { flex: 1 1 auto; min-width: 0; }
      .toast-title { font-weight: 700; font-size: 12.5px; margin-bottom: 2px; }
      .toast-msg { color: var(--color-on-surface-soft); font-size: 12.5px; word-break: break-word; }
      .toast-actions {
        margin-top: 8px;
        display: flex;
        gap: 6px;
        justify-content: flex-end;
      }
      .toast-btn {
        font-size: 12px;
        font-weight: 700;
        padding: 6px 12px;
        border-radius: 8px;
        border: 1px solid var(--color-outline-variant);
        background: var(--color-surface);
        color: var(--color-on-surface);
        cursor: pointer;
        transition: background .15s, border-color .15s, transform .15s;
      }
      .toast-btn:hover { background: var(--color-surface-container-low); }
      .toast-btn.primary {
        background: var(--accent-rag);
        border-color: var(--accent-rag);
        color: #fff;
      }
      .toast-btn.primary:hover { background: var(--accent-rag-strong); }
      .toast-btn.danger {
        background: var(--color-error);
        border-color: var(--color-error);
        color: #fff;
      }

      .toast-close {
        flex: 0 0 20px;
        background: transparent;
        border: 0;
        color: var(--color-secondary);
        cursor: pointer;
        padding: 0;
        font-size: 16px;
        line-height: 1;
        margin-top: -2px;
      }
      .toast-close:hover { color: var(--color-on-surface); }
    `;
    document.head.appendChild(css);
  }

  const ICONS = {
    success: "check_circle",
    error:   "error",
    info:    "info",
    warn:    "warning",
  };

  function makeToast(kind, message, opts) {
    injectStyles();
    const host = ensureContainer();
    opts = opts || {};
    const node = document.createElement("div");
    node.className = "toast " + kind;
    node.setAttribute("role", kind === "error" ? "alert" : "status");

    const safe = String(message == null ? "" : message);
    node.innerHTML = `
      <span class="toast-ico"><span class="material-symbols-outlined">${ICONS[kind] || "info"}</span></span>
      <div class="toast-body">
        ${opts.title ? `<div class="toast-title">${escape(opts.title)}</div>` : ""}
        <div class="toast-msg">${escape(safe)}</div>
        ${opts.actionsHtml || ""}
      </div>
      ${opts.persistent ? "" : `<button class="toast-close" aria-label="닫기">×</button>`}
    `;
    host.appendChild(node);
    requestAnimationFrame(() => node.classList.add("show"));

    function dismiss() {
      node.classList.remove("show");
      setTimeout(() => node.remove(), 220);
    }

    const closeBtn = node.querySelector(".toast-close");
    if (closeBtn) closeBtn.addEventListener("click", dismiss);

    let timer = null;
    const duration = opts.duration != null
      ? opts.duration
      : (kind === "error" ? 6000 : 3500);

    if (!opts.persistent && duration > 0) {
      timer = setTimeout(dismiss, duration);
      node.addEventListener("mouseenter", () => { if (timer) clearTimeout(timer); });
      node.addEventListener("mouseleave", () => { timer = setTimeout(dismiss, 1500); });
    }

    return { node, dismiss };
  }

  function escape(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function confirm(message, opts) {
    injectStyles();
    opts = opts || {};
    return new Promise((resolve) => {
      const host = ensureContainer();
      const node = document.createElement("div");
      node.className = "toast warn";
      node.setAttribute("role", "alertdialog");
      node.innerHTML = `
        <span class="toast-ico"><span class="material-symbols-outlined">help</span></span>
        <div class="toast-body">
          ${opts.title ? `<div class="toast-title">${escape(opts.title)}</div>` : ""}
          <div class="toast-msg">${escape(message)}</div>
          <div class="toast-actions">
            <button class="toast-btn" data-act="no">${escape(opts.cancelText || "취소")}</button>
            <button class="toast-btn ${opts.destructive ? "danger" : "primary"}" data-act="yes">
              ${escape(opts.okText || "확인")}
            </button>
          </div>
        </div>
      `;
      host.appendChild(node);
      requestAnimationFrame(() => node.classList.add("show"));

      function finish(answer) {
        node.classList.remove("show");
        setTimeout(() => node.remove(), 220);
        resolve(answer);
      }
      node.querySelector('[data-act="yes"]').addEventListener("click", () => finish(true));
      node.querySelector('[data-act="no"]').addEventListener("click", () => finish(false));

      // Focus default action
      setTimeout(() => {
        const btn = node.querySelector('[data-act="yes"]');
        if (btn) btn.focus();
      }, 30);
    });
  }

  window.Toast = {
    success: (msg, opts) => makeToast("success", msg, opts),
    error:   (msg, opts) => makeToast("error",   msg, opts),
    info:    (msg, opts) => makeToast("info",    msg, opts),
    warn:    (msg, opts) => makeToast("warn",    msg, opts),
    confirm,
  };
})();
