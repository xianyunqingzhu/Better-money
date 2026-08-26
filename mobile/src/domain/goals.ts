/** 目标储蓄分配（app/goals.py 的 TS 移植）。 */
import { toCents } from "./money";
import type { GoalRow } from "./types";

export interface GoalAllocation {
  goalId: number;
  goalName: string;
  amount: number; // 元
}

/** 把金额按优先级分给「冷静期/进行中」且未存满的目标；返回分配明细。 */
export function allocateSavings(
  goals: readonly GoalRow[],
  amount: number,
): GoalAllocation[] {
  let remaining = Math.round(Math.max(toCents(amount), 0));
  if (remaining <= 0) return [];

  const eligible = goals
    .filter((g) => !g.deleted_at && ["冷静期", "进行中", "已暂停"].includes(g.status))
    .filter((g) => toCents(g.saved) < toCents(g.price))
    .sort((a, b) => a.priority - b.priority || a.id - b.id);

  const allocations: GoalAllocation[] = [];
  for (const goal of eligible) {
    if (remaining <= 0) break;
    const capacity = Math.max(toCents(goal.price) - toCents(goal.saved), 0);
    const assigned = Math.min(remaining, capacity);
    if (assigned <= 0) continue;
    allocations.push({
      goalId: goal.id,
      goalName: goal.name,
      amount: assigned / 100,
    });
    remaining -= assigned;
  }
  return allocations;
}

/** 冷静期剩余天数（负数表示已到期）。 */
export function cooldownDaysLeft(goal: GoalRow, nowIso: string): number {
  if (!goal.cooldown_until) return 0;
  const until = new Date(goal.cooldown_until + "T23:59:59");
  const now = new Date(nowIso + "T00:00:00");
  return Math.ceil((until.getTime() - now.getTime()) / 86400000);
}
