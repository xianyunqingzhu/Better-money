/** 页面一：概览五项数据、预算提醒与快速手动记账。 */
import { app, type App } from "../app";
import { monthBounds, todayIso } from "../domain/dates";
import { summaryCard } from "../domain/ledger";
import { EXPENSE_CATS, INCOME_CATS, VALID_TYPES } from "../domain/types";
import { $, el, fmtMoney } from "./dom";

const CATS_BY_TYPE: Record<string, string[]> = {
  支出: EXPENSE_CATS,
  收入: INCOME_CATS,
  退款: EXPENSE_CATS,
  取现: ["—"],
  转账: ["—"],
  还款: ["—"],
};

export function categoryOptions(type: string): string[] {
  return CATS_BY_TYPE[type] || EXPENSE_CATS;
}

export function renderHome(ctx: App) {
  const { first, last } = monthBounds(ctx.month);
  $("#home-month").textContent = ctx.month;
  const card = summaryCard(
    ctx.config,
    ctx.month,
    ctx.repo.allTransactions(),
    ctx.repo.allAdjustments(),
    ctx.repo.allGoals(),
  );

  const stats: [string, string, string][] = [
    ["balance", "当前余额", fmtMoney(card.balance)],
    ["expense", "本月支出", fmtMoney(card.monthExpense)],
    ["income", "本月收入", fmtMoney(card.monthIncome)],
    ["budget", "月预算", fmtMoney(card.monthlyBudget)],
    ["spendable", "今日可花", fmtMoney(card.todaySpendable)],
  ];
  const grid = $("#stat-grid");
  grid.innerHTML = "";
  for (const [key, label, value] of stats) {
    grid.append(
      el("div", { class: `stat-card ${key}` }, [
        el("div", { class: "stat-label" }, [label]),
        el("div", { class: "stat-value" }, [value]),
      ]),
    );
  }

  // 预算提醒
  const alert = $("#budget-alert");
  const budget = card.monthlyBudget;
  const ratio = card.budgetRatio;
  if (budget <= 0) {
    alert.classList.add("hidden");
  } else {
    alert.classList.remove("hidden");
    const remaining = budget - card.monthExpense;
    if (ratio > 1) {
      alert.className = "budget-alert danger";
      alert.textContent =
        `⚠️ 本月支出已超预算：已花 ${fmtMoney(card.monthExpense)} / ${fmtMoney(budget)}，` +
        `超出 ${fmtMoney(-remaining)}（${(ratio * 100).toFixed(0)}%）。`;
    } else if (ratio > 0.8) {
      alert.className = "budget-alert warn";
      alert.textContent =
        `⚠️ 预算已用 ${(ratio * 100).toFixed(0)}%：已花 ${fmtMoney(card.monthExpense)} / ` +
        `${fmtMoney(budget)}，剩余 ${fmtMoney(remaining)}。`;
    } else {
      alert.classList.add("hidden");
    }
  }

  renderQuickForm(ctx, first, last);
}

function renderQuickForm(ctx: App, _first: string, _last: string) {
  const box = $("#quick-form");
  box.innerHTML = "";
  const type = el("select", { id: "quick-type" });
  for (const t of VALID_TYPES) type.append(el("option", { value: t }, [t]));
  const amount = el("input", {
    id: "quick-amount",
    type: "number",
    inputmode: "decimal",
    step: "0.01",
    min: "0.01",
    placeholder: "金额（元）",
  });
  const date = el("input", { id: "quick-date", type: "date", value: todayIso() });
  const category = el("select", { id: "quick-category" });
  const fillCats = () => {
    category.innerHTML = "";
    for (const c of categoryOptions(type.value)) {
      category.append(el("option", { value: c }, [c]));
    }
  };
  type.addEventListener("change", fillCats);
  fillCats();
  const submit = el("button", { class: "btn primary", id: "quick-submit" }, ["记下这笔"]);
  submit.addEventListener("click", async () => {
    const value = Number(amount.value);
    if (!value || value <= 0) {
      app.toast("请填写有效金额", "error");
      return;
    }
    ctx.repo.addTransaction({
      date: date.value || todayIso(),
      amount: Math.round(value * 100) / 100,
      type: type.value,
      category: category.value,
      merchant: "",
      note: "",
      source: "手动",
    });
    await ctx.afterDataChange();
    app.toast("已记下", "success");
    amount.value = "";
  });
  box.append(
    el("div", { class: "field-grid" }, [type, amount]),
    el("div", { class: "form-row" }, [el("label", {}, ["日期（可补记）"]), date]),
    el("div", { class: "form-row" }, [el("label", {}, ["分类"]), category]),
    submit,
  );
}

export function renderHomeMonth(ctx: App) {
  $("#home-month").textContent = ctx.month;
}
