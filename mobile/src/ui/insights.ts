/** 页面二：图表（分类占比/趋势/周对比）、目标进度与历史记录。 */
import * as echarts from "echarts/core";
import { BarChart, LineChart, PieChart } from "echarts/charts";
import { GridComponent, LegendComponent, TooltipComponent } from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import { app, type App } from "../app";
import { todayIso } from "../domain/dates";
import { monthList as computeMonthList, statsForMonth } from "../domain/stats";
import { EXPENSE_CATS, type TransactionRow } from "../domain/types";
import { $, el, escapeHtml, fmtMoney, fmtShort, confirmDialog } from "./dom";
import { openEntryForEdit } from "./entry";
import { renderGoalProgress } from "./goals";

echarts.use([PieChart, LineChart, BarChart, GridComponent, LegendComponent, TooltipComponent, CanvasRenderer]);

const charts: Record<string, echarts.ECharts | null> = {};

function ensureChart(id: string): echarts.ECharts {
  if (!charts[id]) charts[id] = echarts.init($(`#${id}`));
  return charts[id]!;
}

const CAT_COLORS = [
  "#3a9d6e", "#e8a13c", "#5b8def", "#c16ad4", "#e2605f", "#46b6c4", "#8f7fe8", "#9aa43a",
];

let historyFilter = { month: "", type: "", category: "", keyword: "" };
let historyVisible = 100;

export function renderInsights(ctx: App) {
  const stats = statsForMonth(ctx.month, ctx.repo.allTransactions());
  $("#cat-month-label").textContent = ctx.month;
  $("#insights-scroll").dataset.rendered = "1";

  // 分类占比环形图 + 列表
  const catChart = ensureChart("chart-cat");
  const catData = stats.category;
  catChart.setOption({
    color: CAT_COLORS,
    tooltip: { trigger: "item", formatter: "{b}: ¥{c} ({d}%)" },
    legend: { show: false },
    series: [
      {
        type: "pie",
        radius: ["52%", "76%"],
        center: ["50%", "50%"],
        avoidLabelOverlap: true,
        itemStyle: { borderRadius: 5, borderColor: "transparent", borderWidth: 2 },
        label: { show: false },
        emphasis: { label: { show: true, fontSize: 13, formatter: "{b}\n¥{c}" } },
        data: catData.map((c) => ({ name: c.name, value: c.value })),
      },
    ],
  });
  const catList = $("#cat-list");
  catList.innerHTML = "";
  if (!catData.length) {
    catList.innerHTML = '<div class="empty-state">本月还没有支出记录。<br>在首页智能解析或手动记账添加第一笔吧。</div>';
  }
  const total = catData.reduce((sum, c) => sum + c.value, 0);
  catData.forEach((c, i) => {
    const row = el("div", { class: "cat-row", "data-cat": c.name });
    row.append(
      el("span", { class: "cat-dot", style: `background:${CAT_COLORS[i % CAT_COLORS.length]}` }),
      el("span", { class: "cat-name" }, [c.name]),
      el("span", { class: "cat-pct" }, [total > 0 ? `${((c.value / total) * 100).toFixed(0)}%` : ""]),
      el("span", { class: "cat-amount" }, [fmtShort(c.value)]),
    );
    row.addEventListener("click", () => {
      openHistoryOverlay(ctx, c.name);
    });
    catList.append(row);
  });

  // 近 30 天趋势
  const dailyChart = ensureChart("chart-daily");
  dailyChart.setOption({
    tooltip: { trigger: "axis" },
    grid: { left: 8, right: 8, top: 12, bottom: 4, containLabel: true },
    xAxis: {
      type: "category",
      data: stats.daily.map((d) => d.date.slice(5)),
      axisLabel: { fontSize: 10, interval: 6 },
    },
    yAxis: { type: "value", splitLine: { lineStyle: { color: "#e4e0d6", opacity: 0.5 } } },
    series: [
      {
        type: "line",
        data: stats.daily.map((d) => d.value),
        smooth: true,
        symbol: "none",
        lineStyle: { width: 2, color: "#3a9d6e" },
        areaStyle: { opacity: 0.12, color: "#3a9d6e" },
      },
    ],
  });

  // 近 8 周对比
  const weekChart = ensureChart("chart-week");
  weekChart.setOption({
    tooltip: { trigger: "axis" },
    grid: { left: 8, right: 8, top: 12, bottom: 4, containLabel: true },
    xAxis: {
      type: "category",
      data: stats.weekly.map((w) => w.label),
      axisLabel: { fontSize: 10, interval: 0 },
    },
    yAxis: { type: "value", splitLine: { lineStyle: { color: "#e4e0d6", opacity: 0.5 } } },
    series: [
      {
        type: "bar",
        data: stats.weekly.map((w) => w.value),
        barMaxWidth: 18,
        itemStyle: { color: "#5b8def", borderRadius: [5, 5, 0, 0] },
      },
    ],
  });

  renderGoalProgress(ctx);
}

export function openHistoryOverlay(ctx: App, category = "") {
  historyFilter = { month: ctx.month, type: "", category, keyword: "" };
  historyVisible = 100;
  $("#history-filters").dataset.bound = "";
  $("#overlay-history").classList.remove("hidden");
  document.body.classList.add("overlay-open");
  renderHistory(ctx);
}

export function renderHistory(ctx: App) {
  if ($("#overlay-history").classList.contains("hidden")) return;
  const box = $("#history-list");
  const filters = $("#history-filters");
  if (filters.dataset.bound !== "1") {
    filters.dataset.bound = "1";
    filters.innerHTML = "";

    const monthSel = el("select", { id: "hist-month" });
    monthSel.append(el("option", { value: "" }, ["全部月份"]));
    for (const m of new Set([ctx.month, ...computeMonthList(ctx.repo.allTransactions())])) {
      monthSel.append(el("option", { value: m }, [m]));
    }
    const typeSel = el("select", { id: "hist-type" });
    typeSel.append(el("option", { value: "" }, ["全部类型"]));
    for (const t of ["支出", "收入", "退款", "取现", "转账", "还款"]) {
      typeSel.append(el("option", { value: t }, [t]));
    }
    const catSel = el("select", { id: "hist-category" });
    catSel.append(el("option", { value: "" }, ["全部分类"]));
    for (const c of [...new Set([...EXPENSE_CATS, "—"])]) {
      catSel.append(el("option", { value: c }, [c]));
    }
    const keyword = el("input", { id: "hist-keyword", type: "search", placeholder: "搜索商家/备注" });
    for (const [id, key] of [
      ["hist-month", "month"],
      ["hist-type", "type"],
      ["hist-category", "category"],
      ["hist-keyword", "keyword"],
    ] as const) {
      const node = id === "hist-keyword" ? keyword : id === "hist-month" ? monthSel : id === "hist-type" ? typeSel : catSel;
      node.value = historyFilter[key];
      node.addEventListener("input", () => {
        historyFilter[key] = node.value;
        historyVisible = 100;
        renderHistory(ctx);
      });
    }
    filters.append(monthSel, typeSel, catSel, keyword);
    monthSel.value = historyFilter.month || ctx.month;
    historyFilter.month = monthSel.value;
  }

  const rows = ctx.repo.listTransactionsFiltered(historyFilter, historyVisible + 1);
  const hasMore = rows.length > historyVisible;

  box.innerHTML = "";
  if (!rows.length) {
    box.innerHTML = '<div class="empty-state">没有符合条件的记录。</div>';
    return;
  }
  for (const row of rows.slice(0, historyVisible)) {
    box.append(historyCard(ctx, row));
  }
  if (hasMore) {
    const more = el("button", { class: "btn ghost", id: "history-more" }, ["加载更多记录"]);
    more.addEventListener("click", () => {
      historyVisible += 100;
      renderHistory(ctx);
    });
    box.append(more);
  }
}

function historyCard(ctx: App, row: TransactionRow): HTMLElement {
  const isExpense = row.type === "支出";
  const isIncome = row.type === "收入";
  const isLinkedRefund = row.type === "退款" && !!row.refund_of;
  const amountClass = isExpense ? "expense" : isIncome ? "income" : "";
  const typePrefix = isLinkedRefund
    ? "退货退款 "
    : row.type === "支出" || row.type === "收入"
      ? ""
      : row.type + " ";
  const card = el("div", { class: "history-card", "data-id": String(row.id) });
  card.append(
    el("div", { class: "history-head" }, [
      el("span", { class: `history-amount ${amountClass}` }, [
        `${typePrefix}${isExpense ? "−" : isIncome ? "+" : ""}${fmtMoney(row.amount)}`,
      ]),
      el("span", { class: "muted small" }, [row.date]),
    ]),
    el("div", { class: "history-meta" }, [
      el("span", {}, [row.category]),
      row.merchant ? el("span", {}, [`@${row.merchant}`]) : el("span"),
      row.note ? el("span", {}, [escapeHtml(row.note)]) : el("span"),
      isLinkedRefund ? el("span", { style: "color:var(--accent)" }, ["已修正原支出"]) : el("span"),
      row.estimated ? el("span", {}, ["估算"]) : el("span"),
      row.source !== "手动" ? el("span", {}, [`来源：${row.source}`]) : el("span"),
    ]),
  );
  if (row.source === "截图" || row.source === "小票") {
    card.append(el("div", { class: "muted small" }, ["原图仅保存在记录产生的设备"]));
  }
  const actions = el("div", { class: "history-actions" });
  const editBtn = el("button", { class: "btn ghost" }, ["编辑"]);
  editBtn.addEventListener("click", () => openEntryForEdit(ctx, row));
  const delBtn = el("button", { class: "btn ghost" }, ["删除"]);
  delBtn.addEventListener("click", async () => {
    if (!(await confirmDialog("删除这笔记录？下次共享后，另一台设备也会同步删除。"))) return;
    ctx.repo.softDeleteTransaction(row.id);
    await ctx.afterDataChange();
    app.toast("已删除", "success");
  });
  actions.append(editBtn, delBtn);
  card.append(actions);
  return card;
}

export function currentMonthLabel(): string {
  return todayIso().slice(0, 7);
}
