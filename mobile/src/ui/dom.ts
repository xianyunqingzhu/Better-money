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

export function toast(message: string, type: "info" | "success" | "error" = "info") {
  if (!toastBox) {
    toastBox = el("div", { id: "toast-box" });
    document.body.append(toastBox);
  }
  const node = el("div", { class: `toast toast-${type}` }, [message]);
  toastBox.append(node);
  requestAnimationFrame(() => node.classList.add("show"));
  setTimeout(() => {
    node.classList.remove("show");
    setTimeout(() => node.remove(), 250);
  }, 2600);
}

export function confirmDialog(message: string): Promise<boolean> {
  return new Promise((resolve) => {
    const overlay = el("div", { class: "dialog-overlay" });
    const box = el("div", { class: "dialog-box" });
    box.append(
      el("p", { class: "dialog-message" }, [message]),
      el("div", { class: "dialog-actions" }, [
        el("button", { class: "btn ghost", id: "dialog-cancel" }, ["取消"]),
        el("button", { class: "btn danger", id: "dialog-ok" }, ["确认"]),
      ]),
    );
    overlay.append(box);
    document.body.append(overlay);
    $("#dialog-cancel", box).addEventListener("click", () => {
      overlay.remove();
      resolve(false);
    });
    $("#dialog-ok", box).addEventListener("click", () => {
      overlay.remove();
      resolve(true);
    });
  });
}

export function openSheet(id: string) {
  const node = $(id);
  node.classList.remove("hidden");
  requestAnimationFrame(() => node.classList.add("open"));
}

export function closeSheet(id: string) {
  const node = $(id);
  node.classList.remove("open");
  setTimeout(() => node.classList.add("hidden"), 220);
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
