/** 页面一：待办、概览三项数据、智能解析（内联）与图片入口。 */
import { app, type App } from "../app";
import { parseText } from "../domain/ai";
import { monthBounds, todayIso } from "../domain/dates";
import { summaryCard } from "../domain/ledger";
import { EXPENSE_CATS, INCOME_CATS } from "../domain/types";
import { $, el, fmtMoney } from "./dom";
import { openConfirmPanel } from "./confirm";
import { renderHomeTodos } from "./todos";

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
  renderHomeTodos(ctx);
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

  renderAiEntry(ctx, first, last);
}

function renderAiEntry(ctx: App, _first: string, _last: string) {
  const box = $("#ai-entry");
  if (box.dataset.bound === "1") return;
  box.dataset.bound = "1";
  box.innerHTML = "";

  const date = el("input", { id: "ai-date", type: "date", value: todayIso() });
  const text = el("textarea", {
    id: "ai-text",
    placeholder: "例如：午饭食堂 15\n奶茶 12\n昨天兼职 200\n聚餐 200 4人AA",
  });
  const parseBtn = el("button", { class: "btn primary big-entry", id: "ai-parse" }, ["智能解析并确认"]);
  const status = el("p", { class: "muted small", id: "ai-status" });

  parseBtn.addEventListener("click", async () => {
    const content = text.value.trim();
    if (!content) {
      app.toast("请先输入记账内容", "error");
      return;
    }
    parseBtn.textContent = "解析中…";
    parseBtn.disabled = true;
    status.textContent = "";
    try {
      const result = await parseText(ctx.config, content, date.value || todayIso());
      if (!result.items.length) {
        ctx.repo.storePending(content);
        await ctx.repo.save();
        app.toast(
          result.questions.length
            ? `没有识别出可入账条目：${result.questions.join("；")}`
            : "没有识别出可入账条目",
          "error",
        );
        return;
      }
      text.value = "";
      openConfirmPanel(ctx, result.items, result.questions, "文字");
    } catch (e) {
      ctx.repo.storePending(content);
      await ctx.repo.save();
      status.textContent = `解析失败（原文已保留，可改用手动记账）：${
        e instanceof Error ? e.message : String(e)
      }`;
      app.toast("解析失败，原文已保留", "error");
    } finally {
      parseBtn.textContent = "智能解析并确认";
      parseBtn.disabled = false;
    }
  });

  box.append(
    el("div", { class: "form-row" }, [el("label", {}, ["日期（可补记）"]), date]),
    text,
    el("div", { class: "btn-row", style: "margin-top:10px" }, [parseBtn]),
    status,
  );
}

export function renderHomeMonth(ctx: App) {
  $("#home-month").textContent = ctx.month;
}
