/** TS 余额算法与电脑端 /api/summary 交叉验证。 */
import { readFileSync } from "node:fs";
import { summaryCard, monthlySnapshot } from "../src/domain/ledger";
import type { AppConfig, TransactionRow, GoalRow, AdjustmentRow } from "../src/domain/types";
import { DEFAULT_CONFIG } from "../src/domain/types";

const raw = JSON.parse(readFileSync(process.argv[2]!, "utf-8"));
const config: AppConfig = { ...DEFAULT_CONFIG, ...raw.config };
const txs = raw.transactions as TransactionRow[];
const goals = raw.goals as GoalRow[];
const adjustments = raw.adjustments as AdjustmentRow[];
const month = "2026-08";

const card = summaryCard(config, month, txs, adjustments, goals);
const snap = monthlySnapshot(config, month, txs, adjustments, goals);

console.log("PC  /api/summary:", JSON.stringify(raw.summary));
console.log("TS  summaryCard :", JSON.stringify({
  balance: card.balance,
  month_income: card.monthIncome,
  month_expense: card.monthExpense,
  monthly_budget: card.monthlyBudget,
  today_spendable: card.todaySpendable,
}));
console.log("TS  snapshot    :", JSON.stringify({
  opening: snap.openingBalance,
  closing: snap.closingBalance,
  income: snap.income,
  expense: snap.expense,
  refund: snap.refund,
  transfer: snap.transferOut,
  adjustments: snap.adjustments,
}));

const mismatches: string[] = [];
if (card.balance !== raw.summary.balance) mismatches.push(`balance ${card.balance} != ${raw.summary.balance}`);
if (card.monthIncome !== raw.summary.month_income) mismatches.push(`income ${card.monthIncome} != ${raw.summary.month_income}`);
if (card.monthExpense !== raw.summary.month_expense) mismatches.push(`expense ${card.monthExpense} != ${raw.summary.month_expense}`);
console.log(mismatches.length ? "MISMATCH: " + mismatches.join("; ") : "TS 与电脑端一致 ✓");
