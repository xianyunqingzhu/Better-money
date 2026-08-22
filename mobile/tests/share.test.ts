/** 共享包往返测试：与 tests/test_share.py 场景对齐。 */
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import JSZip from "jszip";
import { LedgerRepo } from "../src/db/repository";
import type { StorageAdapter } from "../src/db/database";
import { localNowSql } from "../src/db/migrations";
import {
  applyImport,
  exportSharePackage,
  parseSharePackage,
  previewImport,
  ShareError,
} from "../src/domain/share";

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

const repos: { a?: LedgerRepo; b?: LedgerRepo } = {};
const storages: { a: MemoryStorage; b: MemoryStorage } = {
  a: new MemoryStorage(),
  b: new MemoryStorage(),
};

/** 远期基准时间：让所有合成时间戳相对顺序确定，且晚于真实时钟。 */
function t(minutes: number): string {
  const date = new Date(Date.UTC(2099, 0, 1) + minutes * 60000);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getUTCFullYear()}-${pad(date.getUTCMonth() + 1)}-${pad(date.getUTCDate())} ` +
    `${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}:00`;
}

async function openA() {
  repos.a = await LedgerRepo.open(storages.a);
  return repos.a!;
}

async function openB() {
  repos.b = await LedgerRepo.open(storages.b);
  return repos.b!;
}

beforeEach(() => {
  storages.a = new MemoryStorage();
  storages.b = new MemoryStorage();
  delete repos.a;
  delete repos.b;
});

afterEach(() => {
  repos.a?.db.close();
  repos.b?.db.close();
});

async function seedTx(repo: LedgerRepo, fields: Record<string, unknown>) {
  const result = repo.addTransaction({
    date: "2026-08-19",
    amount: 30,
    type: "支出",
    category: "餐饮",
    merchant: "奶茶店",
    ...(fields as object),
  } as never);
  await repo.save();
  return result.id;
}

async function importPackage(
  repo: LedgerRepo,
  zipBytes: Uint8Array,
  decisions = {},
) {
  const pkg = await parseSharePackage(zipBytes);
  const preview = previewImport(repo, pkg);
  const result = await applyImport(repo, pkg, decisions);
  return { preview, result, pkg };
}

describe("导出结构", () => {
  it("package structure and no secrets", async () => {
    const repo = await openA();
    await seedTx(repo, { date: "2026-08-20", amount: 29.9, merchant: "KFC" });
    repo.addGoal({ name: "耳机", price: 1299 });
    await repo.updateConfig({ api_key: "sk-top-secret" });
    await repo.save();

    const exported = await exportSharePackage(repo);
    const zip = await JSZip.loadAsync(exported.zip);
    expect(Object.keys(zip.files).sort()).toEqual([
      "goals.json",
      "line_items.json",
      "manifest.json",
      "savings_wins.json",
      "settings_public.json",
      "tombstones.json",
      "transactions.json",
    ]);
    const manifest = JSON.parse(await zip.file("manifest.json")!.async("string"));
    expect(manifest.format).toBe("better-money-share");
    expect(manifest.counts.transactions).toBe(1);
    const txs = JSON.parse(await zip.file("transactions.json")!.async("string"));
    expect(txs[0].amount).toBe("29.90");
    expect(txs[0].id).toBeUndefined();
    const goals = JSON.parse(await zip.file("goals.json")!.async("string"));
    expect(goals[0].price).toBe("1299.00");
    const allText = await Promise.all(
      Object.keys(zip.files).map((n) => zip.file(n)!.async("string")),
    );
    expect(allText.join("")).not.toContain("sk-top-secret");

    expect(repo.syncStatus().pending_changes).toBe(0);
    expect(repo.listSyncEvents()[0].direction).toBe("export");
  });
});

describe("导入合并", () => {
  it("import into empty device", async () => {
    const a = await openA();
    await seedTx(a, { date: "2026-08-19", amount: 12.5, merchant: "食堂" });
    await seedTx(a, { date: "2026-08-20", amount: 50, type: "收入", category: "兼职" });
    a.addGoal({ name: "相机", price: 500 });
    await a.save();
    const exported = await exportSharePackage(a);

    const b = await openB();
    const { preview, result } = await importPackage(b, exported.zip);
    expect(preview.summary.add_transactions).toBe(2);
    expect(preview.summary.add_goals).toBe(1);
    expect(preview.summary.conflict_days).toEqual([]);
    expect(result.add_transactions).toBe(2);
    expect(result.add_goals).toBe(1);
    expect(b.listTransactions().length).toBe(2);
    expect(b.listGoals().length).toBe(1);
  });

  it("reimport is idempotent", async () => {
    const a = await openA();
    await seedTx(a, { date: "2026-08-19", amount: 30 });
    const exported = await exportSharePackage(a);

    const b = await openB();
    await importPackage(b, exported.zip);
    const { preview, result } = await importPackage(b, exported.zip);
    expect(preview.summary.add_transactions).toBe(0);
    expect(preview.summary.modify_transactions).toBe(0);
    expect(result.add_transactions).toBe(0);
    expect(b.listTransactions().length).toBe(1);
  });

  it("single side edit auto adopts", async () => {
    const a = await openA();
    const id = await seedTx(a, { date: "2026-08-19", amount: 30 });
    const uuid = a.db.query<{ uuid: string }>(
      "SELECT uuid FROM transactions WHERE id = ?",
      [id],
    )[0].uuid;
    const pkg1 = await exportSharePackage(a);

    const b = await openB();
    await importPackage(b, pkg1.zip);

    a.db.run(
      "UPDATE transactions SET amount = 31, updated_at = ? WHERE uuid = ?",
      [t(60), uuid],
    );
    await a.save();
    const pkg2 = await exportSharePackage(a);

    const { preview } = await importPackage(b, pkg2.zip);
    expect(preview.summary.modify_transactions).toBe(1);
    expect(preview.summary.conflict_days).toEqual([]);
    expect(
      b.db.query<{ amount: number }>(
        "SELECT amount FROM transactions WHERE uuid = ?",
        [uuid],
      )[0].amount,
    ).toBe(31);
  });

  it("both edited same day conflicts with three modes", async () => {
    const a = await openA();
    const id = await seedTx(a, { date: "2026-08-19", amount: 30 });
    const uuid = a.db.query<{ uuid: string }>(
      "SELECT uuid FROM transactions WHERE id = ?",
      [id],
    )[0].uuid;
    const pkg1 = await exportSharePackage(a);

    const b = await openB();
    await importPackage(b, pkg1.zip);

    a.db.run(
      "UPDATE transactions SET amount = 31, updated_at = ? WHERE uuid = ?",
      [t(0), uuid],
    );
    await a.save();
    const pkg2 = await exportSharePackage(a);

    b.db.run(
      "UPDATE transactions SET amount = 32, updated_at = ? WHERE uuid = ?",
      [t(30), uuid],
    );
    await b.save();

    const { preview } = await importPackage(b, pkg2.zip, {
      days: { "2026-08-19": { mode: "keep_local" } },
    });
    expect(preview.summary.conflict_days.map((d) => d.date)).toEqual(["2026-08-19"]);
    expect(preview.summary.conflict_days[0].items[0].kind).toBe("both_changed");
    expect(
      b.db.query<{ amount: number }>(
        "SELECT amount FROM transactions WHERE uuid = ?",
        [uuid],
      )[0].amount,
    ).toBe(32);

    // 新的双侧修改 → keep_peer
    a.db.run(
      "UPDATE transactions SET amount = 33, updated_at = ? WHERE uuid = ?",
      [t(60), uuid],
    );
    await a.save();
    const pkg3 = await exportSharePackage(a);
    b.db.run(
      "UPDATE transactions SET amount = 34, updated_at = ? WHERE uuid = ?",
      [t(90), uuid],
    );
    await b.save();
    await importPackage(b, pkg3.zip, {
      days: { "2026-08-19": { mode: "keep_peer" } },
    });
    expect(
      b.db.query<{ amount: number }>(
        "SELECT amount FROM transactions WHERE uuid = ?",
        [uuid],
      )[0].amount,
    ).toBe(33);

    // 再次双侧修改 → merge + drop
    a.db.run(
      "UPDATE transactions SET amount = 35, updated_at = ? WHERE uuid = ?",
      [t(120), uuid],
    );
    await a.save();
    const pkg4 = await exportSharePackage(a);
    b.db.run(
      "UPDATE transactions SET updated_at = ? WHERE uuid = ?",
      [t(150), uuid],
    );
    await b.save();
    await importPackage(b, pkg4.zip, {
      days: { "2026-08-19": { mode: "merge", items: { [uuid]: "drop" } } },
    });
    expect(
      b.db.query<{ deleted_at: string }>(
        "SELECT deleted_at FROM transactions WHERE uuid = ?",
        [uuid],
      )[0].deleted_at,
    ).toBeTruthy();
  });

  it("delete propagates and purges after sync", async () => {
    const a = await openA();
    const id = await seedTx(a, { date: "2026-08-19", amount: 30 });
    const uuid = a.db.query<{ uuid: string }>(
      "SELECT uuid FROM transactions WHERE id = ?",
      [id],
    )[0].uuid;
    const pkg1 = await exportSharePackage(a);

    const b = await openB();
    await importPackage(b, pkg1.zip);

    a.db.run(
      "UPDATE transactions SET deleted_at = ?, updated_at = ? WHERE id = ?",
      [t(120), t(120), id],
    );
    a.db.run(
      `INSERT INTO sync_tombstones(uuid, kind, deleted_at, device_id, synced)
       VALUES (?, 'transaction', ?, ?, 0)`,
      [uuid, t(120), a.localDeviceId()],
    );
    await a.save();
    const pkg2 = await exportSharePackage(a);

    const { preview } = await importPackage(b, pkg2.zip);
    expect(preview.summary.delete_transactions).toBe(1);
    expect(
      b.db.query<{ deleted_at: string }>(
        "SELECT deleted_at FROM transactions WHERE uuid = ?",
        [uuid],
      )[0].deleted_at,
    ).toBeTruthy();

    b.db.run("UPDATE sync_tombstones SET synced = 1");
    await b.save();
    await importPackage(b, pkg2.zip);
    expect(
      b.db.query("SELECT id FROM transactions WHERE uuid = ?", [uuid]).length,
    ).toBe(0);
    expect(
      b.db.query("SELECT id FROM sync_tombstones WHERE uuid = ?", [uuid]).length,
    ).toBe(1);
  });

  it("delete vs modify conflict", async () => {
    const a = await openA();
    const id = await seedTx(a, { date: "2026-08-19", amount: 30 });
    const uuid = a.db.query<{ uuid: string }>(
      "SELECT uuid FROM transactions WHERE id = ?",
      [id],
    )[0].uuid;
    const pkg1 = await exportSharePackage(a);

    const b = await openB();
    await importPackage(b, pkg1.zip);

    a.db.run(
      "UPDATE transactions SET deleted_at = ?, updated_at = ? WHERE id = ?",
      [t(120), t(120), id],
    );
    a.db.run(
      `INSERT INTO sync_tombstones(uuid, kind, deleted_at, device_id, synced)
       VALUES (?, 'transaction', ?, ?, 0)`,
      [uuid, t(120), a.localDeviceId()],
    );
    await a.save();
    const pkg2 = await exportSharePackage(a);

    b.db.run(
      "UPDATE transactions SET amount = 35, updated_at = ? WHERE uuid = ?",
      [t(150), uuid],
    );
    await b.save();

    const { preview } = await importPackage(b, pkg2.zip, {
      days: { "2026-08-19": { mode: "keep_local" } },
    });
    expect(preview.summary.conflict_days[0].items[0].kind).toBe("delete_vs_modify");
    expect(
      b.db.query<{ amount: number }>(
        "SELECT amount FROM transactions WHERE uuid = ?",
        [uuid],
      )[0].amount,
    ).toBe(35);

    b.db.run(
      "UPDATE transactions SET updated_at = ? WHERE uuid = ?",
      [t(180), uuid],
    );
    await b.save();
    await importPackage(b, pkg2.zip, {
      days: { "2026-08-19": { mode: "keep_peer" } },
    });
    expect(
      b.db.query<{ deleted_at: string }>(
        "SELECT deleted_at FROM transactions WHERE uuid = ?",
        [uuid],
      )[0].deleted_at,
    ).toBeTruthy();
  });

  it("resurrect conflict after purge", async () => {
    const a = await openA();
    const id = await seedTx(a, { date: "2026-08-19", amount: 30 });
    const uuid = a.db.query<{ uuid: string }>(
      "SELECT uuid FROM transactions WHERE id = ?",
      [id],
    )[0].uuid;
    const pkg1 = await exportSharePackage(a);

    const b = await openB();
    await importPackage(b, pkg1.zip);

    // B 修改并导出（A 从未收到）
    b.db.run(
      "UPDATE transactions SET amount = 33, updated_at = ? WHERE uuid = ?",
      [t(0), uuid],
    );
    await b.save();
    const pkgB = await exportSharePackage(b);

    // A 删除该笔
    a.db.run(
      `UPDATE transactions SET deleted_at = '${t(-60)}', updated_at = '${t(-60)}' WHERE id = ?`,
      [id],
    );
    a.db.run(
      `INSERT INTO sync_tombstones(uuid, kind, deleted_at, device_id, synced)
       VALUES (?, 'transaction', '${t(-60)}', ?, 0)`,
      [uuid, a.localDeviceId()],
    );
    await a.save();

    const { preview } = await importPackage(a, pkgB.zip, {
      days: { "2026-08-19": { mode: "keep_local" } },
    });
    expect(preview.summary.conflict_days[0].items[0].kind).toBe("delete_vs_modify");
    expect(
      a.db.query<{ deleted_at: string }>(
        "SELECT deleted_at FROM transactions WHERE uuid = ?",
        [uuid],
      )[0].deleted_at,
    ).toBeTruthy();

    // 物理清理后 → resurrect
    a.db.run("UPDATE sync_tombstones SET synced = 1 WHERE uuid = ?", [uuid]);
    await a.save();
    await importPackage(a, pkgB.zip);
    expect(
      a.db.query("SELECT id FROM transactions WHERE uuid = ?", [uuid]).length,
    ).toBe(0);

    b.db.run(
      "UPDATE transactions SET amount = 40, updated_at = ? WHERE uuid = ?",
      [t(60), uuid],
    );
    await b.save();
    const pkgB2 = await exportSharePackage(b);
    const { preview: preview2 } = await importPackage(a, pkgB2.zip, {
      days: { "2026-08-19": { mode: "keep_local" } },
    });
    expect(preview2.summary.conflict_days[0].items[0].kind).toBe("resurrect");
    expect(
      a.db.query("SELECT id FROM transactions WHERE uuid = ?", [uuid]).length,
    ).toBe(0);
  });

  it("suspected duplicate default keeps both", async () => {
    const a = await openA();
    await seedTx(a, { date: "2026-08-19", amount: 30, merchant: "奶茶店" });

    const b = await openB();
    await seedTx(b, { date: "2026-08-19", amount: 30, merchant: "奶茶店" });
    const pkgB = await exportSharePackage(b);

    const { preview, result } = await importPackage(a, pkgB.zip);
    expect(preview.summary.dupes.length).toBe(1);
    expect(result.add_transactions).toBe(1);
    expect(a.listTransactions().length).toBe(2);

    await importPackage(a, pkgB.zip);
    expect(a.listTransactions().length).toBe(2);
  });

  it("goal conflicts and required decisions", async () => {
    const a = await openA();
    const goalId = a.addGoal({ name: "耳机", price: 1299 });
    const uuid = a.db.query<{ uuid: string }>(
      "SELECT uuid FROM goals WHERE id = ?",
      [goalId],
    )[0].uuid;
    await a.save();
    const pkg1 = await exportSharePackage(a);

    const b = await openB();
    await importPackage(b, pkg1.zip);

    a.db.run("UPDATE goals SET saved = 50, updated_at = ? WHERE uuid = ?", [
      t(0),
      uuid,
    ]);
    await a.save();
    const pkg2 = await exportSharePackage(a);

    b.db.run("UPDATE goals SET saved = 60, updated_at = ? WHERE uuid = ?", [
      t(30),
      uuid,
    ]);
    await b.save();
    const { preview } = await importPackage(b, pkg2.zip);
    expect(preview.summary.goal_conflicts[0].kind).toBe("both_changed");
    expect(preview.summary.goal_conflicts[0].suggested).toBe("local");
    expect(
      b.db.query<{ saved: number }>("SELECT saved FROM goals WHERE uuid = ?", [uuid])[0].saved,
    ).toBe(60);

    // 删除 vs 修改：缺决策必须拒绝且回滚
    a.db.run(
      `UPDATE goals SET deleted_at = '${t(60)}', updated_at = '${t(60)}' WHERE uuid = ?`,
      [uuid],
    );
    a.db.run(
      `INSERT INTO sync_tombstones(uuid, kind, deleted_at, device_id, synced)
       VALUES (?, 'goal', '${t(60)}', ?, 0)`,
      [uuid, a.localDeviceId()],
    );
    await a.save();
    const pkg3 = await exportSharePackage(a);

    b.db.run("UPDATE goals SET updated_at = ? WHERE uuid = ?", [
      t(90),
      uuid,
    ]);
    await b.save();
    const before = b.db.query<{ saved: number }>(
      "SELECT saved FROM goals WHERE uuid = ?",
      [uuid],
    )[0].saved;
    await expect(importPackage(b, pkg3.zip, {})).rejects.toThrow(ShareError);
    expect(
      b.db.query<{ saved: number }>("SELECT saved FROM goals WHERE uuid = ?", [uuid])[0].saved,
    ).toBe(before);

    await importPackage(b, pkg3.zip, { goals: { [uuid]: "local" } });
    expect(
      b.db.query<{ deleted_at: string }>(
        "SELECT deleted_at FROM goals WHERE uuid = ?",
        [uuid],
      )[0].deleted_at,
    ).toBe("");
  });

  it("settings preview and apply", async () => {
    const a = await openA();
    await a.updateConfig({ monthly_budget: 2000, cooldown_days: 10 });
    const pkgA = await exportSharePackage(a);

    const b = await openB();
    const { preview } = await importPackage(b, pkgA.zip, {
      settings: "apply_package",
    });
    expect(preview.settings.conflict).toBe(true);
    expect(b.getConfig().monthly_budget).toBe(2000);
    expect(b.getConfig().cooldown_days).toBe(10);

    await b.updateConfig({ monthly_budget: 3000 });
    await importPackage(b, pkgA.zip);
    expect(b.getConfig().monthly_budget).toBe(3000);
  });
});

describe("坏包拒绝", () => {
  async function makeValidZip(): Promise<Uint8Array> {
    const a = await openA();
    await seedTx(a, { date: "2026-08-19", amount: 30 });
    const exported = await exportSharePackage(a);
    return exported.zip;
  }

  async function rebuild(
    zipBytes: Uint8Array,
    mutate?: (name: string, data: string) => string,
    extra?: { name: string; data: string; symlink?: boolean }[],
  ): Promise<Uint8Array> {
    const src = await JSZip.loadAsync(zipBytes);
    const out = new JSZip();
    for (const [name, entry] of Object.entries(src.files)) {
      if (entry.dir) continue;
      let data = await entry.async("string");
      if (mutate) data = mutate(name, data);
      out.file(name, data);
    }
    for (const item of extra || []) {
      if (item.symlink) {
        out.file(item.name, item.data, { unixPermissions: 0o120777 });
      } else {
        out.file(item.name, item.data);
      }
    }
    return out.generateAsync({ type: "uint8array", compression: "DEFLATE", platform: "UNIX" });
  }

  it("wrong format / unknown member / symlink / missing member / duplicate uuid", async () => {
    const good = await makeValidZip();

    await expect(
      parseSharePackage(
        await rebuild(good, (name, data) =>
          name === "manifest.json"
            ? data.replace('"better-money-share"', '"better-money-backup"')
            : data,
        ),
      ),
    ).rejects.toThrow("不是 Better-money 共享包");

    await expect(
      parseSharePackage(await rebuild(good, undefined, [{ name: "api_key.json", data: "{}" }])),
    ).rejects.toThrow("未知文件");

    await expect(
      parseSharePackage(
        await rebuild(good, undefined, [{ name: "manifest.json", data: "{}", symlink: true }]),
      ),
    ).rejects.toThrow("符号链接");

    const src = await JSZip.loadAsync(good);
    const out = new JSZip();
    for (const [name, entry] of Object.entries(src.files)) {
      if (entry.dir || name === "goals.json") continue;
      out.file(name, await entry.async("string"));
    }
    const missing = await out.generateAsync({ type: "uint8array" });
    await expect(parseSharePackage(missing)).rejects.toThrow("缺少文件");

    await expect(
      parseSharePackage(
        await rebuild(good, (name, data) => {
          if (name !== "transactions.json") return data;
          const txs = JSON.parse(data);
          txs.push({ ...txs[0] });
          return JSON.stringify(txs);
        }),
      ),
    ).rejects.toThrow("重复 uuid");
  });
});

describe("迁移", () => {
  it("v2 旧库升级到 v3 并回填 uuid", async () => {
    const initSqlJs = (await import("sql.js")).default;
    const SQL = await initSqlJs();
    const db = new SQL.Database();
    db.run(`
      CREATE TABLE transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL, amount REAL NOT NULL, type TEXT NOT NULL,
        category TEXT NOT NULL DEFAULT '其他', merchant TEXT DEFAULT '',
        note TEXT DEFAULT '', source TEXT DEFAULT '手动',
        estimated INTEGER DEFAULT 0, created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
      );
      CREATE TABLE line_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        transaction_id INTEGER NOT NULL REFERENCES transactions(id),
        name TEXT NOT NULL, qty REAL DEFAULT 1, price REAL DEFAULT 0
      );
      CREATE TABLE goals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL, price REAL NOT NULL, saved REAL DEFAULT 0,
        priority INTEGER DEFAULT 100, status TEXT DEFAULT '冷静期',
        cooldown_until TEXT DEFAULT '', expected_date TEXT DEFAULT '',
        note TEXT DEFAULT '', created_at TEXT NOT NULL, achieved_at TEXT DEFAULT ''
      );
      CREATE TABLE summaries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        period_type TEXT NOT NULL, period_start TEXT NOT NULL,
        period_end TEXT NOT NULL, content TEXT DEFAULT '',
        image_path TEXT DEFAULT '', expired INTEGER DEFAULT 0,
        created_at TEXT NOT NULL
      );
      CREATE TABLE adjustments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL, diff REAL NOT NULL, note TEXT DEFAULT '',
        created_at TEXT NOT NULL,
        reverses_adjustment_id INTEGER REFERENCES adjustments(id)
      );
      CREATE TABLE savings_wins (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        goal_name TEXT NOT NULL, amount REAL NOT NULL, date TEXT NOT NULL,
        created_at TEXT NOT NULL
      );
      CREATE TABLE pending_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        raw_text TEXT DEFAULT '', image_path TEXT DEFAULT '',
        created_at TEXT NOT NULL
      );
      PRAGMA user_version = 2;
    `);
    db.run(
      `INSERT INTO transactions(date, amount, type, category, created_at, updated_at)
       VALUES ('2026-08-01', 20, '支出', '餐饮', '2026-08-01 10:00:00', '2026-08-01 10:00:00')`,
    );
    const bytes = db.export();
    db.close();

    const storage = new MemoryStorage();
    storage.files.set("data/better_money.db", bytes);
    const repo = await LedgerRepo.open(storage);
    const rows = repo.db.query<{ uuid: string; device_id: string; deleted_at: string; last_synced_at: string }>(
      "SELECT * FROM transactions",
    );
    expect(rows.length).toBe(1);
    expect(rows[0].uuid).toMatch(/^[0-9a-f]{32}$/);
    expect(rows[0].device_id).toBe(repo.localDeviceId());
    expect(rows[0].deleted_at).toBe("");
    expect(rows[0].last_synced_at).toBe("");
    repo.db.close();
  });
});

describe("diff 直测", () => {
  it("keeps last_synced semantics for pending changes", async () => {
    const a = await openA();
    await seedTx(a, { date: "2026-08-19", amount: 30 });
    expect(a.syncStatus().pending_changes).toBe(1);
    const exported = await exportSharePackage(a);
    expect(a.syncStatus().pending_changes).toBe(0);
    a.db.run(
      "UPDATE transactions SET updated_at = ? WHERE amount = 30",
      [localNowSql(new Date(Date.now() + 60_000))],
    );
    await a.save();
    expect(a.syncStatus().pending_changes).toBe(1);
    void exported;
  });
});
