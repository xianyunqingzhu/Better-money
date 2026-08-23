/** 图表统计（/api/stats 的 TS 移植）。 */
import { addDays, monthRange, parseDate, toIso } from "./dates";
import { centsToDisplay, toCents } from "./money";
import type { TransactionRow } from "./types";

export interface CategorySlice {
  name: string;
  value: number; // 元
}

export interface DayPoint {
  date: string;
  value: number;
}

export interface WeekPoint {
  label: string;
  value: number;
}

/** 一笔交易对「支出 − 退款」口径的贡献（分）。 */
function expenseContribution(tx: TransactionRow): number {
  if (tx.type === "支出") return toCents(tx.amount);
  if (tx.type === "退款") return -toCents(tx.amount);
  return 0;
}

export function categoryBreakdown(
  transactions: readonly TransactionRow[],
  start: string,
  end: string,
): CategorySlice[] {
  const sums = new Map<string, number>();
  for (const tx of transactions) {
    if (tx.deleted_at) continue;
    if (tx.date < start || tx.date > end) continue;
    if (tx.type !== "支出" && tx.type !== "退款") continue;
    sums.set(tx.category, (sums.get(tx.category) || 0) + expenseContribution(tx));
  }
  const result: CategorySlice[] = [];
  for (const [name, cents] of sums) {
    if (cents <= 0) continue;
    result.push({ name, value: Number(centsToDisplay(cents)) });
  }
  result.sort((a, b) => b.value - a.value);
  return result;
}

export function monthTotals(
  transactions: readonly TransactionRow[],
  start: string,
  end: string,
): { expense: number; income: number } {
  let expense = 0;
  let income = 0;
  for (const tx of transactions) {
    if (tx.deleted_at) continue;
    if (tx.date < start || tx.date > end) continue;
    if (tx.type === "支出" || tx.type === "退款") expense += expenseContribution(tx);
    else if (tx.type === "收入") income += toCents(tx.amount);
  }
  return { expense: Number(centsToDisplay(expense)), income: Number(centsToDisplay(income)) };
}

/** 近 30 天趋势（结束于今天或历史月月末）。 */
export function dailyTrend(
  transactions: readonly TransactionRow[],
  end: string,
  now: Date = new Date(),
): DayPoint[] {
  const today = toIso(now);
  const trendEnd = today < end ? today : end;
  const trendStart = addDays(trendEnd, -29);
  const byDate = new Map<string, number>();
  for (const tx of transactions) {
    if (tx.deleted_at) continue;
    if (tx.date < trendStart || tx.date > trendEnd) continue;
    if (tx.type !== "支出" && tx.type !== "退款") continue;
    byDate.set(tx.date, (byDate.get(tx.date) || 0) + expenseContribution(tx));
  }
  const result: DayPoint[] = [];
  let cursor = trendStart;
  while (cursor <= trendEnd) {
    result.push({
      date: cursor,
      value: Number(centsToDisplay(byDate.get(cursor) || 0)),
    });
    cursor = addDays(cursor, 1);
  }
  void now;
  return result;
}

/** 近 8 周柱状对比（周一起算，最后一项为本周；含今天所在的自然周）。 */
export function weeklyComparison(
  transactions: readonly TransactionRow[],
  now: Date = new Date(),
): WeekPoint[] {
  const today = toIso(now);
  const todayDate = parseDate(today)!;
  const weekday = (todayDate.getDay() + 6) % 7; // 周一=0（周日=6）
  const thisMonday = addDays(today, -weekday);
  const eightWeeksAgo = addDays(thisMonday, -7 * 7);
  const byDate = new Map<string, number>();
  for (const tx of transactions) {
    if (tx.deleted_at) continue;
    if (tx.date < eightWeeksAgo) continue;
    if (tx.type !== "支出" && tx.type !== "退款") continue;
    byDate.set(tx.date, (byDate.get(tx.date) || 0) + expenseContribution(tx));
  }
  const result: WeekPoint[] = [];
  for (let i = 0; i < 8; i++) {
    const ws = addDays(eightWeeksAgo, 7 * i);
    const we = addDays(ws, 6);
    let total = 0;
    let cursor = ws;
    while (cursor <= we) {
      total += byDate.get(cursor) || 0;
      cursor = addDays(cursor, 1);
    }
    const label = i === 7 ? "本周" : `${Number(ws.slice(5, 7))}/${Number(ws.slice(8, 10))}周`;
    result.push({ label, value: Number(centsToDisplay(total)) });
  }
  return result;
}

export function monthList(transactions: readonly TransactionRow[]): string[] {
  const months = new Set<string>();
  for (const tx of transactions) {
    if (tx.deleted_at) continue;
    months.add(tx.date.slice(0, 7));
  }
  return [...months].sort().reverse();
}

export function statsForMonth(
  month: string,
  transactions: readonly TransactionRow[],
  now: Date = new Date(),
) {
  const { first, last } = monthRange(month, now);
  const totals = monthTotals(transactions, first, last);
  return {
    month: first.slice(0, 7),
    month_expense: totals.expense,
    month_income: totals.income,
    category: categoryBreakdown(transactions, first, last),
    daily: dailyTrend(transactions, last, now),
    weekly: weeklyComparison(transactions, now),
  };
}
