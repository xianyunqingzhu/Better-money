/** 仓储层：交易/目标/总结/对账/待处理（app/main.py 端点的 TS 移植）。 */
import type { SqlValue } from "sql.js";
import { addDays, monthBounds, todayIso, toIso } from "../domain/dates";
import { allocateSavings } from "../domain/goals";
import { toCents } from "../domain/money";
import { calculateBalance } from "../domain/ledger";
import {
  DEFAULT_CONFIG,
  type AdjustmentRow,
  type AppConfig,
  type GoalRow,
  type LineItemRow,
  type PendingItemRow,
  type SavingsWinRow,
  type SummaryRow,
  type TransactionRow,
  VALID_TYPES,
} from "../domain/types";
import { AppDatabase } from "./database";
import type { StorageAdapter } from "./database";
import { localNowSql, uuidHex } from "./migrations";

export const CONFIG_FILE = "data/config.json";

export interface NewTransaction {
  date: string;
  amount: number;
  type: string;
  category: string;
  merchant?: string;
  note?: string;
  source?: string;
  estimated?: number;
  line_items?: { name: string; qty: number; price: number }[];
  uuid?: string;
  device_id?: string;
  created_at?: string;
  updated_at?: string;
  /** 退款配对：目标原支出 uuid；"__none__"=显式不配对；空=自动匹配 */
  refund_of?: string;
}

export interface RefundPairing {
  original_uuid: string;
  original_date: string;
  original_merchant: string;
  refund_amount: number;
  full: boolean;
}

export class LedgerRepo {
  readonly db: AppDatabase;
  private storage: StorageAdapter;
  private config: AppConfig;
  private deviceId: string;

  private constructor(db: AppDatabase, storage: StorageAdapter, config: AppConfig) {
    this.db = db;
    this.storage = storage;
    this.config = config;
    this.deviceId = config.device_id || "";
  }

  static async open(storage: StorageAdapter, wasmUrl?: string): Promise<LedgerRepo> {
    const db = await AppDatabase.open(storage, wasmUrl);
    let config: AppConfig = { ...DEFAULT_CONFIG };
    try {
      const raw = await storage.read(CONFIG_FILE);
      if (raw) {
        const parsed = JSON.parse(new TextDecoder().decode(raw));
        config = { ...DEFAULT_CONFIG, ...parsed };
      }
    } catch {
      config = { ...DEFAULT_CONFIG };
    }
    const repo = new LedgerRepo(db, storage, config);
    repo.ensureDeviceIdentity();
    return repo;
  }

  getConfig(): AppConfig {
    return { ...this.config };
  }

  async updateConfig(patch: Partial<AppConfig>): Promise<void> {
    this.config = { ...this.config, ...patch };
    await this.storage.write(
      CONFIG_FILE,
      new TextEncoder().encode(JSON.stringify(this.config, null, 2)),
    );
  }

  ensureDeviceIdentity() {
    let changed = false;
    if (!this.config.device_id) {
      this.config.device_id = uuidHex();
      changed = true;
    }
    if (!this.config.device_name) {
      this.config.device_name = "手机";
      changed = true;
    }
    this.deviceId = this.config.device_id;
    const now = localNowSql();
    this.db.run(
      `INSERT INTO sync_devices(device_id, name, platform, created_at)
       VALUES (?, ?, 'mobile', ?)
       ON CONFLICT(device_id) DO UPDATE SET name = excluded.name`,
      [this.config.device_id, this.config.device_name, now],
    );
    for (const table of ["transactions", "goals", "savings_wins"]) {
      this.db.run(`UPDATE ${table} SET device_id = ? WHERE device_id = ''`, [
        this.config.device_id,
      ]);
    }
    if (changed) {
      void this.storage.write(
        CONFIG_FILE,
        new TextEncoder().encode(JSON.stringify(this.config, null, 2)),
      );
    }
  }

  localDeviceId(): string {
    if (!this.deviceId) this.ensureDeviceIdentity();
    return this.deviceId;
  }

  // ---------- 交易 ----------

  listTransactions(limit = 1000): TransactionRow[] {
    return this.db.query<TransactionRow>(
      "SELECT * FROM transactions WHERE deleted_at = '' ORDER BY date DESC, id DESC LIMIT ?",
      [limit],
    );
  }

  /** 先筛选再分页，避免旧月份被全局最近 1000 笔的上限截掉。 */
  listTransactionsFiltered(
    filters: { month?: string; type?: string; category?: string; keyword?: string },
    limit = 100,
    offset = 0,
  ): TransactionRow[] {
    const clauses = ["deleted_at = ''"];
    const params: SqlValue[] = [];
    if (filters.month) {
      clauses.push("substr(date, 1, 7) = ?");
      params.push(filters.month);
    }
    if (filters.type) {
      clauses.push("type = ?");
      params.push(filters.type);
    }
    if (filters.category) {
      clauses.push("category = ?");
      params.push(filters.category);
    }
    const keyword = filters.keyword?.trim();
    if (keyword) {
      clauses.push("(instr(merchant, ?) > 0 OR instr(note, ?) > 0 OR instr(category, ?) > 0)");
      params.push(keyword, keyword, keyword);
    }
    return this.db.query<TransactionRow>(
      `SELECT * FROM transactions WHERE ${clauses.join(" AND ")} ORDER BY date DESC, id DESC LIMIT ? OFFSET ?`,
      [...params, limit, offset],
    );
  }

  addTransaction(tx: NewTransaction): { id: number; savings_allocations: { goal_id: number; goal_name: string; amount: number }[] } {
    const now = localNowSql();
    const result = this.db.transaction(() => {
      this.db.run(
        `INSERT INTO transactions(date, amount, type, category, merchant, note, source,
          estimated, created_at, updated_at, uuid, device_id, refund_of)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
        [
          tx.date,
          tx.amount,
          tx.type,
          tx.category,
          tx.merchant ?? "",
          tx.note ?? "",
          tx.source ?? "手动",
          tx.estimated ?? 0,
          tx.created_at ?? now,
          tx.updated_at ?? now,
          tx.uuid ?? uuidHex(),
          tx.device_id ?? this.localDeviceId(),
          tx.refund_of ?? "",
        ],
      );
      const id = this.db.queryOne<{ id: number }>(
        "SELECT last_insert_rowid() AS id",
      )!.id;
      const allocations =
        tx.type === "收入"
          ? this.applyAutoSave({ amount: tx.amount })
          : [];
      for (const item of tx.line_items ?? []) {
        this.db.run(
          `INSERT INTO line_items(transaction_id, name, qty, price, uuid, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)`,
          [id, item.name, item.qty, item.price, uuidHex(), now],
        );
      }
      return { id, savings_allocations: allocations };
    });
    this.markSummariesExpired(tx.date);
    return result;
  }

  applyAutoSave(incomeItem: { amount: number }) {
    const ratio = this.config.auto_save_ratio || 0;
    if (ratio <= 0) return [];
    const amount = Math.round(toCents(incomeItem.amount) * ratio) / 100;
    const allocations = allocateSavings(this.listGoals(), amount);
    for (const allocation of allocations) {
      this.db.run("UPDATE goals SET saved = saved + ?, updated_at = ? WHERE id = ?", [
        allocation.amount,
        localNowSql(),
        allocation.goalId,
      ]);
    }
    return allocations.map((a) => ({
      goal_id: a.goalId,
      goal_name: a.goalName,
      amount: a.amount,
    }));
  }

  /** 解析/确认面板批量入账（含单品明细与退款配对），对应 _save_items。 */
  saveItems(items: NewTransaction[], source = "文字") {
    const now = localNowSql();
    const deviceId = this.localDeviceId();
    const saved: { id: number; refund_paired?: RefundPairing }[] = [];
    this.db.transaction(() => {
      for (const item of items) {
        let refundOf = "";
        if (item.type === "退款") {
          const explicit = item.refund_of ?? "";
          if (explicit === "__none__") refundOf = "";
          else if (explicit) refundOf = explicit;
          else {
            const candidates = this.findRefundCandidates(item);
            if (candidates.length) refundOf = candidates[0].uuid;
          }
        }
        let pairing: RefundPairing | null = null;
        if (refundOf) {
          pairing = this.pairRefund(item, refundOf);
          if (!pairing) {
            item.refund_of = "";
            refundOf = "";
          }
        }
        this.db.run(
          `INSERT INTO transactions(date, amount, type, category, merchant, note, source,
            estimated, created_at, updated_at, uuid, device_id, refund_of)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
          [
            item.date,
            item.amount,
            item.type,
            item.category,
            item.merchant ?? "",
            item.note ?? "",
            source,
            item.estimated ?? 0,
            now,
            now,
            item.uuid ?? uuidHex(),
            deviceId,
            refundOf,
          ],
        );
        const id = this.db.queryOne<{ id: number }>("SELECT last_insert_rowid() AS id")!.id;
        if (item.type === "收入") this.applyAutoSave(item);
        for (const li of item.line_items ?? []) {
          this.db.run(
            `INSERT INTO line_items(transaction_id, name, qty, price, uuid, updated_at)
             VALUES (?, ?, ?, ?, ?, ?)`,
            [id, li.name, li.qty ?? 1, li.price ?? 0, uuidHex(), now],
          );
        }
        saved.push(pairing ? { id, refund_paired: pairing } : { id });
      }
    });
    for (const item of items) this.markSummariesExpired(item.date);
    return { saved };
  }

  /** 退款配对候选：商家一致、可退余额足够、60 天内、最近日期优先。 */
  findRefundCandidates(
    item: { merchant?: string; amount: number; date: string },
  ): { uuid: string; date: string; amount: number; merchant: string; category: string; note: string; remaining: number }[] {
    const merchant = (item.merchant || "").trim();
    const amount = Math.round(item.amount * 100) / 100;
    if (!merchant || amount <= 0) return [];
    const anchor = new Date(item.date + "T00:00:00");
    if (Number.isNaN(anchor.getTime())) return [];
    const windowStart = new Date(anchor);
    windowStart.setDate(windowStart.getDate() - 60);
    const windowIso = toIso(windowStart);
    const rows = this.db.query<TransactionRow & { remaining: number }>(
      `SELECT t.*,
        (t.amount - COALESCE((SELECT SUM(r.amount) FROM transactions r
          WHERE r.refund_of = t.uuid AND r.deleted_at = ''), 0)) AS remaining
       FROM transactions t
       WHERE t.type = '支出' AND t.deleted_at = '' AND t.merchant = ?
         AND t.date <= ? AND t.date >= ?
       ORDER BY t.date DESC, t.id DESC`,
      [merchant, item.date, windowIso],
    );
    const candidates: { uuid: string; date: string; amount: number; merchant: string; category: string; note: string; remaining: number }[] = [];
    for (const row of rows) {
      const remaining = Math.round((row.remaining || 0) * 100) / 100;
      if (remaining + 0.005 >= amount) {
        candidates.push({
          uuid: row.uuid,
          date: row.date,
          amount: row.amount,
          merchant: row.merchant,
          category: row.category,
          note: row.note,
          remaining,
        });
      }
      if (candidates.length >= 8) break;
    }
    return candidates;
  }

  /** 修正原支出并改写退款条目；失败返回 null（按独立退款处理）。 */
  private pairRefund(item: NewTransaction, refundOfUuid: string): RefundPairing | null {
    const UUID_RE = /^[0-9a-fA-F]{32}$/;
    if (!UUID_RE.test(refundOfUuid)) return null;
    const original = this.db.queryOne<TransactionRow>(
      "SELECT * FROM transactions WHERE uuid = ? AND type = '支出' AND deleted_at = ''",
      [refundOfUuid],
    );
    if (!original) return null;
    const amount = Math.round(item.amount * 100) / 100;
    if (amount <= 0) return null;
    const alreadyRow = this.db.queryOne<{ n: number }>(
      "SELECT COALESCE(SUM(amount), 0) AS n FROM transactions WHERE refund_of = ? AND deleted_at = ''",
      [refundOfUuid],
    );
    const remaining = Math.round((original.amount - (alreadyRow?.n || 0)) * 100) / 100;
    if (remaining + 0.005 < amount) return null;
    const now = localNowSql();
    const full = Math.abs(remaining - amount) < 0.005;
    const noteSuffix = `（退货退 ¥${amount.toFixed(2)}）`;
    if (full) {
      this.db.run(
        "UPDATE transactions SET deleted_at = ?, updated_at = ? WHERE uuid = ?",
        [now, now, refundOfUuid],
      );
      this.db.run(
        `INSERT INTO sync_tombstones(uuid, kind, deleted_at, device_id, synced)
         VALUES (?, 'transaction', ?, ?, 0)
         ON CONFLICT(uuid) DO UPDATE SET deleted_at = excluded.deleted_at
         WHERE excluded.deleted_at > sync_tombstones.deleted_at`,
        [refundOfUuid, now, this.localDeviceId()],
      );
    } else {
      const newAmount = Math.round((original.amount - amount) * 100) / 100;
      this.db.run(
        `UPDATE transactions SET amount = ?, updated_at = ?,
          note = CASE WHEN note = '' OR note IS NULL THEN ?
                      WHEN note LIKE '%退货退%' THEN note ELSE note || ? END
         WHERE uuid = ?`,
        [newAmount, now, noteSuffix, noteSuffix, refundOfUuid],
      );
    }
    item.refund_of = refundOfUuid;
    item.date = original.date;
    if (!item.note) {
      item.note = `配对退货：${original.date} ${original.merchant}`;
    }
    return {
      original_uuid: refundOfUuid,
      original_date: original.date,
      original_merchant: original.merchant,
      refund_amount: amount,
      full,
    };
  }

  patchTransaction(id: number, patch: Partial<TransactionRow>): boolean {
    const row = this.db.queryOne<TransactionRow>(
      "SELECT date FROM transactions WHERE id = ? AND deleted_at = ''",
      [id],
    );
    if (!row) return false;
    const fields: string[] = [];
    const args: SqlValue[] = [];
    if (patch.date) {
      fields.push("date = ?");
      args.push(patch.date);
    }
    if (patch.amount && patch.amount > 0) {
      fields.push("amount = ?");
      args.push(patch.amount);
    }
    if (patch.type && (VALID_TYPES as readonly string[]).includes(patch.type)) {
      fields.push("type = ?");
      args.push(patch.type);
    }
    if (patch.category) {
      fields.push("category = ?");
      args.push(patch.category);
    }
    if (patch.merchant !== undefined) {
      fields.push("merchant = ?");
      args.push(patch.merchant);
    }
    if (patch.note !== undefined) {
      fields.push("note = ?");
      args.push(patch.note);
    }
    if (patch.estimated === 0 || patch.estimated === 1) {
      fields.push("estimated = ?");
      args.push(patch.estimated);
    }
    if (fields.length) {
      fields.push("updated_at = ?");
      args.push(localNowSql());
      this.db.run(`UPDATE transactions SET ${fields.join(", ")} WHERE id = ?`, [...args, id]);
    }
    this.markSummariesExpired(patch.date || row.date);
    return true;
  }

  /** 软删除：保留记录与 tombstone。 */
  softDeleteTransaction(id: number): boolean {
    const row = this.db.queryOne<TransactionRow>(
      "SELECT date, uuid FROM transactions WHERE id = ? AND deleted_at = ''",
      [id],
    );
    if (!row) return false;
    const now = localNowSql();
    this.db.run("UPDATE transactions SET deleted_at = ?, updated_at = ? WHERE id = ?", [
      now,
      now,
      id,
    ]);
    this.db.run(
      `INSERT INTO sync_tombstones(uuid, kind, deleted_at, device_id, synced)
       VALUES (?, 'transaction', ?, ?, 0)
       ON CONFLICT(uuid) DO UPDATE SET deleted_at = excluded.deleted_at`,
      [row.uuid, now, this.localDeviceId()],
    );
    this.markSummariesExpired(row.date);
    return true;
  }

  listLineItems(transactionId: number): LineItemRow[] {
    return this.db.query<LineItemRow>(
      "SELECT * FROM line_items WHERE transaction_id = ? ORDER BY id",
      [transactionId],
    );
  }

  // ---------- 目标 ----------

  listGoals(): GoalRow[] {
    return this.db.query<GoalRow>(
      "SELECT * FROM goals WHERE deleted_at = '' ORDER BY priority, id",
    );
  }

  addGoal(g: { name: string; price: number; expected_date?: string; note?: string }): number {
    const cfg = this.config;
    const until = addDays(todayIso(), cfg.cooldown_days || 7);
    const now = localNowSql();
    const maxp = this.db.queryOne<{ m: number }>(
      "SELECT COALESCE(MAX(priority), -1) + 1 AS m FROM goals WHERE deleted_at = ''",
    )!.m;
    this.db.run(
      `INSERT INTO goals(name, price, saved, priority, status, cooldown_until,
        expected_date, note, created_at, uuid, device_id, updated_at)
       VALUES (?, ?, 0, ?, '冷静期', ?, ?, ?, ?, ?, ?, ?)`,
      [
        g.name.trim(),
        g.price,
        maxp,
        until,
        g.expected_date ?? "",
        g.note ?? "",
        now,
        uuidHex(),
        this.localDeviceId(),
        now,
      ],
    );
    return this.db.queryOne<{ id: number }>("SELECT last_insert_rowid() AS id")!.id;
  }

  patchGoal(id: number, patch: Partial<GoalRow>): boolean {
    const exists = this.db.queryOne("SELECT id FROM goals WHERE id = ? AND deleted_at = ''", [id]);
    if (!exists) return false;
    const fields: string[] = [];
    const args: SqlValue[] = [];
    for (const key of ["name", "price", "saved", "expected_date", "note"] as const) {
      if (patch[key] !== undefined) {
        fields.push(`${key} = ?`);
        args.push(patch[key] as SqlValue);
      }
    }
    if (fields.length) {
      fields.push("updated_at = ?");
      args.push(localNowSql());
      this.db.run(`UPDATE goals SET ${fields.join(", ")} WHERE id = ?`, [...args, id]);
    }
    return true;
  }

  softDeleteGoal(id: number): { name: string; saved: number } | null {
    const goal = this.db.queryOne<GoalRow>(
      "SELECT name, saved, uuid FROM goals WHERE id = ? AND deleted_at = ''",
      [id],
    );
    if (!goal) return null;
    const now = localNowSql();
    this.db.run("UPDATE goals SET deleted_at = ?, updated_at = ? WHERE id = ?", [now, now, id]);
    this.db.run(
      `INSERT INTO sync_tombstones(uuid, kind, deleted_at, device_id, synced)
       VALUES (?, 'goal', ?, ?, 0)
       ON CONFLICT(uuid) DO UPDATE SET deleted_at = excluded.deleted_at`,
      [goal.uuid, now, this.localDeviceId()],
    );
    return { name: goal.name, saved: goal.saved };
  }

  goalAction(
    id: number,
    action: "pause" | "resume" | "abandon" | "want" | "pass" | "achieve_buy" | "achieve_freeze" | "up" | "down",
  ): boolean {
    const goal = this.db.queryOne<GoalRow>(
      "SELECT * FROM goals WHERE id = ? AND deleted_at = ''",
      [id],
    );
    if (!goal) return false;
    const today = todayIso();
    const now = localNowSql();
    const deviceId = this.localDeviceId();
    switch (action) {
      case "pause":
        this.db.run("UPDATE goals SET status = '已暂停', updated_at = ? WHERE id = ?", [now, id]);
        break;
      case "resume":
        this.db.run("UPDATE goals SET status = '进行中', updated_at = ? WHERE id = ?", [now, id]);
        break;
      case "abandon":
        this.db.run("UPDATE goals SET status = '已放弃', saved = 0, updated_at = ? WHERE id = ?", [now, id]);
        break;
      case "want":
        this.db.run("UPDATE goals SET status = '进行中', updated_at = ? WHERE id = ?", [now, id]);
        break;
      case "pass":
        this.db.run("UPDATE goals SET status = '已放弃', saved = 0, updated_at = ? WHERE id = ?", [now, id]);
        this.db.run(
          `INSERT INTO savings_wins(goal_name, amount, date, created_at, uuid, device_id, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)`,
          [goal.name, goal.price, today, now, uuidHex(), deviceId, now],
        );
        break;
      case "achieve_buy":
        this.db.run(
          "UPDATE goals SET status = '已达成', achieved_at = ?, updated_at = ? WHERE id = ?",
          [now, now, id],
        );
        this.db.run(
          `INSERT INTO transactions(date, amount, type, category, merchant, note, source,
            estimated, created_at, updated_at, uuid, device_id)
           VALUES (?, ?, '支出', '购物', ?, '达成目标购买', '目标', 0, ?, ?, ?, ?)`,
          [today, goal.price, goal.name, now, now, uuidHex(), deviceId],
        );
        this.markSummariesExpired(today);
        break;
      case "achieve_freeze":
        this.db.run("UPDATE goals SET status = '已暂停', updated_at = ? WHERE id = ?", [now, id]);
        break;
      case "up":
      case "down": {
        const ordered = this.db.query<{ id: number }>(
          "SELECT id FROM goals WHERE deleted_at = '' ORDER BY priority, id",
        );
        const ids = ordered.map((r) => r.id);
        const i = ids.indexOf(id);
        const j = action === "up" ? i - 1 : i + 1;
        if (i >= 0 && j >= 0 && j < ids.length) {
          [ids[i], ids[j]] = [ids[j], ids[i]];
        }
        for (let k = 0; k < ids.length; k++) {
          this.db.run("UPDATE goals SET priority = ?, updated_at = ? WHERE id = ?", [k, now, ids[k]]);
        }
        break;
      }
      default:
        return false;
    }
    return true;
  }

  goalTransfer(fromId: number, toId: number, amount: number): "ok" | "not_found" | "not_enough" {
    const from = this.db.queryOne<GoalRow>(
      "SELECT id, saved FROM goals WHERE id = ? AND deleted_at = ''",
      [fromId],
    );
    const to = this.db.queryOne("SELECT id FROM goals WHERE id = ? AND deleted_at = ''", [toId]);
    if (!from || !to) return "not_found";
    if (from.saved < amount) return "not_enough";
    const now = localNowSql();
    this.db.run("UPDATE goals SET saved = saved - ?, updated_at = ? WHERE id = ?", [amount, now, fromId]);
    this.db.run("UPDATE goals SET saved = saved + ?, updated_at = ? WHERE id = ?", [amount, now, toId]);
    return "ok";
  }

  savingsWins(month: string): { total: number; count: number; items: SavingsWinRow[] } {
    const rows = this.db.query<SavingsWinRow>(
      "SELECT * FROM savings_wins WHERE deleted_at = '' AND date LIKE ? ORDER BY date DESC, id DESC",
      [month + "%"],
    );
    let total = 0;
    for (const row of rows) total += Math.round(toCents(row.amount));
    return { total: total / 100, count: rows.length, items: rows };
  }

  // ---------- 总结 ----------

  listSummaries(): SummaryRow[] {
    return this.db.query<SummaryRow>("SELECT * FROM summaries ORDER BY id DESC");
  }

  upsertSummary(summary: { period_type: string; period_start: string; period_end: string; content: string; overwrite?: boolean }):
    { overwritten: boolean; id: number } {
    const existing = this.db.queryOne<SummaryRow>(
      "SELECT id FROM summaries WHERE period_type = ? AND period_start = ? AND period_end = ?",
      [summary.period_type, summary.period_start, summary.period_end],
    );
    const now = localNowSql();
    if (existing) {
      if (!summary.overwrite) return { overwritten: false, id: existing.id };
      this.db.run(
        "UPDATE summaries SET content = ?, expired = 0, created_at = ? WHERE id = ?",
        [summary.content, now, existing.id],
      );
      return { overwritten: true, id: existing.id };
    }
    this.db.run(
      `INSERT INTO summaries(period_type, period_start, period_end, content, image_path, expired, created_at)
       VALUES (?, ?, ?, ?, '', 0, ?)`,
      [summary.period_type, summary.period_start, summary.period_end, summary.content, now],
    );
    return { overwritten: false, id: this.db.queryOne<{ id: number }>("SELECT last_insert_rowid() AS id")!.id };
  }

  deleteSummary(id: number): boolean {
    const exists = this.db.queryOne("SELECT id FROM summaries WHERE id = ?", [id]);
    if (!exists) return false;
    this.db.run("DELETE FROM summaries WHERE id = ?", [id]);
    return true;
  }

  markSummariesExpired(dateStr: string) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(dateStr)) return;
    this.db.run(
      "UPDATE summaries SET expired = 1 WHERE period_start <= ? AND period_end >= ?",
      [dateStr, dateStr],
    );
  }

  // ---------- 对账 ----------

  ledgerBalance(): number {
    const cfg = this.config;
    const cents = calculateBalance(cfg, this.allTransactions(), this.allAdjustments());
    return cents / 100;
  }

  addAdjustment(diff: number, note: string): { id: number } {
    this.db.run(
      "INSERT INTO adjustments(date, diff, note, created_at) VALUES (?, ?, ?, ?)",
      [todayIso(), diff, note || "对账校准", localNowSql()],
    );
    const id = this.db.queryOne<{ id: number }>("SELECT last_insert_rowid() AS id")!.id;
    this.markSummariesExpired(todayIso());
    return { id };
  }

  listAdjustments(): (AdjustmentRow & { reversed_by_id: number | null })[] {
    return this.db.query(
      `SELECT a.*, b.id AS reversed_by_id FROM adjustments a
       LEFT JOIN adjustments b ON b.reverses_adjustment_id = a.id
       ORDER BY a.id DESC`,
    );
  }

  reverseAdjustment(id: number): { ok: boolean; error?: string; reversal_id?: number } {
    const row = this.db.queryOne<AdjustmentRow>(
      "SELECT * FROM adjustments WHERE id = ?",
      [id],
    );
    if (!row) return { ok: false, error: "not_found" };
    const reversed = this.db.queryOne(
      "SELECT id FROM adjustments WHERE reverses_adjustment_id = ?",
      [id],
    );
    if (reversed) return { ok: false, error: "already_reversed" };
    this.db.run(
      `INSERT INTO adjustments(date, diff, note, created_at, reverses_adjustment_id)
       VALUES (?, ?, ?, ?, ?)`,
      [row.date, -row.diff, `撤销：${row.note}`, localNowSql(), row.id],
    );
    const reversalId = this.db.queryOne<{ id: number }>("SELECT last_insert_rowid() AS id")!.id;
    this.markSummariesExpired(row.date);
    return { ok: true, reversal_id: reversalId };
  }

  // ---------- 待处理 ----------

  listPending(): PendingItemRow[] {
    return this.db.query<PendingItemRow>("SELECT * FROM pending_items ORDER BY id DESC");
  }

  storePending(rawText: string, imagePath = "") {
    this.db.run(
      "INSERT INTO pending_items(raw_text, image_path, created_at) VALUES (?, ?, ?)",
      [rawText, imagePath, localNowSql()],
    );
  }

  deletePending(id: number) {
    this.db.run("DELETE FROM pending_items WHERE id = ?", [id]);
  }

  // ---------- 摘要数据 ----------

  allTransactions(includeDeleted = false): TransactionRow[] {
    if (includeDeleted) return this.db.query<TransactionRow>("SELECT * FROM transactions ORDER BY date, id");
    return this.db.query<TransactionRow>(
      "SELECT * FROM transactions WHERE deleted_at = '' ORDER BY date, id",
    );
  }

  allGoals(includeDeleted = false): GoalRow[] {
    if (includeDeleted) return this.db.query<GoalRow>("SELECT * FROM goals ORDER BY priority, id");
    return this.db.query<GoalRow>(
      "SELECT * FROM goals WHERE deleted_at = '' ORDER BY priority, id",
    );
  }

  allWins(includeDeleted = false): SavingsWinRow[] {
    if (includeDeleted) return this.db.query<SavingsWinRow>("SELECT * FROM savings_wins ORDER BY date, id");
    return this.db.query<SavingsWinRow>(
      "SELECT * FROM savings_wins WHERE deleted_at = '' ORDER BY date, id",
    );
  }

  allAdjustments(): AdjustmentRow[] {
    return this.db.query<AdjustmentRow>("SELECT * FROM adjustments ORDER BY date, id");
  }

  /** 同步状态：待同步变化数、上次导出/导入。 */
  syncStatus() {
    const pending = (table: string) => {
      const row = this.db.queryOne<{ n: number }>(
        `SELECT COUNT(*) AS n FROM ${table} WHERE
         (deleted_at = '' AND updated_at != last_synced_at)
         OR (deleted_at <> '' AND deleted_at > last_synced_at)`,
      );
      return row?.n || 0;
    };
    const device = this.config.device_id;
    const local = device
      ? this.db.queryOne<{ name: string; last_export_at: string; last_import_at: string }>(
        "SELECT name, last_export_at, last_import_at FROM sync_devices WHERE device_id = ?",
        [device],
      )
      : null;
    return {
      pending_changes:
        pending("transactions") + pending("goals") + pending("savings_wins"),
      device_name: local?.name || "手机",
      last_export_at: local?.last_export_at || "",
      last_import_at: local?.last_import_at || "",
    };
  }

  listSyncEvents(limit = 20) {
    return this.db.query("SELECT * FROM sync_events ORDER BY id DESC LIMIT ?", [limit]);
  }

  async save(): Promise<void> {
    await this.db.save(this.storage);
  }

  monthHasData(month: string): boolean {
    const { first, last } = monthBounds(month);
    const row = this.db.queryOne<{ n: number }>(
      "SELECT COUNT(*) AS n FROM transactions WHERE deleted_at = '' AND date BETWEEN ? AND ?",
      [first, last],
    );
    return (row?.n || 0) > 0;
  }
}
