/** sql.js 数据库封装：加载/持久化/查询辅助。 */
import initSqlJs, { type Database, type SqlJsStatic, type SqlValue } from "sql.js";
import { runMigrations, userVersion } from "./migrations";
import { SCHEMA_VERSION } from "./schema";

export const DB_FILE = "data/better_money.db";
export const DB_BAK_FILE = "data/better_money.db.bak";

export interface StorageAdapter {
  read(name: string): Promise<Uint8Array | null>;
  write(name: string, data: Uint8Array): Promise<void>;
  delete(name: string): Promise<void>;
  exists(name: string): Promise<boolean>;
}

let SQL: SqlJsStatic | null = null;

export async function initSqlJsRuntime(wasmUrl?: string): Promise<SqlJsStatic> {
  if (SQL) return SQL;
  const options: { locateFile?: (file: string) => string; wasmBinary?: ArrayBuffer } = {};
  const isNode = typeof process !== "undefined" && !!process.versions?.node;
  if (isNode) {
    // Node（测试/工具）：直接读包内 wasm 二进制，避免路径解析问题
    const moduleName = "module";
    const { createRequire } = await import(/* @vite-ignore */ moduleName);
    const nodeFs = "node:fs";
    const fs = await import(/* @vite-ignore */ nodeFs);
    const req = createRequire(import.meta.url);
    options.wasmBinary = fs.readFileSync(req.resolve("sql.js/dist/sql-wasm.wasm"));
  } else if (wasmUrl) {
    options.locateFile = (file) => (file.endsWith(".wasm") ? wasmUrl : file);
  }
  SQL = await initSqlJs(options);
  return SQL;
}

export class AppDatabase {
  private db: Database;
  private dirty = false;

  private constructor(db: Database) {
    this.db = db;
  }

  static async open(storage: StorageAdapter, wasmUrl?: string): Promise<AppDatabase> {
    const SQLRuntime = await initSqlJsRuntime(wasmUrl);
    const existing = await storage.read(DB_FILE);
    if (existing) {
      try {
        const db = new SQLRuntime.Database(existing);
        const previousVersion = userVersion(db);
        runMigrations(db);
        const wrapped = new AppDatabase(db);
        if (previousVersion !== SCHEMA_VERSION) wrapped.markDirty();
        return wrapped;
      } catch {
        // 主库损坏 → 尝试 .bak
        const backup = await storage.read(DB_BAK_FILE);
        if (backup) {
          const db = new SQLRuntime.Database(backup);
          runMigrations(db);
          const wrapped = new AppDatabase(db);
          wrapped.markDirty();
          return wrapped;
        }
        const db = new SQLRuntime.Database();
        runMigrations(db);
        const wrapped = new AppDatabase(db);
        wrapped.markDirty();
        return wrapped;
      }
    }
    const db = new SQLRuntime.Database();
    runMigrations(db);
    const wrapped = new AppDatabase(db);
    wrapped.markDirty();
    return wrapped;
  }

  raw(): Database {
    return this.db;
  }

  markDirty() {
    this.dirty = true;
  }

  /** 写操作后调用：原子落盘（先写 .tmp 再改名，保留上一版为 .bak）。 */
  async save(storage: StorageAdapter): Promise<void> {
    if (!this.dirty) return;
    const data = this.db.export();
    const previous = await storage.read(DB_FILE).catch(() => null);
    await storage.write(DB_FILE + ".tmp", data);
    if (previous) await storage.write(DB_BAK_FILE, previous);
    await storage.write(DB_FILE, data);
    await storage.delete(DB_FILE + ".tmp").catch(() => undefined);
    this.dirty = false;
  }

  /** 只读查询 → 对象数组。 */
  query<T = Record<string, SqlValue>>(sql: string, params: SqlValue[] = []): T[] {
    const statement = this.db.prepare(sql);
    try {
      statement.bind(params);
      const result: T[] = [];
      while (statement.step()) {
        result.push(statement.getAsObject() as T);
      }
      return result;
    } finally {
      statement.free();
    }
  }

  queryOne<T = Record<string, SqlValue>>(sql: string, params: SqlValue[] = []): T | null {
    const rows = this.query<T>(sql, params);
    return rows.length ? rows[0] : null;
  }

  run(sql: string, params: SqlValue[] = []): void {
    this.db.run(sql, params);
    this.dirty = true;
  }

  execScript(sql: string): void {
    this.db.exec(sql);
    this.dirty = true;
  }

  transaction<T>(work: () => T): T {
    this.db.run("BEGIN IMMEDIATE");
    try {
      const result = work();
      this.db.run("COMMIT");
      this.dirty = true;
      return result;
    } catch (error) {
      this.db.run("ROLLBACK");
      throw error;
    }
  }

  close() {
    this.db.close();
  }
}
