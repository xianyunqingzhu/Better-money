/** DOM 辅助。 */

export function $(selector: string, root: ParentNode = document): HTMLElement {
  const node = root.querySelector(selector);
  if (!node) throw new Error(`element not found: ${selector}`);
  return node as HTMLElement;
}

export function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  attrs: Record<string, string> = {},
  children: (Node | string)[] = [],
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "class") node.className = value;
    else if (key.startsWith("on") && typeof value === "string") {
      // 事件用 data 绑定，避免 innerHTML 注入
      node.setAttribute(key, value);
    } else node.setAttribute(key, value);
  }
  for (const child of children) {
    node.append(typeof child === "string" ? document.createTextNode(child) : child);
  }
  return node;
}

export function escapeHtml(value: unknown): string {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[ch]!);
}

export function fmtMoney(value: number | string): string {
  return "¥" + (Math.round(Number(value) * 100) / 100).toFixed(2);
}

export function fmtShort(value: number | string): string {
  return String(Math.round(Number(value) * 100) / 100);
}

let toastBox: HTMLElement | null = null;

export function toast(message: string, type: "info" | "success" | "error" = "info",
  action?: { label: string; run: () => Promise<void> }) {
  if (!toastBox) {
    toastBox = el("div", { id: "toast-box" });
    document.body.append(toastBox);
  }
  const node = el("div", { class: `toast toast-${type}`, role: "status" }, [el("span", {}, [message])]);
  let timeout: ReturnType<typeof setTimeout>;
  const dismiss = () => {
    clearTimeout(timeout);
    node.classList.remove("show");
    setTimeout(() => node.remove(), 200);
  };
  if (action) {
    const button = el("button", { class: "toast-action", type: "button" }, [action.label]);
    button.addEventListener("click", async () => {
      button.disabled = true;
      clearTimeout(timeout);
      try { await action.run(); dismiss(); }
      catch { button.disabled = false; timeout = setTimeout(dismiss, 8000); }
    });
    node.append(button);
  }
  // 新提示替换旧提示，避免堆叠遮住操作区域。
  toastBox.replaceChildren(node);
  requestAnimationFrame(() => node.classList.add("show"));
  timeout = setTimeout(dismiss, action ? 8000 : 3200);
}

export function confirmDialog(message: string, confirmLabel = "确认"): Promise<boolean> {
  return new Promise((resolve) => {
    const overlay = el("div", { class: "dialog-overlay" });
    const previousFocus = document.activeElement as HTMLElement | null;
    const box = el("div", { class: "dialog-box", role: "alertdialog", "aria-modal": "true", "aria-label": message });
    box.append(
      el("p", { class: "dialog-message" }, [message]),
      el("div", { class: "dialog-actions" }, [
        el("button", { class: "btn ghost", id: "dialog-cancel" }, ["取消"]),
        el("button", { class: "btn danger", id: "dialog-ok" }, [confirmLabel]),
      ]),
    );
    overlay.append(box);
    document.body.append(overlay);
    const finish = (accepted: boolean) => {
      overlay.remove();
      previousFocus?.focus();
      resolve(accepted);
    };
    $("#dialog-cancel", box).addEventListener("click", () => finish(false));
    $("#dialog-ok", box).addEventListener("click", () => finish(true));
    $("#dialog-cancel", box).focus();
    box.addEventListener("keydown", event => {
      if (event.key === "Escape") { event.preventDefault(); finish(false); }
      if (event.key === "Tab") {
        event.preventDefault();
        const next = document.activeElement === $("#dialog-cancel", box) ? "#dialog-ok" : "#dialog-cancel";
        $(next, box).focus();
      }
    });
  });
}

const sheetTimers = new Map<string, ReturnType<typeof setTimeout>>();

export function openSheet(id: string) {
  clearTimeout(sheetTimers.get(id));
  sheetTimers.delete(id);
  const node = $(id);
  node.dataset.closing = "false";
  node.classList.remove("hidden");
  requestAnimationFrame(() => { if (node.dataset.closing !== "true") node.classList.add("open"); });
}

export function closeSheet(id: string) {
  const node = $(id);
  node.dataset.closing = "true";
  node.classList.remove("open");
  clearTimeout(sheetTimers.get(id));
  const delay = matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : 220;
  sheetTimers.set(id, setTimeout(() => { node.classList.add("hidden"); sheetTimers.delete(id); }, delay));
}

export function openOverlay(id: string) {
  const node = $(id);
  node.classList.remove("hidden");
  document.body.classList.add("overlay-open");
}

export function closeOverlay(id: string) {
  $(id).classList.add("hidden");
  document.body.classList.remove("overlay-open");
}
