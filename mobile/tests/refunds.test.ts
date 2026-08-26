/** 退款配对：自动匹配、全额/部分退、手动指定、不配对与统计口径。 */
import { beforeEach, describe, expect, it } from "vitest";
import { LedgerRepo } from "../src/db/repository";
import type { StorageAdapter } from "../src/db/database";
import { monthlySnapshot, summaryCard } from "../src/domain/ledger";
import { categoryBreakdown } from "../src/domain/stats";
import { allocateSavings } from "../src/domain/goals";
import type { GoalRow } from "../src/domain/types";

class MemoryStorage implements StorageAdapter {
  files = new Map<string, Uint8Array>();
  async read(name: string) {
    return this.files.get(name) ?? null;
  }
  async write(name: string, data: Uint8Array) {
    this.files.set(name, data);
  }
  async delete(name: string) {
    this.files.delete(name);
  }
  async exists(name: string) {
    return this.files.has(name);
  }
}

let repo: LedgerRepo;

async function open() {
  repo = await LedgerRepo.open(new MemoryStorage());
}

beforeEach(() => {
  repo?.db.close();
});

async function seedExpense(fields: Record<string, unknown>) {
  repo.addTransaction({
    date: "2026-08-10",
    amount: 89.9,
    type: "支出",
    category: "购物",
    merchant: "淘宝",
    note: "",
    source: "手动",
    ...(fields as object),
  } as never);
  await repo.save();
}

function refundItem(fields: Record<string, unknown>) {
  return {
    date: "2026-08-15",
    amount: 89.9,
    type: "退款",
    category: "购物",
    merchant: "淘宝",
    note: "",
    estimated: 0,
    ...(fields as object),
  } as never;
}

describe("退款配对", () => {
  it("全额退款：原支出软删除并留 tombstone，退款归到原日期", async () => {
    await open();
    await seedExpense({});
    const orig = repo.listTransactions()[0];
    const result = repo.saveItems([refundItem({})]);
    await repo.save();
    expect(result.saved[0].refund_paired).toMatchObject({ full: true, original_uuid: orig.uuid });
    const origAfter = repo.db.queryOne<{ deleted_at: string }>(
      "SELECT deleted_at FROM transactions WHERE uuid = ?",
      [orig.uuid],
    )!;
    expect(origAfter.deleted_at).toBeTruthy();
    const refund = repo.listTransactions().find((t) => t.type === "退款")!;
    expect(refund.refund_of).toBe(orig.uuid);
    expect(refund.date).toBe("2026-08-10");
    expect(refund.note).toContain("配对退货");
    expect(
      repo.db.query("SELECT id FROM sync_tombstones WHERE uuid = ?", [orig.uuid]).length,
    ).toBe(1);
  });

  it("部分退款：原支出扣减退款额并追加备注", async () => {
    await open();
    await seedExpense({ amount: 100 });
    const orig = repo.listTransactions()[0];
    const result = repo.saveItems([refundItem({ amount: 30 })]);
    await repo.save();
    expect(result.saved[0].refund_paired).toMatchObject({ full: false });
    const origAfter = repo.db.queryOne<{ amount: number; note: string }>(
      "SELECT amount, note FROM transactions WHERE uuid = ?",
      [orig.uuid],
    )!;
    expect(origAfter.amount).toBe(70);
    expect(origAfter.note).toContain("退货退 ¥30.00");
  });

  it("多次部分退款：剩余不足时不再自动配对", async () => {
    await open();
    await seedExpense({ amount: 100 });
    const orig = repo.listTransactions()[0];
    repo.saveItems([refundItem({ amount: 30 })]);
    repo.saveItems([refundItem({ amount: 30 })]);
    const third = repo.saveItems([refundItem({ amount: 50 })]);
    await repo.save();
    expect(third.saved[0].refund_paired).toBeUndefined();
    const linked = repo.db.query<{ refund_of: string; amount: number }>(
      "SELECT refund_of, amount FROM transactions WHERE type = '退款'",
    );
    const pairedSum = linked.filter((r) => r.refund_of).reduce((s, r) => s + r.amount, 0);
    expect(pairedSum).toBe(60);
    const standalone = linked.find((r) => !r.refund_of)!;
    expect(standalone.amount).toBe(50);
    expect(
      repo.db.queryOne<{ amount: number }>(
        "SELECT amount FROM transactions WHERE uuid = ?",
        [orig.uuid],
      )!.amount,
    ).toBe(40);
  });

  it("找不到匹配（商家不同）：按独立退款入账", async () => {
    await open();
    await seedExpense({});
    const result = repo.saveItems([refundItem({ merchant: "京东" })]);
    await repo.save();
    expect(result.saved[0].refund_paired).toBeUndefined();
    const refund = repo.listTransactions().find((t) => t.type === "退款")!;
    expect(refund.refund_of).toBe("");
    expect(refund.date).toBe("2026-08-15");
  });

  it("手动指定 refund_of 与 __none__", async () => {
    await open();
    await seedExpense({});
    const orig = repo.listTransactions()[0];
    const manual = repo.saveItems([refundItem({ refund_of: orig.uuid })]);
    expect(manual.saved[0].refund_paired).toBeTruthy();
    await seedExpense({ date: "2026-08-12" });
    const none = repo.saveItems([refundItem({ date: "2026-08-12", refund_of: "__none__" })]);
    expect(none.saved[0].refund_paired).toBeUndefined();
    await repo.save();
  });
});

describe("退款配对后的统计口径", () => {
  it("已配对退款不重复冲减：余额与支出均正确", async () => {
    await open();
    await seedExpense({ amount: 100, date: "2026-08-05" });
    const orig = repo.listTransactions()[0];
    repo.saveItems([refundItem({ amount: 30, date: "2026-08-06" })]);
    await repo.save();

    const txs = repo.allTransactions();
    const card = summaryCard(
      { ...repo.getConfig(), initial_balance: 500, initial_balance_date: "2026-08-01" },
      "2026-08",
      txs,
      [],
      [],
    );
    // 余额 = 500 − 100 + 30（配对退款不参与冲减，靠原支出修正体现）
    expect(card.balance).toBe(430);
    expect(card.monthExpense).toBe(70);
    const cats = categoryBreakdown(txs, "2026-08-01", "2026-08-31");
    expect(cats.find((c) => c.name === "购物")?.value).toBe(70);
    const snap = monthlySnapshot(
      { ...repo.getConfig(), initial_balance: 500, initial_balance_date: "2026-08-01" },
      "2026-08",
      txs,
      [],
      [],
    );
    expect(snap.closingBalance).toBe(430);
    expect(snap.refund).toBe(0); // 已配对退款不进入 refund 汇总
    void orig;
  });
});

describe("目标自动存包含已暂停目标", () => {
  const base: GoalRow = {
    id: 0, name: "", price: 0, saved: 0, priority: 0, status: "进行中",
    cooldown_until: "", expected_date: "", note: "", created_at: "now",
    achieved_at: "", uuid: "", device_id: "", updated_at: "now",
    deleted_at: "", last_synced_at: "",
  };
  it("满额目标跳过，剩余流入已暂停目标", () => {
    const goals: GoalRow[] = [
      { ...base, id: 1, name: "目标一", price: 100, saved: 100, priority: 0 },
      { ...base, id: 2, name: "目标二(已暂停)", price: 200, saved: 0, priority: 1, status: "已暂停" },
    ];
    expect(allocateSavings(goals, 30)).toEqual([
      { goalId: 2, goalName: "目标二(已暂停)", amount: 30 },
    ]);
  });
  it("已达成/已放弃目标仍不参与", () => {
    const goals: GoalRow[] = [
      { ...base, id: 1, name: "已达成", price: 100, saved: 0, priority: 0, status: "已达成" },
      { ...base, id: 2, name: "已放弃", price: 100, saved: 0, priority: 1, status: "已放弃" },
    ];
    expect(allocateSavings(goals, 30)).toEqual([]);
  });
});
