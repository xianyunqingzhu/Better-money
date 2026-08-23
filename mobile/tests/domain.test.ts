/** 账本算术移植基线：断言与 tests/test_ledger.py、test_goals.py、test_stats.py 对齐。 */
import { describe, expect, it } from "vitest";
import { allocateSavings } from "../src/domain/goals";
import { calculateBalance, monthlySnapshot, plannedAmount, summaryCard } from "../src/domain/ledger";
import { toCents } from "../src/domain/money";
import { categoryBreakdown, dailyTrend, monthList, monthTotals, statsForMonth, weeklyComparison } from "../src/domain/stats";
import type { AdjustmentRow, AppConfig, GoalRow, TransactionRow } from "../src/domain/types";
import { DEFAULT_CONFIG } from "../src/domain/types";

function cfg(overrides: Partial<AppConfig>): AppConfig {
  return { ...DEFAULT_CONFIG, ...overrides };
}

let nextId = 1;
function tx(overrides: Partial<TransactionRow>): TransactionRow {
  const base: TransactionRow = {
    id: nextId++,
    date: "2026-08-01",
    amount: 0,
    type: "支出",
    category: "测试",
    merchant: "",
    note: "",
    source: "手动",
    estimated: 0,
    created_at: "now",
    updated_at: "now",
    uuid: `u${nextId}`,
    device_id: "dev",
    deleted_at: "",
    last_synced_at: "",
  };
  return { ...base, ...overrides };
}

function goal(overrides: Partial<GoalRow>): GoalRow {
  const base: GoalRow = {
    id: nextId++,
    name: "目标",
    price: 100,
    saved: 0,
    priority: 0,
    status: "冷静期",
    cooldown_until: "",
    expected_date: "",
    note: "",
    created_at: "now",
    achieved_at: "",
    uuid: `g${nextId}`,
    device_id: "dev",
    updated_at: "now",
    deleted_at: "",
    last_synced_at: "",
  };
  return { ...base, ...overrides };
}

function adj(date: string, diff: number): AdjustmentRow {
  return {
    id: nextId++,
    date,
    diff,
    note: "对账",
    created_at: "now",
    reverses_adjustment_id: null,
  };
}

describe("monthly snapshot（对照 test_ledger.py）", () => {
  it("rolls forward without manual reset", () => {
    const c = cfg({ initial_balance: 1000, initial_balance_date: "2026-07-15" });
    const txs = [
      tx({ date: "2026-07-20", amount: 200, type: "收入" }),
      tx({ date: "2026-07-25", amount: 50, type: "支出" }),
      tx({ date: "2026-08-02", amount: 100, type: "支出" }),
    ];
    const snap = monthlySnapshot(c, "2026-08", txs, [], []);
    expect(snap.openingBalance).toBe(1150);
    expect(snap.income).toBe(0);
    expect(snap.expense).toBe(100);
    expect(snap.closingBalance).toBe(1050);
  });

  it("excludes transactions before initial balance date", () => {
    const c = cfg({ initial_balance: 100, initial_balance_date: "2026-07-10" });
    const txs = [
      tx({ date: "2026-07-01", amount: 999, type: "收入" }),
      tx({ date: "2026-07-10", amount: 20, type: "支出" }),
    ];
    const snap = monthlySnapshot(c, "2026-07", txs, [], []);
    expect(snap.openingBalance).toBe(100);
    expect(snap.closingBalance).toBe(80);
  });

  it("refunds transfers and adjustments affect balance", () => {
    const c = cfg({ initial_balance: 1000, initial_balance_date: "2026-08-01" });
    const txs = [
      tx({ date: "2026-08-03", amount: 200, type: "退款" }),
      tx({ date: "2026-08-04", amount: 100, type: "取现" }),
      tx({ date: "2026-08-05", amount: 60, type: "支出" }),
    ];
    const snap = monthlySnapshot(c, "2026-08", txs, [adj("2026-08-06", 25)], []);
    expect(snap.refund).toBe(200);
    expect(snap.transferOut).toBe(100);
    expect(snap.expense).toBe(60);
    expect(snap.adjustments).toBe(25);
    expect(snap.closingBalance).toBe(1065);
  });

  it("calculate balance through date", () => {
    const c = cfg({ initial_balance: 100, initial_balance_date: "2026-08-01" });
    const txs = [
      tx({ date: "2026-08-01", amount: 50, type: "支出" }),
      tx({ date: "2026-08-10", amount: 30, type: "支出" }),
      tx({ date: "2026-08-20", amount: 10, type: "支出" }),
    ];
    expect(calculateBalance(c, txs, [], "2026-08-10")).toBe(2000); // 20 元
  });

  it("period bounds", () => {
    const c = cfg({ initial_balance: 0, initial_balance_date: "2026-01-01" });
    const snap = monthlySnapshot(c, "2026-02", [], [], []);
    expect(snap.periodStart).toBe("2026-02-01");
    expect(snap.periodEnd).toBe("2026-02-28");
  });

  it("planned amount sums planned goals and unplanned floor", () => {
    const c = cfg({ initial_balance: 500, initial_balance_date: "2026-08-01" });
    const goals = [
      goal({ name: "相机", price: 100, saved: 40, status: "进行中" }),
      goal({ name: "旅行", price: 200, saved: 999, status: "冷静期" }),
      goal({ name: "电脑", price: 500, saved: 10, status: "已暂停" }),
      goal({ name: "耳机", price: 300, saved: 300, status: "已达成" }),
    ];
    expect(plannedAmount(goals)).toBe(25000); // 250 元
    const snap = monthlySnapshot(c, "2026-08", [], [], goals);
    expect(snap.plannedAmount).toBe(250);
    expect(snap.unplannedBalance).toBe(250);
    const low = monthlySnapshot(
      cfg({ initial_balance: 100, initial_balance_date: "2026-08-01" }),
      "2026-08", [], [], goals,
    );
    expect(low.closingBalance).toBe(100);
    expect(low.unplannedBalance).toBe(0);
  });
});

describe("allocate savings（对照 test_goals.py）", () => {
  it("fills eligible goals in priority order and keeps cents", () => {
    const goals = [
      goal({ id: 1, name: "短目标", price: 1, saved: 0, priority: 0, status: "进行中" }),
      goal({ id: 2, name: "长目标", price: 10, saved: 0, priority: 1, status: "冷静期" }),
    ];
    const result = allocateSavings(goals, 10 * 0.333);
    expect(result).toEqual([
      { goalId: 1, goalName: "短目标", amount: 1 },
      { goalId: 2, goalName: "长目标", amount: 2.33 },
    ]);
  });

  it("ignores full and overfilled goals", () => {
    const goals = [
      goal({ id: 1, price: 50, saved: 50, status: "进行中" }),
      goal({ id: 2, price: 50, saved: 999, status: "进行中" }),
    ];
    expect(allocateSavings(goals, 100)).toEqual([]);
  });

  it("empty goals or nonpositive amount", () => {
    expect(allocateSavings([], 10)).toEqual([]);
    expect(allocateSavings([], 0)).toEqual([]);
    expect(allocateSavings([], -0.01)).toEqual([]);
    expect(allocateSavings([], -100)).toEqual([]);
  });
});

describe("stats（对照 test_stats.py）", () => {
  const seed = [
    tx({ date: "2026-07-20", amount: 40, type: "支出", category: "学习", merchant: "书店" }),
    tx({ date: "2026-07-25", amount: 20, type: "支出", category: "餐饮", merchant: "食堂" }),
    tx({ date: "2026-08-01", amount: 100, type: "支出", category: "餐饮", merchant: "食堂" }),
    tx({ date: "2026-08-02", amount: 30, type: "支出", category: "奶茶咖啡", merchant: "奶茶店" }),
    tx({ date: "2026-08-03", amount: 10, type: "退款", category: "奶茶咖啡", merchant: "奶茶店" }),
    tx({ date: "2026-08-10", amount: 300, type: "收入", category: "兼职" }),
  ];
  const now = new Date(2026, 7, 21); // 2026-08-21

  it("current month totals and category", () => {
    const s = statsForMonth("2026-08", seed, now);
    expect(s.month_expense).toBe(120); // 100+30-10
    expect(s.month_income).toBe(300);
    const catMap = Object.fromEntries(s.category.map((c) => [c.name, c.value]));
    expect(catMap).toEqual({ 餐饮: 100, 奶茶咖啡: 20 });
    expect(s.daily.length).toBe(30);
    expect(s.daily[s.daily.length - 1].date).toBe("2026-08-21");
    expect(s.daily.some((d) => d.date === "2026-08-01" && d.value === 100)).toBe(true);
    expect(s.weekly.length).toBe(8);
    expect(s.weekly.map((w) => w.value)).toContain(130); // 07-27~08-02 周
    expect(s.weekly.map((w) => w.value)).toContain(-10); // 08-03 起的周
    expect(s.weekly[s.weekly.length - 1].label).toBe("本周");
  });

  it("historical month stats end at month end", () => {
    const s = statsForMonth("2026-07", seed, now);
    expect(s.month_expense).toBe(60);
    const catMap = Object.fromEntries(s.category.map((c) => [c.name, c.value]));
    expect(catMap).toEqual({ 学习: 40, 餐饮: 20 });
    expect(s.daily[s.daily.length - 1].date).toBe("2026-07-31");
  });

  it("month list", () => {
    expect(monthList(seed)).toEqual(["2026-08", "2026-07"]);
  });

  it("excludes soft-deleted rows everywhere", () => {
    const deleted = tx({ date: "2026-08-01", amount: 999, type: "支出", category: "餐饮", deleted_at: "2026-08-02 00:00:00" });
    const totals = monthTotals([...seed, deleted], "2026-08-01", "2026-08-31");
    expect(totals.expense).toBe(120);
    expect(categoryBreakdown([...seed, deleted], "2026-08-01", "2026-08-31").length).toBe(2);
    expect(weeklyComparison([...seed, deleted], now).map((w) => w.value)).toContain(130);
    expect(dailyTrend([...seed, deleted], "2026-08-31", now).map((d) => d.value)).toContain(100);
    expect(monthList([...seed, deleted])).toEqual(["2026-08", "2026-07"]);
  });
});

describe("money helpers", () => {
  it("round trips yuan to cents", () => {
    expect(toCents(29.9)).toBe(2990);
    expect(toCents("29.90")).toBe(2990);
    expect(toCents("¥15")).toBe(1500);
    expect(toCents("15块")).toBe(1500);
    expect(toCents(0.1 + 0.2)).toBe(30);
  });
});

describe("余额单位回归（真实电脑数据场景）", () => {
  it("summaryCard 余额不做二次除 100", () => {
    const c = cfg({ initial_balance: 369.16, initial_balance_date: "2026-08-17" });
    const txs = [
      tx({ date: "2026-08-18", amount: 29.9, type: "支出", category: "餐饮" }),
      tx({ date: "2026-08-20", amount: 817.24, type: "收入", category: "兼职" }),
      tx({ date: "2026-08-21", amount: 597.76, type: "支出", category: "购物" }),
    ];
    const card = summaryCard(c, "2026-08", txs, [], [], new Date(2026, 7, 23));
    expect(card.balance).toBe(558.74);
    expect(card.monthIncome).toBe(817.24);
    expect(card.monthExpense).toBe(627.66);
  });

  it("todaySpendable 按元返回", () => {
    const c = cfg({ initial_balance: 0, initial_balance_date: "2026-08-01", monthly_budget: 1000 });
    const txs = [tx({ date: "2026-08-05", amount: 300, type: "支出", category: "餐饮" })];
    const card = summaryCard(c, "2026-08", txs, [], [], new Date(2026, 7, 23));
    // 剩余 700 元 / 剩余 9 天(23~31) = 77.78
    expect(card.todaySpendable).toBe(77.78);
  });
});

describe("周对比周日回归", () => {
  it("周日时本周窗口包含当天所在自然周", () => {
    // 2026-08-23 是周日；08-17~08-23 应为「本周」
    const sunday = new Date(2026, 7, 23);
    const txs = [
      tx({ date: "2026-08-22", amount: 50, type: "支出", category: "餐饮" }),
      tx({ date: "2026-08-17", amount: 20, type: "支出", category: "餐饮" }),
    ];
    const weekly = weeklyComparison(txs, sunday);
    expect(weekly.length).toBe(8);
    expect(weekly[7].label).toBe("本周");
    expect(weekly[7].value).toBe(70); // 08-17~08-23 合计 70
  });
});
