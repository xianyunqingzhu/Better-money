/** 目标清单：完整增删改、状态操作、调拨、省下的钱。 */
import { app, type App } from "../app";
import { cooldownDaysLeft } from "../domain/goals";
import { todayIso } from "../domain/dates";
import type { GoalRow } from "../domain/types";
import { $, el, fmtMoney, confirmDialog, openSheet, closeSheet } from "./dom";

export function renderGoalProgress(ctx: App) {
  const box = $("#goal-progress");
  box.innerHTML = "";
  const goals = ctx.repo.listGoals();
  if (!goals.length) {
    box.innerHTML = '<div class="empty-state">还没有目标。<br>去「目标清单」建立第一个愿望。</div>';
    return;
  }
  for (const goal of goals) {
    const pct = goal.price > 0 ? Math.min(goal.saved / goal.price, 1) : 0;
    const item = el("div", { class: "goal-progress-item" });
    const info = el("div", { class: "goal-progress-info" });
    info.append(
      el("div", { class: "goal-progress-name" }, [goal.name]),
      el("div", { class: "goal-progress-meta" }, [`${goal.status} · 已存 ${fmtMoney(goal.saved)} / ${fmtMoney(goal.price)}`]),
      el("div", { class: "progress-track" }, [
        el("div", { class: "progress-fill", style: `width:${(pct * 100).toFixed(0)}%` }),
      ]),
    );
    item.append(info, el("span", { class: "goal-progress-value" }, [`${(pct * 100).toFixed(0)}%`]));
    item.addEventListener("click", () => {
      openOverlayGoals(ctx);
    });
    box.append(item);
  }
}

export function openOverlayGoals(ctx: App) {
  const overlay = $("#overlay-goals");
  overlay.classList.remove("hidden");
  document.body.classList.add("overlay-open");
  renderGoalsOverlay(ctx);
}

export function renderGoalsOverlay(ctx: App) {
  const body = $("#goals-body");
  if (body.dataset.bound !== "1") {
    body.dataset.bound = "1";
    $("#goal-add").addEventListener("click", () => openGoalSheet(ctx, null));
  }
  if (!document.querySelector("#overlay-goals") || $("#overlay-goals").classList.contains("hidden")) {
    return;
  }
  body.innerHTML = "";
  const goals = ctx.repo.listGoals();
  if (!goals.length) {
    body.innerHTML = '<div class="empty-state">还没有目标。点击右上角「新建」加入愿望清单，冷静期帮你拦住冲动消费。</div>';
    return;
  }
  for (const goal of goals) {
    body.append(goalCard(ctx, goal));
  }
  const month = todayIso().slice(0, 7);
  const wins = ctx.repo.savingsWins(month);
  if (wins.count > 0) {
    body.append(
      el("h3", { style: "margin:18px 0 8px" }, [`本月省下的钱：${fmtMoney(wins.total)}（${wins.count} 次）`]),
    );
    for (const win of wins.items) {
      body.append(
        el("div", { class: "win-row" }, [
          el("span", {}, [win.goal_name]),
          el("span", { class: "goal-progress-value" }, [fmtMoney(win.amount)]),
        ]),
      );
    }
  }
}

function goalCard(ctx: App, goal: GoalRow): HTMLElement {
  const card = el("div", { class: "goal-card" });
  const daysLeft = cooldownDaysLeft(goal, todayIso());
  const statusText =
    goal.status === "冷静期"
      ? `冷静期 · 剩 ${Math.max(daysLeft, 0)} 天`
      : goal.status;
  const pct = goal.price > 0 ? Math.min(goal.saved / goal.price, 1) : 0;
  card.append(
    el("div", { class: "goal-card-head" }, [
      el("span", { class: "goal-card-name" }, [goal.name]),
      el("span", { class: "goal-card-status" }, [statusText]),
    ]),
    el("div", { class: "muted small" }, [
      `已存 ${fmtMoney(goal.saved)} / ${fmtMoney(goal.price)}` +
        (goal.expected_date ? ` · 期望 ${goal.expected_date}` : "") +
        (goal.note ? ` · ${goal.note}` : ""),
    ]),
    el("div", { class: "progress-track", style: "margin-top:8px" }, [
      el("div", { class: "progress-fill", style: `width:${(pct * 100).toFixed(0)}%` }),
    ]),
  );

  const actions = el("div", { class: "goal-card-row" });
  const addBtn = (label: string, fn: () => void, danger = false) => {
    const b = el("button", { class: `btn ghost${danger ? " danger" : ""}` }, [label]);
    b.addEventListener("click", fn);
    actions.append(b);
    return b;
  };

  if (goal.status === "冷静期") {
    addBtn("还想要", async () => {
      ctx.repo.goalAction(goal.id, "want");
      await ctx.afterDataChange();
      app.toast("已进入进行中", "success");
    });
    addBtn("先不买（记下省的钱）", async () => {
      ctx.repo.goalAction(goal.id, "pass");
      await ctx.afterDataChange();
      app.toast(`已记下省下的 ${fmtMoney(goal.price)}`, "success");
    });
  } else if (goal.status === "进行中") {
    addBtn("暂停", async () => {
      ctx.repo.goalAction(goal.id, "pause");
      await ctx.afterDataChange();
    });
    addBtn("我买了", async () => {
      if (!(await confirmDialog(`确认已购买「${goal.name}」？将记一笔 ${fmtMoney(goal.price)} 的购物支出并标记达成。`))) return;
      ctx.repo.goalAction(goal.id, "achieve_buy");
      await ctx.afterDataChange();
      app.toast("已达成并记账", "success");
    });
  } else if (goal.status === "已暂停") {
    addBtn("恢复", async () => {
      ctx.repo.goalAction(goal.id, "resume");
      await ctx.afterDataChange();
    });
  }
  if (goal.status !== "已达成" && goal.status !== "已放弃") {
    addBtn("放弃", async () => {
      if (!(await confirmDialog(`放弃目标「${goal.name}」？已存金额将清零。`))) return;
      ctx.repo.goalAction(goal.id, "abandon");
      await ctx.afterDataChange();
    });
  }
  addBtn("↑", async () => {
    ctx.repo.goalAction(goal.id, "up");
    await ctx.afterDataChange();
  });
  addBtn("↓", async () => {
    ctx.repo.goalAction(goal.id, "down");
    await ctx.afterDataChange();
  });
  addBtn("编辑", () => openGoalSheet(ctx, goal));
  addBtn("删除", async () => {
    if (!(await confirmDialog(`删除目标「${goal.name}」？删除会同步到另一台设备。`))) return;
    ctx.repo.softDeleteGoal(goal.id);
    await ctx.afterDataChange();
    app.toast("已删除", "success");
  }, true);
  card.append(actions);
  return card;
}

export function openGoalSheet(ctx: App, goal: GoalRow | null) {
  const body = $("#goal-sheet-body");
  body.innerHTML = "";
  $("#goal-sheet-title").textContent = goal ? "编辑目标" : "新建目标";

  const name = el("input", { id: "goal-name", placeholder: "想要什么？", value: goal?.name || "" });
  const price = el("input", {
    id: "goal-price", type: "number", inputmode: "decimal", step: "0.01", min: "0.01",
    placeholder: "价格（元）", value: goal ? String(goal.price) : "",
  });
  const saved = el("input", {
    id: "goal-saved", type: "number", inputmode: "decimal", step: "0.01", min: "0",
    placeholder: "已存金额（元）", value: goal ? String(goal.saved) : "0",
  });
  const expected = el("input", { id: "goal-expected", type: "date", value: goal?.expected_date || "" });
  const note = el("input", { id: "goal-note", placeholder: "备注 / 外部链接（可选）", value: goal?.note || "" });
  const submit = el("button", { class: "btn primary", id: "goal-save" }, [goal ? "保存" : "创建目标"]);
  submit.addEventListener("click", async () => {
    const priceValue = Number(price.value);
    if (!name.value.trim() || !priceValue || priceValue <= 0) {
      app.toast("请填写名称和价格", "error");
      return;
    }
    if (goal) {
      ctx.repo.patchGoal(goal.id, {
        name: name.value.trim(),
        price: Math.round(priceValue * 100) / 100,
        saved: Math.round(Number(saved.value || 0) * 100) / 100,
        expected_date: expected.value,
        note: note.value.trim(),
      });
    } else {
      ctx.repo.addGoal({
        name: name.value.trim(),
        price: Math.round(priceValue * 100) / 100,
        expected_date: expected.value,
        note: note.value.trim(),
      });
    }
    await ctx.afterDataChange();
    closeSheet("#goal-sheet");
    app.toast(goal ? "已保存" : "已创建，进入冷静期", "success");
  });
  body.append(
    el("div", { class: "form-row" }, [el("label", {}, ["名称"]), name]),
    el("div", { class: "form-row" }, [el("label", {}, ["价格（元）"]), price]),
    goal
      ? el("div", { class: "form-row" }, [el("label", {}, ["已存金额（手动调拨）"]), saved])
      : el("span"),
    el("div", { class: "form-row" }, [el("label", {}, ["期望日期（可选）"]), expected]),
    el("div", { class: "form-row" }, [el("label", {}, ["备注 / 外部链接（可选）"]), note]),
    submit,
  );
  openSheet("#goal-sheet");
}
