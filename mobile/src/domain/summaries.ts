/** 总结素材收集（app/summarizer.py gather 的 TS 移植）。 */
import { addDays } from "./dates";
import { centsToDisplay, toCents } from "./money";
import type { AppConfig, GoalRow, SavingsWinRow, TransactionRow } from "./types";

export function periodBounds(
  periodType: "周" | "月",
  anchor: string,
): { start: string; end: string } {
  const date = new Date(anchor + "T00:00:00");
  if (periodType === "周") {
    const weekday = (date.getDay() + 6) % 7; // 周一=0
    const start = addDays(anchor, -weekday);
    return { start, end: addDays(start, 6) };
  }
  const start = `${anchor.slice(0, 7)}-01`;
  const year = Number(anchor.slice(0, 4));
  const month = Number(anchor.slice(5, 7));
  const lastDay = new Date(year, month, 0).getDate();
  return { start, end: `${anchor.slice(0, 7)}-${String(lastDay).padStart(2, "0")}` };
}

export interface SummaryGather {
  period_type: string;
  start: string;
  end: string;
  expense: number;
  income: number;
  tx_count: number;
  cats: [string, number][];
  top_merchants: [string, number][];
  big: TransactionRow[];
  incomes: TransactionRow[];
  days: Record<string, number>;
  estimated_n: number;
  prev_expense: number;
  prev_cats: [string, number][];
  goals: { name: string; price: number; saved: number; status: string }[];
  budget: number;
  month_spent: number;
  month_income_total: number;
  savings_rate: number | null;
  wins_total: number;
  wins_count: number;
}

export function gather(
  cfg: AppConfig,
  periodType: "周" | "月",
  start: string,
  end: string,
  transactions: readonly TransactionRow[],
  goals: readonly GoalRow[],
  wins: readonly SavingsWinRow[],
): SummaryGather {
  const expenseContribution = (tx: TransactionRow) =>
    tx.type === "支出" ? toCents(tx.amount) :
    tx.type === "退款" && !tx.refund_of ? -toCents(tx.amount) : 0;

  const live = transactions.filter((t) => !t.deleted_at && t.date >= start && t.date <= end);
  const expense = live.filter((t) => t.type === "支出" || t.type === "退款")
    .reduce((sum, t) => sum + expenseContribution(t), 0);
  const income = live.filter((t) => t.type === "收入")
    .reduce((sum, t) => sum + toCents(t.amount), 0);

  const catSums = new Map<string, number>();
  for (const tx of live) {
    if (tx.type === "支出" || tx.type === "退款") {
      catSums.set(tx.category, (catSums.get(tx.category) || 0) + expenseContribution(tx));
    }
  }
  const cats: [string, number][] = [...catSums.entries()]
    .filter(([, cents]) => cents > 0)
    .map(([name, cents]): [string, number] => [name, Number(centsToDisplay(cents))])
    .sort((a, b) => b[1] - a[1]);

  const merchantSums = new Map<string, number>();
  for (const tx of live) {
    if (tx.type === "支出" && tx.merchant) {
      merchantSums.set(tx.merchant, (merchantSums.get(tx.merchant) || 0) + toCents(tx.amount));
    }
  }
  const topMerchants: [string, number][] = [...merchantSums.entries()]
    .sort((a, b) => b[1] - a[1])
    .slice(0, 5)
    .map(([name, cents]) => [name, Number(centsToDisplay(cents))]);

  const big = live.filter((t) => t.type === "支出" && t.amount >= 100);
  const incomes = live.filter((t) => t.type === "收入");
  const days: Record<string, number> = {};
  for (const t of live) {
    if (t.type === "支出" || t.type === "退款") {
      days[t.date] = (days[t.date] || 0) + Number(centsToDisplay(expenseContribution(t)));
    }
  }
  const estimatedN = live.filter((t) => t.estimated).length;

  const length = (new Date(end).getTime() - new Date(start).getTime()) / 86400000 + 1;
  const prevStart = addDays(start, -length);
  const prevEnd = addDays(start, -1);
  const prevLive = transactions.filter(
    (t) => !t.deleted_at && t.date >= prevStart && t.date <= prevEnd,
  );
  const prevExpense = prevLive
    .filter((t) => t.type === "支出" || t.type === "退款")
    .reduce((sum, t) => sum + expenseContribution(t), 0);
  const prevCatSums = new Map<string, number>();
  for (const tx of prevLive) {
    if (tx.type === "支出" || tx.type === "退款") {
      prevCatSums.set(tx.category, (prevCatSums.get(tx.category) || 0) + expenseContribution(tx));
    }
  }
  const prevCats: [string, number][] = [...prevCatSums.entries()]
    .filter(([, cents]) => cents > 0)
    .map(([name, cents]): [string, number] => [name, Number(centsToDisplay(cents))]);

  const monthStart = start.slice(0, 7) + "-01";
  const monthLive = transactions.filter((t) => !t.deleted_at && t.date >= monthStart);
  const monthSpent = monthLive
    .filter((t) => t.type === "支出" || t.type === "退款")
    .reduce((sum, t) => sum + expenseContribution(t), 0);
  const monthIncomeTotal = monthLive
    .filter((t) => t.type === "收入")
    .reduce((sum, t) => sum + toCents(t.amount), 0);

  const winsInPeriod = wins.filter((w) => !w.deleted_at && w.date >= start && w.date <= end);
  const winsTotal = winsInPeriod.reduce((sum, w) => sum + toCents(w.amount), 0);

  const savingsRate =
    monthIncomeTotal > 0 ? (monthIncomeTotal - monthSpent) / monthIncomeTotal : null;

  return {
    period_type: periodType,
    start,
    end,
    expense: Number(centsToDisplay(expense)),
    income: Number(centsToDisplay(income)),
    tx_count: live.filter((t) => t.type !== "退款").length,
    cats,
    top_merchants: topMerchants,
    big,
    incomes,
    days,
    estimated_n: estimatedN,
    prev_expense: Number(centsToDisplay(prevExpense)),
    prev_cats: prevCats,
    goals: goals
      .filter((g) => !g.deleted_at)
      .map((g) => ({ name: g.name, price: g.price, saved: g.saved, status: g.status })),
    budget: cfg.monthly_budget,
    month_spent: Number(centsToDisplay(monthSpent)),
    month_income_total: Number(centsToDisplay(monthIncomeTotal)),
    savings_rate: savingsRate !== null ? Number(savingsRate.toFixed(4)) : null,
    wins_total: Number(centsToDisplay(winsTotal)),
    wins_count: winsInPeriod.length,
  };
}

/** 组装总结 prompt（与桌面 summarizer 的输入组装一致）。 */
export function buildSummaryPrompt(
  g: SummaryGather,
  tone: string,
  preset: string,
): string {
  const catText = g.cats.map(([name, value]) => `${name} ${value} 元`).join("、") || "无";
  const merchantText = g.top_merchants.map(([name, value]) => `${name} ${value} 元`).join("、") || "无";
  const bigText = g.big
    .map((t) => `${t.date} ${t.merchant || t.note || "大额支出"} ${t.amount} 元`)
    .join("；") || "无";
  const incomesText = g.incomes
    .map((t) => `${t.date} ${t.category} ${t.amount} 元`)
    .join("；") || "无";
  const winsText =
    g.wins_count > 0 ? `冷静期放弃购买省下 ${g.wins_total} 元（${g.wins_count} 次）` : "无";
  const goalsText = g.goals
    .map((goal) => `${goal.name}（已存 ${goal.saved}/${goal.price}，${goal.status}）`)
    .join("；") || "无";
  const prevText =
    g.prev_expense > 0
      ? `上一周期支出 ${g.prev_expense} 元` +
        (g.prev_cats.length ? `，分类：${g.prev_cats.map(([n, v]) => `${n} ${v}`).join("、")}` : "")
      : "上一周期无支出记录";
  const rateText =
    g.savings_rate === null ? "无法计算" : `${(g.savings_rate * 100).toFixed(1)}%`;
  return [
    preset,
    `区间：${g.start} ~ ${g.end}`,
    `总支出：${g.expense} 元；总收入：${g.income} 元；记录 ${g.tx_count} 笔`,
    `分类：${catText}`,
    `高频商家：${merchantText}`,
    `大额支出：${bigText}`,
    `收入来源：${incomesText}`,
    `估算笔数：${g.estimated_n}`,
    `月预算：${g.budget} 元；本月已花 ${g.month_spent} 元；本月储蓄率 ${rateText}`,
    prevText,
    `目标：${goalsText}`,
    `省下的钱：${winsText}`,
    `语气：${tone}`,
  ].join("\n");
}
