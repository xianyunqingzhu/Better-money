import { afterEach, expect, it } from "vitest";
import { LedgerRepo } from "../src/db/repository";
import type { StorageAdapter } from "../src/db/database";
import { statsForMonth } from "../src/domain/stats";

class MemoryStorage implements StorageAdapter {
  files = new Map<string, Uint8Array>();
  async read(name: string) { return this.files.get(name) ?? null; }
  async write(name: string, data: Uint8Array) { this.files.set(name, data.slice()); }
  async delete(name: string) { this.files.delete(name); }
  async exists(name: string) { return this.files.has(name); }
}

let repo: LedgerRepo | undefined;
afterEach(() => { repo?.db.close(); repo = undefined; });

it("智能解析同日同商家同金额的两笔支出都入账，历史和三项统计同步增加", async () => {
  repo = await LedgerRepo.open(new MemoryStorage());
  const item = { date: "2026-09-30", amount: 18, type: "支出", category: "餐饮", merchant: "食堂" };
  const first = repo.saveItems([item]);
  const second = repo.saveItems([item]);
  expect(first.saved).toHaveLength(1);
  expect(second.saved).toHaveLength(1);
  expect(repo.allTransactions()).toHaveLength(2);
  expect(repo.listTransactionsFiltered({ month: "2026-09" }, 100)).toHaveLength(2);
  const stats = statsForMonth("2026-09", repo.allTransactions(), new Date(2026, 8, 30));
  expect(stats.category.find(c => c.name === "餐饮")?.value).toBe(36);
  expect(stats.daily.at(-1)?.value).toBe(36);
  expect(stats.weekly.at(-1)?.value).toBe(36);
});

it("超过 1000 笔历史后，先按月份筛选再分页，旧记录仍能查到", async () => {
  repo = await LedgerRepo.open(new MemoryStorage());
  repo.db.transaction(() => {
    for (let i = 0; i < 1100; i++) repo!.db.run(
      "INSERT INTO transactions(date, amount, type, category, merchant, created_at, updated_at) VALUES (?, 1, '支出', '餐饮', '旧账', '', '')",
      ["2026-09-15"],
    );
  });
  repo.addTransaction({ date: "2026-01-20", amount: 50, type: "支出", category: "餐饮", merchant: "较早的补记" });
  expect(repo.listTransactionsFiltered({ month: "2026-01" }, 101).map(t => t.merchant)).toContain("较早的补记");
  expect(repo.listTransactionsFiltered({ month: "2026-09" }, 101)).toHaveLength(101);
  expect(repo.listTransactionsFiltered({ month: "2026-09" }, 101, 100)).toHaveLength(101);
});
