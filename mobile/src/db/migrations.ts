/** 版本化迁移（app/migrations.py 的 TS 移植）。 */
import type { Database } from "sql.js";
import { BASE_SCHEMA, SYNC_SCHEMA } from "./schema";

export function uuidHex(): string {
  if (globalThis.crypto && "randomUUID" in globalThis.crypto) {
    return globalThis.crypto.randomUUID().replace(/-/g, "");
  }
  let result = "";
  for (let i = 0; i < 32; i++) {
    result += Math.floor(Math.random() * 16).toString(16);
  }
  return result;
}

function tableColumns(db: Database, table: string): Set<string> {
  const columns = new Set<string>();
  const rows = db.exec(`PRAGMA table_info(${table})`);
  if (!rows.length) return columns;
  for (const row of rows[0].values) columns.add(String(row[1]));
  return columns;
}

function tableExists(db: Database, table: string): boolean {
  const rows = db.exec(
    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
    [table],
  );
  return rows.length > 0 && rows[0].values.length > 0;
}

function addColumnIfMissing(db: Database, table: string, column: string, definition: string) {
  if (!tableExists(db, table)) return; // 全新库由 BASE_SCHEMA 建表，无需 ALTER
  if (!tableColumns(db, table).has(column)) {
    db.run(`ALTER TABLE ${table} ADD COLUMN ${column} ${definition}`);
  }
}

export function migrateToVersion3(db: Database) {
  const hasUserTables = tableExists(db, "transactions") || tableExists(db, "goals");
  if (hasUserTables) {
    // 旧库升级：只补列 + 同步元数据，绝不重建被删的用户表。
    for (const [column, definition] of [
      ["uuid", "TEXT NOT NULL DEFAULT ''"],
      ["device_id", "TEXT NOT NULL DEFAULT ''"],
      ["deleted_at", "TEXT NOT NULL DEFAULT ''"],
      ["last_synced_at", "TEXT NOT NULL DEFAULT ''"],
    ] as const) {
      addColumnIfMissing(db, "transactions", column, definition);
    }
    addColumnIfMissing(db, "line_items", "uuid", "TEXT NOT NULL DEFAULT ''");
    addColumnIfMissing(db, "line_items", "updated_at", "TEXT NOT NULL DEFAULT ''");
    for (const column of ["uuid", "device_id", "updated_at", "deleted_at", "last_synced_at"]) {
      addColumnIfMissing(db, "goals", column, "TEXT NOT NULL DEFAULT ''");
    }
    for (const column of ["uuid", "device_id", "updated_at", "deleted_at", "last_synced_at"]) {
      addColumnIfMissing(db, "savings_wins", column, "TEXT NOT NULL DEFAULT ''");
    }
    db.exec(SYNC_SCHEMA);
  } else {
    // 全新库：完整 DDL（含全部用户表与索引）
    db.exec(BASE_SCHEMA);
  }

  // 3. 稳定 UUID 回填（旧数据）
  for (const table of ["transactions", "goals", "line_items", "savings_wins"]) {
    const rows = db.exec(`SELECT id FROM ${table} WHERE uuid = ''`);
    if (!rows.length) continue;
    for (const row of rows[0].values) {
      db.run(`UPDATE ${table} SET uuid = ? WHERE id = ?`, [uuidHex(), Number(row[0])]);
    }
  }

  const now = localNowSql();
  db.run(
    "UPDATE goals SET updated_at = created_at WHERE updated_at = '' AND created_at <> ''",
  );
  db.run("UPDATE goals SET updated_at = ? WHERE updated_at = ''", [now]);
  db.run(
    "UPDATE savings_wins SET updated_at = created_at WHERE updated_at = '' AND created_at <> ''",
  );
  db.run("UPDATE savings_wins SET updated_at = ? WHERE updated_at = ''", [now]);
  db.run(
    `UPDATE line_items SET updated_at = COALESCE((
       SELECT updated_at FROM transactions t WHERE t.id = line_items.transaction_id), ?)
     WHERE updated_at = ''`,
    [now],
  );

  // 时间戳归一化：共享包要求 "%Y-%m-%d %H:%M:%S"
  const tsGlob = "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]";
  for (const [table, columns] of [
    ["transactions", ["created_at", "updated_at"]],
    ["goals", ["created_at", "updated_at"]],
    ["savings_wins", ["created_at", "updated_at"]],
    ["line_items", ["updated_at"]],
  ] as const) {
    for (const column of columns) {
      db.run(`UPDATE ${table} SET ${column} = ? WHERE ${column} NOT GLOB ?`, [now, tsGlob]);
    }
  }

  db.run(`PRAGMA user_version = 3`);
}

/** v4：退款配对字段。 */
export function migrateToVersion4(db: Database) {
  if (tableExists(db, "transactions")) {
    addColumnIfMissing(db, "transactions", "refund_of", "TEXT NOT NULL DEFAULT ''");
  }
  db.run(
    "CREATE INDEX IF NOT EXISTS idx_transactions_refund_of ON transactions(refund_of)",
  );
  db.run("PRAGMA user_version = 4");
}

/** 与桌面 now_str() 格式一致的本地时间。 */
export function localNowSql(now: Date = new Date()): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())} ` +
    `${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}`;
}

export function runMigrations(db: Database) {
  const version = userVersion(db);
  if (version < 3) migrateToVersion3(db);
  if (version < 4) migrateToVersion4(db);
}

export function userVersion(db: Database): number {
  const rows = db.exec("PRAGMA user_version");
  return Number(rows[0]?.values[0]?.[0] || 0);
}
