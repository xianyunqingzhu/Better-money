/** 金额处理：整数分运算，避免浮点误差。 */

export type Cents = number;

/** 元（number/字符串，最多两位小数）→ 分。四舍五入到分。 */
export function toCents(value: number | string): Cents {
  if (typeof value === "number") {
    if (!Number.isFinite(value)) return 0;
    return Math.round(value * 100);
  }
  const text = String(value).replace(/[¥￥元块\s,]/g, "").trim();
  if (!text) return 0;
  const parsed = Number(text);
  if (!Number.isFinite(parsed)) return 0;
  return Math.round(parsed * 100);
}

/** 分 → 元（number，两位小数精度）。 */
export function fromCents(cents: Cents): number {
  return Math.round(cents) / 100;
}

/** 分 → 固定两位小数字符串（共享包与展示用）。 */
export function centsToStr(cents: Cents): string {
  return (Math.round(cents) / 100).toFixed(2);
}

/** 分 → 展示字符串（最多两位小数，去尾零）。 */
export function centsToDisplay(cents: Cents): string {
  return String(Math.round(cents) / 100);
}

export function sumCents(values: Iterable<Cents>): Cents {
  let total = 0;
  for (const value of values) total += Math.round(value);
  return Math.round(total);
}

/** 收入自动存：amount(分) × ratio，四舍五入到分（与桌面 round(x,2) 一致）。 */
export function ratioOf(amountCents: Cents, ratio: number): Cents {
  return Math.round(Math.round(amountCents) * (ratio || 0));
}

/** 今天可花 = (月预算 − 本月支出) / 剩余天数；预算为 0 时返回 0。 */
export function todaySpendable(
  budgetCents: Cents,
  monthExpenseCents: Cents,
  daysLeft: number,
): number {
  if (budgetCents <= 0) return 0;
  const remaining = budgetCents - monthExpenseCents;
  const days = Math.max(daysLeft, 1);
  return remaining / days;
}
