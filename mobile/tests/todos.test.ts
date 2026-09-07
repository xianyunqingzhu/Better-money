import { afterEach, beforeEach, expect, it } from "vitest";
import { AppDatabase, DB_FILE, type StorageAdapter } from "../src/db/database";
import { TodoRepo } from "../src/db/todos";

class MemoryStorage implements StorageAdapter {
  files = new Map<string, Uint8Array>();
  async read(name: string) { return this.files.get(name) ?? null; }
  async write(name: string, bytes: Uint8Array) { this.files.set(name, bytes.slice()); }
  async delete(name: string) { this.files.delete(name); }
  async exists(name: string) { return this.files.has(name); }
}
let storage: MemoryStorage;
let db: AppDatabase;
let todos: TodoRepo;
beforeEach(async () => {
  storage = new MemoryStorage();
  db = await AppDatabase.open(storage);
  todos = new TodoRepo(db);
});
afterEach(() => db.close());

it("deletes active and completed todos permanently without affecting the others", async () => {
  const a = todos.create("删除未完成");
  const b = todos.create("删除已完成");
  const c = todos.create("保留");
  todos.complete(b);
  todos.remove(a);
  todos.remove(b);
  todos.remove(b);
  todos.restore(b);
  await db.save(storage);
  db.close();
  db = await AppDatabase.open(storage);
  todos = new TodoRepo(db);
  expect(todos.list().map(t => t.id)).toEqual([c]);
  expect(todos.list(true)).toEqual([]);
});

it("creates, edits and orders todos without changing their identity", () => {
  const a = todos.create("  交电费  ");
  const b = todos.create("买菜");
  const c = todos.create("读书");
  todos.edit(b, "买水果");
  todos.move(c, -1);
  todos.move(a, -1);
  expect(todos.list().map(t => t.title)).toEqual(["交电费", "读书", "买水果"]);
  todos.move(c, 1);
  todos.move(c, 1);
  expect(todos.list().map(t => t.id)).toEqual([a, b, c]);
  expect(() => todos.create(" \n ")).toThrow();
  expect(() => todos.edit(b, "")).toThrow();
  expect(todos.list()).toHaveLength(3);
});

it("completes into history, restores once at the end and survives reopening", async () => {
  const a = todos.create("交电费");
  const b = todos.create("买菜");
  todos.complete(a);
  todos.complete(a);
  expect(todos.list().map(t => t.id)).toEqual([b]);
  expect(todos.list(true)[0].completed_at).not.toBe("");
  await db.save(storage);
  db.close();
  db = await AppDatabase.open(storage);
  todos = new TodoRepo(db);
  expect(todos.list(true).map(t => t.id)).toEqual([a]);
  todos.restore(a);
  todos.restore(a);
  expect(todos.list().map(t => t.id)).toEqual([b, a]);
  expect(todos.list(true)).toEqual([]);
});

it("migrates a v4 database preserving existing data and persists the new schema", async () => {
  db.run("CREATE TABLE migration_sentinel (value TEXT)");
  db.run("INSERT INTO migration_sentinel VALUES ('existing')");
  db.run("DROP TABLE todos");
  db.run("PRAGMA user_version = 4");
  await db.save(storage);
  db.close();
  db = await AppDatabase.open(storage);
  expect(db.queryOne<{ value: string }>("SELECT value FROM migration_sentinel")?.value).toBe("existing");
  expect(new TodoRepo(db).list()).toEqual([]);
  await db.save(storage);
  const exported = storage.files.get(DB_FILE)!;
  const raw = db.raw();
  const restored = new (raw.constructor as new (bytes: Uint8Array) => typeof raw)(exported);
  expect(restored.exec("PRAGMA user_version")[0].values[0][0]).toBe(5);
  restored.close();
});
