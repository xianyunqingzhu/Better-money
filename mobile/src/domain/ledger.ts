/** 账本算术：日期化初始余额、月度滚动与计划金额（app/ledger.py 的 TS 移植）。 */
import { addDays, monthBounds, parseDate, todayIso } from "./dates";
import { Cents, centsToDisplay, sumCents, toCents } from "./money";
import type { AdjustmentRow, AppConfig, GoalRow, TransactionRow } from "./types";

export interface LedgerSnapshot {
  openingBalance: number; // 元
  income: number;
  refund: number;
  expense: number;
  transferOut: number;
  adjustments: number;
  closingBalance: number;
  plannedAmount: number;
  unplannedBalance: number;
  periodStart: string;
  periodEnd: string;
}

export function initialBalanceStart(cfg: AppConfig): string {
  const raw = (cfg.initial_balance_date || "").trim();
  return parseDate(raw) ? raw : todayIso();
}

const LIVE_TRANSFER_TYPES = ["取现", "转账", "还款"];

/** 从 startDate 起（含）到 throughDate（含）的余额（分）。 */
export function calculateBalance(
  cfg: AppConfig,
  transactions: readonly TransactionRow[],
  adjustments: readonly AdjustmentRow[],
  throughDate?: string,
): Cents {
  const start = initialBalanceStart(cfg);
  const initial = toCents(cfg.initial_balance);
  let income = 0;
  let refund = 0;
  let expense = 0;
  let transferOut = 0;
  let adj = 0;
  for (const tx of transactions) {
    if (tx.deleted_at) continue;
    if (tx.date < start) continue;
    if (throughDate !== undefined && tx.date > throughDate) continue;
    const cents = toCents(tx.amount);
    if (tx.type === "收入") income += cents;
    else if (tx.type === "退款") refund += cents;
    else if (tx.type === "支出") expense += cents;
    else if (LIVE_TRANSFER_TYPES.includes(tx.type)) transferOut += cents;
  }
  for (const a of adjustments) {
    if (a.date < start) continue;
    if (throughDate !== undefined && a.date > throughDate) continue;
    adj += toCents(a.diff);
  }
  return Math.round(initial + income + refund - expense - transferOut + adj);
}

export function plannedAmount(goals: readonly GoalRow[]): Cents {
  let total = 0;
  for (const goal of goals) {
    if (goal.deleted_at) continue;
    if (!["冷静期", "进行中", "已暂停"].includes(goal.status)) continue;
    total += Math.min(toCents(goal.saved), toCents(goal.price));
  }
  return Math.round(total);
}

export function monthlySnapshot(
  cfg: AppConfig,
  month: string,
  transactions: readonly TransactionRow[],
  adjustments: readonly AdjustmentRow[],
  goals: readonly GoalRow[],
): LedgerSnapshot {
  const { first, last } = monthBounds(month);
  const opening = calculateBalance(cfg, transactions, adjustments, addDays(first, -1));
  const closing = calculateBalance(cfg, transactions, adjustments, last);

  let income = 0;
  let refund = 0;
  let expense = 0;
  let transferOut = 0;
  let adj = 0;
  for (const tx of transactions) {
    if (tx.deleted_at) continue;
    if (tx.date < first || tx.date > last) continue;
    const cents = toCents(tx.amount);
    if (tx.type === "收入") income += cents;
    else if (tx.type === "退款") refund += cents;
    else if (tx.type === "支出") expense += cents;
    else if (LIVE_TRANSFER_TYPES.includes(tx.type)) transferOut += cents;
  }
  for (const a of adjustments) {
    if (a.date >= first && a.date <= last) adj += toCents(a.diff);
  }
  const planned = plannedAmount(goals);
  const unplanned = Math.max(Math.round(closing - planned), 0);
  const round = (cents: Cents) => Number(centsToDisplay(cents));
  return {
    openingBalance: round(opening),
    income: round(income),
    refund: round(refund),
    expense: round(expense),
    transferOut: round(transferOut),
    adjustments: round(adj),
    closingBalance: round(closing),
    plannedAmount: round(planned),
    unplannedBalance: round(unplanned),
    periodStart: first,
    periodEnd: last,
  };
}

/** 首页五项数据 + 预算预警（对应桌面 /api/summary）。 */
export function summaryCard(
  cfg: AppConfig,
  month: string,
  transactions: readonly TransactionRow[],
  adjustments: readonly AdjustmentRow[],
  goals: readonly GoalRow[],
  now: Date = new Date(),
) {
  const snap = monthlySnapshot(cfg, month, transactions, adjustments, goals);
  const monthStart = month + "-01";
  let expense = 0;
  let refund = 0;
  let income = 0;
  for (const tx of transactions) {
    if (tx.deleted_at) continue;
    if (tx.date < monthStart) continue;
    const cents = toCents(tx.amount);
    if (tx.type === "支出") expense += cents;
    else if (tx.type === "退款") refund += cents;
    else if (tx.type === "收入") income += cents;
  }
  const monthExpense = expense - refund;
  const budget = toCents(cfg.monthly_budget);
  const ratio = budget > 0 ? monthExpense / budget : 0;
  const lastDay = new Date(now.getFullYear(), now.getMonth() + 1, 0).getDate();
  const daysLeft = lastDay - now.getDate() + 1;
  const spendable = budget > 0 ? (budget - monthExpense) / Math.max(daysLeft, 1) : 0;
  const round = (cents: number) => Number(centsToDisplay(cents));
  return {
    balance: round(snap.closingBalance),
    monthExpense: round(monthExpense),
    monthIncome: round(income),
    monthlyBudget: round(budget),
    todaySpendable: Number(spendable.toFixed(2)),
    daysLeft,
    budgetRatio: ratio,
    plannedAmount: snap.plannedAmount,
    unplannedBalance: snap.unplannedBalance,
  };
}

export function sumCentsOrZero(values: readonly Cents[]): Cents {
  return sumCents(values);
}
