import type { App } from "../app";
import { TodoRepo, type Todo } from "../db/todos";
import { $, el, openOverlay } from "./dom";

let busy = false;
let history = false;

async function change(ctx: App, work: (repo: TodoRepo) => void): Promise<boolean> {
  if (busy) return false;
  busy = true;
  const repo = new TodoRepo(ctx.repo.db);
  const before = [...repo.list(), ...repo.list(true)];
  try {
    work(repo);
    await ctx.repo.save();
    renderHomeTodos(ctx);
    return true;
  } catch (error) {
    // 保存失败时保留原来的可操作列表，避免事项看似完成却没有落盘。
    ctx.repo.db.transaction(() => {
      ctx.repo.db.run("DELETE FROM todos");
      for (const row of before) ctx.repo.db.run(
        "INSERT INTO todos (id, title, priority, created_at, updated_at, completed_at) VALUES (?, ?, ?, ?, ?, ?)",
        [row.id, row.title, row.priority, row.created_at, row.updated_at, row.completed_at],
      );
    });
    ctx.toast(`未保存：${error instanceof Error ? error.message : String(error)}`, "error");
    renderHomeTodos(ctx);
    return false;
  } finally {
    busy = false;
  }
}

export function renderHomeTodos(ctx: App) {
  const list = $("#home-todos");
  const rows = new TodoRepo(ctx.repo.db).list();
  list.replaceChildren();
  $("#home-todos-count").textContent = `（${rows.length}）`;
  if (!rows.length) list.append(el("p", { class: "muted small" }, ["暂无待办，可在第二页「待办事项」中新建。"]));
  for (const row of rows) {
    const checkbox = el("input", { type: "checkbox", "aria-label": `完成：${row.title}` });
    checkbox.addEventListener("change", async () => {
      checkbox.disabled = true;
      await change(ctx, repo => repo.complete(row.id));
      checkbox.checked = false;
      checkbox.disabled = false;
    });
    list.append(el("label", { class: "todo-check" }, [checkbox, el("span", {}, [row.title])]));
  }
}

export function openTodos(ctx: App) {
  history = false;
  openOverlay("#overlay-todos");
  renderManager(ctx);
}

function renderManager(ctx: App) {
  const body = $("#todos-body");
  body.replaceChildren();
  $("#todos-title").textContent = history ? "待办历史记录" : "待办事项";
  const toggle = el("button", { class: "btn ghost" }, [history ? "返回未完成" : "历史记录"]);
  toggle.addEventListener("click", () => { history = !history; renderManager(ctx); });
  const actions = el("div", { class: "btn-row todo-toolbar" }, [toggle]);
  if (!history) {
    const add = el("button", { class: "btn primary" }, ["新建待办"]);
    add.addEventListener("click", () => renderEditor(ctx));
    actions.prepend(add);
  }
  body.append(actions);
  const rows = new TodoRepo(ctx.repo.db).list(history);
  if (!rows.length) body.append(el("p", { class: "empty-state" }, [history ? "还没有已完成的待办事项。" : "暂无待办，点击上方按钮新建。"]));
  rows.forEach((row, index) => {
    const card = el("div", { class: "card todo-item" }, [el("p", { class: "todo-title" }, [row.title])]);
    const buttons = el("div", { class: "btn-row" });
    const edit = el("button", { class: "btn ghost" }, ["编辑"]);
    edit.addEventListener("click", () => renderEditor(ctx, row));
    buttons.append(edit);
    if (history) {
      card.append(el("p", { class: "muted small" }, [`完成于 ${new Date(row.completed_at).toLocaleString("zh-CN")}`]));
      const restore = el("button", { class: "btn primary" }, ["恢复为未完成"]);
      restore.addEventListener("click", async () => {
        restore.disabled = true;
        await change(ctx, repo => repo.restore(row.id));
        renderManager(ctx);
      });
      buttons.append(restore);
    } else {
      for (const [direction, label] of [[-1, "上移"], [1, "下移"]] as const) {
        const move = el("button", { class: "btn ghost", "aria-label": `${label}：${row.title}` }, [label]);
        move.disabled = direction === -1 ? index === 0 : index === rows.length - 1;
        move.addEventListener("click", async () => {
          move.disabled = true;
          await change(ctx, repo => repo.move(row.id, direction));
          renderManager(ctx);
        });
        buttons.append(move);
      }
    }
    card.append(buttons);
    body.append(card);
  });
}

function renderEditor(ctx: App, todo?: Todo) {
  const body = $("#todos-body");
  $("#todos-title").textContent = todo ? "编辑待办" : "新建待办";
  const input = el("textarea", { id: "todo-title-input", maxlength: "500", placeholder: "输入要完成的事项", rows: "4", required: "required" });
  input.value = todo?.title ?? "";
  const save = el("button", { class: "btn primary", type: "submit" }, ["保存"]);
  const cancel = el("button", { class: "btn ghost", type: "button" }, ["取消"]);
  cancel.addEventListener("click", () => renderManager(ctx));
  const form = el("form", {}, [
    el("div", { class: "form-row" }, [el("label", { for: input.id }, ["待办内容（最多 500 字）"]), input]),
    el("div", { class: "btn-row" }, [save, cancel]),
  ]);
  form.addEventListener("submit", async event => {
    event.preventDefault();
    save.disabled = true;
    cancel.disabled = true;
    const saved = await change(ctx, repo => { if (todo) repo.edit(todo.id, input.value); else repo.create(input.value); });
    save.disabled = false;
    cancel.disabled = false;
    if (saved) renderManager(ctx);
  });
  body.replaceChildren(form);
  input.focus();
}
